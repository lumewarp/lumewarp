"""Member policies, last-admin protection and automatic key suspension."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from lumewarp_control_plane.authz import Authorizer, Decision
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigOutbox,
    KeyLifecycle,
    TeamMember,
    TeamRole,
    member_scope_id,
)
from tests.api.helpers import BOOTSTRAP_AUTH, auth, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_service_account,
    make_team,
    make_user,
)


async def test_member_upsert_patch_list_and_delete(client, committed) -> None:
    team = await make_team(committed)
    user = await make_user(committed, display_name="Member")
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/members/{user.id}"
    created = client.put(path, json={"role": "member", "rpm_limit": 10}, headers=BOOTSTRAP_AUTH)
    assert created.status_code == 201, created.text
    assert created.json()["member_scope_id"] == str(member_scope_id(team.id, user.id))
    assert created.json()["email"] == user.email
    assert created.json()["display_name"] == "Member"
    assert created.json()["user_active"] is True
    updated = client.put(path, json={"role": "member"}, headers=BOOTSTRAP_AUTH)
    assert updated.status_code == 200
    assert updated.json()["rpm_limit"] is None
    patched = client.patch(path, json={"rpm_limit": 5}, headers=BOOTSTRAP_AUTH)
    assert patched.status_code == 200
    assert patched.json()["rpm_limit"] == 5
    listed = client.get(f"/api/v1/teams/{team.id}/members", headers=BOOTSTRAP_AUTH)
    assert [r["user_id"] for r in listed.json()["items"]] == [str(user.id)]
    assert client.delete(path, headers=BOOTSTRAP_AUTH).status_code == 204
    assert client.patch(path, json={"role": "member"}, headers=BOOTSTRAP_AUTH).status_code == 404
    unknown = client.put(
        f"/api/v1/teams/{team.id}/members/{uuid.uuid4()}",
        json={"role": "member"},
        headers=BOOTSTRAP_AUTH,
    )
    assert unknown.status_code == 422
    assert "user_id" in unknown.json()["error"]["message"]


@pytest.mark.parametrize("caller", ["bootstrap", "admin", "superuser"])
@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE"])
async def test_last_admin_cannot_be_demoted_or_removed(client, committed, caller, method) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    if caller == "superuser":
        _, token = await user_with_token(committed, superuser=True)
    await committed.commit()
    headers = BOOTSTRAP_AUTH if caller == "bootstrap" else auth(token)
    path = f"/api/v1/teams/{team.id}/members/{admin.id}"
    response = client.request(
        method, path, json=None if method == "DELETE" else {"role": "member"}, headers=headers
    )
    assert response.status_code == 409, response.text
    assert "admin" in response.json()["error"]["message"]
    assert client.patch(path, json={"role": "admin"}, headers=headers).status_code == 200
    second = await make_user(committed)
    await make_member(committed, team, second, role=TeamRole.ADMIN)
    await committed.commit()
    response = client.request(
        method, path, json=None if method == "DELETE" else {"role": "member"}, headers=headers
    )
    assert response.status_code == (204 if method == "DELETE" else 200), response.text


async def test_removal_suspends_only_non_revoked_keys_in_that_team(client, committed) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    owner, token = await user_with_token(committed)
    await make_member(committed, team, owner)
    await make_member(committed, other, owner)
    active = await make_key(committed, team, owner)
    suspended = await make_key(committed, team, owner, lifecycle=KeyLifecycle.SUSPENDED)
    revoked = await make_key(
        committed, team, owner, lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC)
    )
    elsewhere = await make_key(committed, other, owner)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/members/{owner.id}"
    assert client.delete(path, headers=BOOTSTRAP_AUTH).status_code == 204
    for key in (active, suspended):
        body = client.get(f"/api/v1/keys/{key.id}", headers=BOOTSTRAP_AUTH).json()
        assert (body["lifecycle"], body["suspended_reason"]) == ("suspended", "owner_removed")
        assert (
            client.patch(
                f"/api/v1/keys/{key.id}", json={"lifecycle": "active"}, headers=BOOTSTRAP_AUTH
            ).status_code
            == 409
        )
    for key, lifecycle in ((revoked, "revoked"), (elsewhere, "active"), (sa_key, "active")):
        assert (
            client.get(f"/api/v1/keys/{key.id}", headers=BOOTSTRAP_AUTH).json()["lifecycle"]
            == lifecycle
        )
    audit = (
        await committed.scalars(
            select(AuditLog).where(AuditLog.team_id == team.id, AuditLog.action == "key.suspend")
        )
    ).all()
    assert {r.resource_id for r in audit} == {str(active.id), str(suspended.id)}
    assert all(r.details["reason"] == "owner_removed" for r in audit)
    assert client.get(f"/api/v1/teams/{team.id}", headers=auth(token)).status_code == 404
    assert client.put(path, json={"role": "member"}, headers=BOOTSTRAP_AUTH).status_code == 201
    restored = client.patch(
        f"/api/v1/keys/{active.id}", json={"lifecycle": "active"}, headers=auth(token)
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["suspended_reason"] is None
    restored = client.patch(
        f"/api/v1/keys/{suspended.id}", json={"lifecycle": "active"}, headers=BOOTSTRAP_AUTH
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["suspended_reason"] is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_external_grants_are_read_only(client, committed, monkeypatch, method) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()

    async def allow(user_id, relation, resource):
        return Decision.ALLOW

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(allow, "external"))
    path = f"/api/v1/teams/{team.id}/members"
    if method != "POST":
        path += f"/{user.id}"
    payload = {"role": "member"}
    if method == "POST":
        payload["email"] = "unknown@example.com"
    response = client.request(
        method, path, json=None if method == "DELETE" else payload, headers=auth(token)
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "managed_externally"
    assert client.get(f"/api/v1/teams/{team.id}/members", headers=auth(token)).status_code == 200


@pytest.mark.parametrize("selector", ["user_id", "email"])
@pytest.mark.parametrize("caller", ["admin", "bootstrap"])
async def test_create_member_by_id_or_email(client, committed, selector, caller) -> None:
    team = await make_team(committed, rpm_limit=20)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    user = await make_user(committed, display_name="New member")
    user.email = user.email.replace("@example.com", "@Example.COM")
    await committed.commit()
    headers = BOOTSTRAP_AUTH if caller == "bootstrap" else auth(token)
    path = f"/api/v1/teams/{team.id}/members"
    payload = {
        selector: str(user.id) if selector == "user_id" else user.email.swapcase(),
        "role": "member",
        "rpm_limit": 10,
    }
    outbox_before = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    created = client.post(path, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["user_id"], body["email"], body["display_name"], body["user_active"]) == (
        str(user.id),
        user.email,
        "New member",
        True,
    )
    assert body["rpm_limit"] == 10
    assert body["member_scope_id"] == str(member_scope_id(team.id, user.id))
    duplicate = client.post(path, json={**payload, "role": "admin"}, headers=headers)
    assert duplicate.status_code == 409, duplicate.text
    listed = client.get(path, headers=headers).json()["items"]
    assert next(row for row in listed if row["user_id"] == str(user.id))["role"] == "member"
    events = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.team_id == team.id, AuditLog.resource_id == str(user.id)
            )
        )
    ).all()
    assert [row.action for row in events] == ["member.add"]
    outbox_after = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    assert outbox_after == outbox_before + 1
    if caller == "admin":
        assert client.get("/api/v1/users", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"user_id": None, "email": None},
        {"user_id": str(uuid.uuid4()), "email": "someone@example.com"},
        {"user_id": "invalid"},
        {"email": ""},
        {"email": " "},
        {"email": "a" * 321},
    ],
)
async def test_member_create_requires_exactly_one_selector(client, committed, selector) -> None:
    team = await make_team(committed)
    await committed.commit()
    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"role": "member", **selector},
        headers=BOOTSTRAP_AUTH,
    )
    assert response.status_code == 422, response.text
    assert not (
        await committed.scalars(select(TeamMember).where(TeamMember.team_id == team.id))
    ).all()


@pytest.mark.parametrize("selector", ["unknown_id", "unknown_email", "partial", "wildcard"])
async def test_member_create_unknown_user_is_not_found(client, committed, selector) -> None:
    team = await make_team(committed)
    user = await make_user(committed)
    await committed.commit()
    payload = (
        {"user_id": str(uuid.uuid4())}
        if selector == "unknown_id"
        else {
            "email": {
                "unknown_email": f"unknown-{user.email}",
                "partial": user.email[:-1],
                "wildcard": "%",
            }[selector]
        }
    )
    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"role": "member", **payload},
        headers=BOOTSTRAP_AUTH,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "user_not_found"


@pytest.mark.parametrize("selector", ["user_id", "email", "put"])
async def test_inactive_user_cannot_be_added(client, committed, selector) -> None:
    team = await make_team(committed)
    user = await make_user(committed, is_active=False)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/members"
    method = "POST"
    payload = {"role": "member"}
    if selector == "put":
        path += f"/{user.id}"
        method = "PUT"
    else:
        payload[selector] = str(user.id) if selector == "user_id" else user.email.upper()
    response = client.request(method, path, json=payload, headers=BOOTSTRAP_AUTH)
    assert response.status_code == 409, response.text
    assert await committed.get(TeamMember, (team.id, user.id)) is None


async def test_member_list_includes_inactive_account_status(client, committed) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    user = await make_user(committed, is_active=False, display_name="Inactive member")
    await make_member(committed, team, user)
    await committed.commit()
    listed = client.get(f"/api/v1/teams/{team.id}/members", headers=auth(token))
    assert listed.status_code == 200, listed.text
    members = {row["user_id"]: row for row in listed.json()["items"]}
    assert members[str(admin.id)]["user_active"] is True
    assert members[str(user.id)]["user_active"] is False
    assert members[str(user.id)]["email"] == user.email
    assert members[str(user.id)]["display_name"] == "Inactive member"


async def test_external_members_list_is_empty_without_local_rows(client, committed, monkeypatch):
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        return Decision.ALLOW if user_id == str(user.id) else Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    response = client.get(f"/api/v1/teams/{team.id}/members", headers=auth(token))
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("caller", ["member", "outsider"])
async def test_member_creation_requires_team_admin(client, committed, caller) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    if caller == "member":
        await make_member(committed, team, user)
    await committed.commit()
    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"email": "unknown@example.com", "role": "member"},
        headers=auth(token),
    )
    assert response.status_code == (403 if caller == "member" else 404), response.text


async def test_member_creation_enforces_policy_narrowing(client, committed) -> None:
    team = await make_team(committed, rpm_limit=10)
    user = await make_user(committed)
    await committed.commit()
    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"email": user.email, "role": "member", "rpm_limit": 11},
        headers=BOOTSTRAP_AUTH,
    )
    assert response.status_code == 422, response.text
    assert await committed.get(TeamMember, (team.id, user.id)) is None

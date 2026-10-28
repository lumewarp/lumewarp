"""Member policies, last-admin protection and automatic key suspension."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from lumewarp_control_plane.authz import Authorizer, Decision
from lumewarp_control_plane.models import AuditLog, KeyLifecycle, TeamRole, member_scope_id
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
    user = await make_user(committed)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/members/{user.id}"
    created = client.put(path, json={"role": "member", "rpm_limit": 10}, headers=BOOTSTRAP_AUTH)
    assert created.status_code == 201, created.text
    assert created.json()["member_scope_id"] == str(member_scope_id(team.id, user.id))
    assert created.json()["email"] == user.email
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
    assert restored.status_code == 403, restored.text
    restored = client.patch(
        f"/api/v1/keys/{active.id}", json={"lifecycle": "active"}, headers=BOOTSTRAP_AUTH
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["suspended_reason"] is None


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_external_grants_are_read_only(client, committed, monkeypatch, method) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()

    async def allow(user_id, relation, resource):
        return Decision.ALLOW

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(allow, "external"))
    path = f"/api/v1/teams/{team.id}/members/{user.id}"
    response = client.request(
        method, path, json=None if method == "DELETE" else {"role": "member"}, headers=auth(token)
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "managed_externally"
    assert client.get(f"/api/v1/teams/{team.id}/members", headers=auth(token)).status_code == 200

"""Users, current-team identity, PATs and deactivation of key owners."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.models import AuditLog, KeyLifecycle, TeamRole
from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team


def test_user_crud(client) -> None:
    created = client.post(
        "/api/v1/users",
        json={"email": f"u-{suffix()}@example.com", "display_name": "User"},
        headers=BOOTSTRAP_AUTH,
    )
    assert created.status_code == 201, created.text
    user = created.json()
    path = f"/api/v1/users/{user['id']}"
    assert user["is_active"] is True
    assert client.get(path, headers=BOOTSTRAP_AUTH).json() == user
    updated = client.patch(
        path, json={"display_name": "Renamed", "is_active": False}, headers=BOOTSTRAP_AUTH
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Renamed"
    assert updated.json()["is_active"] is False
    assert client.delete(path, headers=BOOTSTRAP_AUTH).status_code == 204
    assert client.get(path, headers=BOOTSTRAP_AUTH).status_code == 404


async def test_current_user_and_pats(client, committed) -> None:
    boot = client.get("/api/v1/users/me", headers=BOOTSTRAP_AUTH)
    assert boot.json() == {
        "kind": "bootstrap",
        "user_id": None,
        "email": None,
        "display_name": "bootstrap",
        "is_superuser": True,
        "manages_grants": True,
        "teams": [],
    }
    assert client.get("/api/v1/users/me/tokens", headers=BOOTSTRAP_AUTH).json()["items"] == []
    assert (
        client.post(
            "/api/v1/users/me/tokens", json={"name": "bootstrap"}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 400
    )
    user, token = await user_with_token(committed)
    team = await make_team(committed)
    other = await make_team(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    await make_member(committed, other, user)
    await committed.commit()
    me = client.get("/api/v1/users/me", headers=auth(token))
    assert me.status_code == 200, me.text
    assert me.json()["kind"] == "user"
    assert me.json()["user_id"] == str(user.id)
    assert me.json()["email"] == user.email
    assert me.json()["manages_grants"] is True
    assert {r["team_id"]: r for r in me.json()["teams"]} == {
        str(team.id): {"team_id": str(team.id), "team_name": team.name, "role": "admin"},
        str(other.id): {"team_id": str(other.id), "team_name": other.name, "role": "member"},
    }
    created = client.post(
        "/api/v1/users/me/tokens",
        json={"name": "second", "expires_at": "2099-01-01T00:00:00Z"},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    pat = created.json()
    assert pat["secret"].startswith("lwp-")
    assert "token_hash" not in created.text
    listed = client.get("/api/v1/users/me/tokens", headers=auth(token)).json()["items"]
    assert pat["token"]["id"] in {r["id"] for r in listed}
    revoked = client.post(
        f"/api/v1/users/me/tokens/{pat['token']['id']}/revoke", headers=auth(token)
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert client.get("/api/v1/users/me", headers=auth(pat["secret"])).status_code == 401
    assert (
        client.post(
            f"/api/v1/users/me/tokens/{uuid.uuid4()}/revoke", headers=auth(token)
        ).status_code
        == 404
    )


async def test_external_backend_current_user_teams(client, committed, monkeypatch) -> None:
    user, token = await user_with_token(committed)
    admin_team = await make_team(committed)
    member_team = await make_team(committed)
    await make_team(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if relation is Relation.SUPERUSER:
            return Decision.DENY
        if resource.id == str(admin_team.id):
            return Decision.ALLOW
        if resource.id == str(member_team.id) and relation is Relation.MEMBER:
            return Decision.ALLOW
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    me = client.get("/api/v1/users/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["manages_grants"] is False
    assert {r["team_id"]: r["role"] for r in me.json()["teams"]} == {
        str(admin_team.id): "admin",
        str(member_team.id): "member",
    }


@pytest.mark.parametrize("manages_grants", [True, False])
def test_bootstrap_reports_backend_grant_management(client, monkeypatch, manages_grants) -> None:
    monkeypatch.setattr(client.app.state.authorizer, "manages_grants", manages_grants)
    response = client.get("/api/v1/users/me", headers=BOOTSTRAP_AUTH)
    assert response.status_code == 200
    assert response.json()["manages_grants"] is manages_grants


@pytest.mark.parametrize("failed_relation", [Relation.MEMBER, Relation.ADMIN])
async def test_current_user_team_listing_errors_are_503(
    client, committed, monkeypatch, failed_relation
) -> None:
    _, token = await user_with_token(committed)
    team = await make_team(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if relation is Relation.SUPERUSER:
            return Decision.DENY
        if resource.id == str(team.id):
            return Decision.ERROR if relation is failed_relation else Decision.ALLOW
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    response = client.get("/api/v1/users/me", headers=auth(token))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "authz_unavailable"


async def test_deactivation_suspends_non_revoked_user_keys_everywhere(client, committed) -> None:
    user, token = await user_with_token(committed)
    other_user, _ = await user_with_token(committed)
    team = await make_team(committed)
    other = await make_team(committed)
    await make_member(committed, team, user)
    await make_member(committed, other, user)
    keys = [
        await make_key(committed, team, user),
        await make_key(committed, other, user, lifecycle=KeyLifecycle.SUSPENDED),
    ]
    revoked = await make_key(
        committed, team, user, lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC)
    )
    untouched = await make_key(committed, team, other_user)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account, created_by_user_id=user.id)
    await committed.commit()
    changed = client.patch(
        f"/api/v1/users/{user.id}", json={"is_active": False}, headers=BOOTSTRAP_AUTH
    )
    assert changed.status_code == 200, changed.text
    for key in keys:
        row = client.get(f"/api/v1/keys/{key.id}", headers=BOOTSTRAP_AUTH).json()
        assert (row["lifecycle"], row["suspended_reason"]) == ("suspended", "owner_deactivated")
    for key, expected in ((revoked, "revoked"), (untouched, "active"), (sa_key, "active")):
        assert (
            client.get(f"/api/v1/keys/{key.id}", headers=BOOTSTRAP_AUTH).json()["lifecycle"]
            == expected
        )
    assert client.get("/api/v1/users/me", headers=auth(token)).status_code == 401
    audit = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.action == "key.suspend",
                AuditLog.resource_id.in_([str(k.id) for k in keys]),
            )
        )
    ).all()
    assert len(audit) == 2
    assert all(r.details["reason"] == "owner_deactivated" for r in audit)


@pytest.mark.parametrize("revoke", [False, True])
async def test_user_with_keys_cannot_be_deleted(client, committed, revoke) -> None:
    user, _ = await user_with_token(committed)
    team = await make_team(committed)
    key = await make_key(committed, team, user)
    await committed.commit()
    if revoke:
        assert (
            client.post(f"/api/v1/keys/{key.id}/revoke", headers=BOOTSTRAP_AUTH).status_code == 200
        )
    assert client.delete(f"/api/v1/users/{user.id}", headers=BOOTSTRAP_AUTH).status_code == 409


@pytest.mark.parametrize("caller", ["bootstrap", "superuser"])
async def test_user_delete_cannot_remove_last_admin_of_any_team(client, committed, caller) -> None:
    owner, token = await user_with_token(committed)
    first = await make_team(committed)
    last = await make_team(committed)
    ordinary = await make_team(committed)
    await make_member(committed, first, owner, role=TeamRole.ADMIN)
    await make_member(committed, last, owner, role=TeamRole.ADMIN)
    await make_member(committed, ordinary, owner)
    await make_member(committed, first, role=TeamRole.ADMIN)
    headers = BOOTSTRAP_AUTH
    if caller == "superuser":
        _, admin_token = await user_with_token(committed, superuser=True)
        headers = auth(admin_token)
    await committed.commit()
    path = f"/api/v1/users/{owner.id}"
    denied = client.delete(path, headers=headers)
    assert denied.status_code == 409, denied.text
    assert "last admin" in denied.json()["error"]["message"]
    assert client.get(path, headers=headers).status_code == 200
    for team in (first, last, ordinary):
        rows = client.get(f"/api/v1/teams/{team.id}/members", headers=BOOTSTRAP_AUTH).json()[
            "items"
        ]
        assert str(owner.id) in {r["user_id"] for r in rows}
    await make_member(committed, last, role=TeamRole.ADMIN)
    await committed.commit()
    assert client.delete(path, headers=headers).status_code == 204
    assert client.get(path, headers=headers).status_code == 404
    assert client.get("/api/v1/users/me", headers=auth(token)).status_code == 401
    for team in (first, last, ordinary):
        rows = client.get(f"/api/v1/teams/{team.id}/members", headers=BOOTSTRAP_AUTH).json()[
            "items"
        ]
        assert str(owner.id) not in {r["user_id"] for r in rows}

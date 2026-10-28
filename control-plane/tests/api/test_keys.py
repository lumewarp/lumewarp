"""Keys have an owner, not a policy; rotation preserves their public identity."""

import uuid

import pytest
from sqlalchemy import event

from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, TeamRole
from lumewarp_control_plane.security import hash_secret
from tests.api.helpers import BOOTSTRAP_AUTH, auth, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team, make_user


async def test_member_creates_and_manages_only_own_key(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    other, _ = await user_with_token(committed)
    await make_member(committed, team, user)
    await make_member(committed, team, other)
    other_key = await make_key(committed, team, other)
    account = await make_service_account(committed, team)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"
    created = client.post(path, json={"name": "own"}, headers=auth(token))
    assert created.status_code == 201, created.text
    body = created.json()
    key = body["key"]
    assert body["secret"].startswith("lw-")
    assert key["display_prefix"] == body["secret"][:9]
    assert (key["owner_kind"], key["user_id"], key["service_account_id"]) == (
        "user",
        str(user.id),
        None,
    )
    assert key["created_by_user_id"] == str(user.id)
    assert key["owner_active"] is True
    assert not {"secret", "key_hash", "allowed_models", "rpm_limit"} & key.keys()
    assert (
        client.post(
            path, json={"name": "impersonate", "user_id": str(other.id)}, headers=auth(token)
        ).status_code
        == 422
    )
    assert (
        client.post(path, json={"name": "policy", "rpm_limit": 1}, headers=auth(token)).status_code
        == 422
    )
    assert (
        client.post(
            path, json={"name": "sa", "service_account_id": str(account.id)}, headers=auth(token)
        ).status_code
        == 403
    )
    for method, tail, payload in (
        ("PATCH", "", {"name": "changed"}),
        ("POST", "/revoke", None),
        ("POST", "/rotate", None),
    ):
        denied = client.request(
            method, f"/api/v1/keys/{other_key.id}{tail}", json=payload, headers=auth(token)
        )
        assert denied.status_code == 403
    read = client.get(f"/api/v1/keys/{key['id']}", headers=auth(token))
    assert read.status_code == 200
    assert read.json()["owner_active"] is True
    assert "secret" not in read.text and "key_hash" not in read.text
    assert (
        client.patch(
            f"/api/v1/keys/{key['id']}",
            json={"name": "renamed", "expires_at": "2099-01-01T00:00:00Z"},
            headers=auth(token),
        ).status_code
        == 200
    )


@pytest.mark.parametrize("caller", ["bootstrap", "superuser"])
async def test_non_member_cannot_create_user_key(client, committed, caller) -> None:
    team = await make_team(committed)
    _, token = await user_with_token(committed, superuser=True)
    await committed.commit()
    response = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": "own"},
        headers=BOOTSTRAP_AUTH if caller == "bootstrap" else auth(token),
    )
    assert response.status_code == 422, response.text
    assert "not a member of this team" in response.json()["error"]["message"]


@pytest.mark.parametrize("role", ["admin", "superuser", "bootstrap"])
async def test_admin_creates_sa_keys_and_checks_team(client, committed, role) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    account = await make_service_account(committed, team)
    foreign = await make_service_account(committed, other)
    user, token = await user_with_token(committed, superuser=role == "superuser")
    if role == "admin":
        await make_member(committed, team, user, role=TeamRole.ADMIN)
    await committed.commit()
    headers = BOOTSTRAP_AUTH if role == "bootstrap" else auth(token)
    path = f"/api/v1/teams/{team.id}/keys"
    response = client.post(
        path, json={"name": "service", "service_account_id": str(account.id)}, headers=headers
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert key["owner_active"] is None
    assert (key["owner_kind"], key["service_account_id"], key["user_id"]) == (
        "service_account",
        str(account.id),
        None,
    )
    assert (
        client.patch(
            f"/api/v1/keys/{key['id']}", json={"name": "managed"}, headers=headers
        ).status_code
        == 200
    )
    for account_id in (foreign.id, uuid.uuid4()):
        assert (
            client.post(
                path,
                json={"name": "wrong team", "service_account_id": str(account_id)},
                headers=headers,
            ).status_code
            == 404
        )


@pytest.mark.parametrize("lifecycle", ["active", "suspended"])
async def test_rotate_replaces_hash_in_same_row(client, committed, lifecycle) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": "rotate"}, headers=auth(token)
    ).json()
    path = f"/api/v1/keys/{created['key']['id']}"
    if lifecycle == "suspended":
        assert (
            client.patch(path, json={"lifecycle": lifecycle}, headers=auth(token)).status_code
            == 200
        )
    rotated = client.post(f"{path}/rotate", headers=auth(token))
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["key"]["id"] == created["key"]["id"]
    assert rotated.json()["key"]["lifecycle"] == lifecycle
    assert rotated.json()["key"]["owner_active"] is True
    assert rotated.json()["secret"] != created["secret"]
    row = await committed.get(ApiKey, uuid.UUID(created["key"]["id"]))
    assert row.key_hash == hash_secret(rotated.json()["secret"])
    assert row.key_hash != hash_secret(created["secret"])


async def test_revoke_is_terminal_and_idempotent(client, committed) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, account)
    await committed.commit()
    path = f"/api/v1/keys/{key.id}"
    for _ in range(2):
        revoked = client.post(f"{path}/revoke", headers=BOOTSTRAP_AUTH)
        assert revoked.status_code == 200
        assert revoked.json()["lifecycle"] == "revoked"
        assert revoked.json()["revoked_at"] is not None
    assert client.patch(path, json={"name": "no"}, headers=BOOTSTRAP_AUTH).status_code == 409
    assert client.post(f"{path}/rotate", headers=BOOTSTRAP_AUTH).status_code == 409


async def test_team_key_filters_and_own_keys_across_teams(client, committed) -> None:
    team = await make_team(committed)
    other_team = await make_team(committed)
    owner, token = await user_with_token(committed)
    other, _ = await user_with_token(committed)
    await make_member(committed, team, owner)
    await make_member(committed, other_team, owner)
    own = await make_key(committed, team, owner)
    elsewhere = await make_key(committed, other_team, owner)
    await make_key(committed, team, other)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account, lifecycle=KeyLifecycle.SUSPENDED)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"
    for filters, expected in (
        ({"user_id": str(owner.id)}, {str(own.id)}),
        ({"owner_kind": "service_account"}, {str(sa_key.id)}),
        ({"service_account_id": str(account.id)}, {str(sa_key.id)}),
        ({"lifecycle": "suspended"}, {str(sa_key.id)}),
    ):
        listed = client.get(path, params=filters, headers=auth(token))
        assert listed.status_code == 200, listed.text
        assert {r["id"] for r in listed.json()["items"]} == expected
    own_page = client.get("/api/v1/users/me/keys?limit=1", headers=auth(token)).json()
    next_page = client.get(
        "/api/v1/users/me/keys", params={"cursor": own_page["next_cursor"]}, headers=auth(token)
    ).json()
    assert {r["id"] for r in own_page["items"] + next_page["items"]} == {
        str(own.id),
        str(elsewhere.id),
    }
    assert client.get("/api/v1/users/me/keys", headers=BOOTSTRAP_AUTH).json() == {
        "items": [],
        "next_cursor": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00"},
        {"name": ""},
        {"rpm_limit": 5},
    ],
)
async def test_key_create_validation(client, committed, body) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    await committed.commit()
    response = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": "key", "service_account_id": str(account.id), **body},
        headers=BOOTSTRAP_AUTH,
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("caller", ["admin", "superuser", "bootstrap"])
@pytest.mark.parametrize("owner_kind", ["user", "service_account"])
async def test_admin_suspension_cannot_be_undone_by_member(
    client, committed, caller, owner_kind
) -> None:
    team = await make_team(committed)
    owner, owner_token = await user_with_token(committed)
    await make_member(committed, team, owner)
    admin, admin_token = await user_with_token(committed, superuser=caller == "superuser")
    if caller == "admin":
        await make_member(committed, team, admin, role=TeamRole.ADMIN)
    key_owner = owner
    if owner_kind == "service_account":
        key_owner = await make_service_account(committed, team)
    key = await make_key(committed, team, key_owner)
    await committed.commit()
    path = f"/api/v1/keys/{key.id}"
    admin_headers = BOOTSTRAP_AUTH if caller == "bootstrap" else auth(admin_token)
    suspended = client.patch(path, json={"lifecycle": "suspended"}, headers=admin_headers)
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["suspended_reason"] == "admin"
    denied = client.patch(path, json={"lifecycle": "active"}, headers=auth(owner_token))
    assert denied.status_code == 403, denied.text
    # Repeating a suspension cannot launder an admin suspension into an owner suspension.
    repeated = client.patch(path, json={"lifecycle": "suspended"}, headers=auth(owner_token))
    assert repeated.status_code == (200 if owner_kind == "user" else 403), repeated.text
    stored = client.get(path, headers=admin_headers).json()
    assert (stored["lifecycle"], stored["suspended_reason"]) == ("suspended", "admin")
    assert (
        client.patch(path, json={"lifecycle": "active"}, headers=auth(owner_token)).status_code
        == 403
    )
    restored = client.patch(path, json={"lifecycle": "active"}, headers=admin_headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["lifecycle"] == "active"
    assert restored.json()["suspended_reason"] is None


async def test_member_can_reactivate_self_suspended_key(client, committed) -> None:
    team = await make_team(committed)
    owner, token = await user_with_token(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner)
    await committed.commit()
    path = f"/api/v1/keys/{key.id}"
    suspended = client.patch(path, json={"lifecycle": "suspended"}, headers=auth(token))
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["suspended_reason"] == "owner"
    restored = client.patch(path, json={"lifecycle": "active"}, headers=auth(token))
    assert restored.status_code == 200, restored.text
    assert restored.json()["lifecycle"] == "active"
    assert restored.json()["suspended_reason"] is None


@pytest.mark.parametrize("external", [True, False])
async def test_returned_member_can_resume_own_owner_removed_key(
    client, committed, monkeypatch, external
):
    team = await make_team(committed)
    owner, token = await user_with_token(committed)
    key = await make_key(
        committed,
        team,
        owner,
        lifecycle=KeyLifecycle.SUSPENDED,
        suspended_reason="owner_removed",
    )
    if external:

        async def check(user_id, relation, resource):
            return (
                Decision.ALLOW
                if user_id == str(owner.id)
                and relation is Relation.MEMBER
                and resource.id == str(team.id)
                else Decision.DENY
            )

        monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    else:
        await make_member(committed, team, owner)
    await committed.commit()
    restored = client.patch(
        f"/api/v1/keys/{key.id}", json={"lifecycle": "active"}, headers=auth(token)
    )
    assert restored.status_code == 200, restored.text
    assert (restored.json()["lifecycle"], restored.json()["suspended_reason"]) == ("active", None)


@pytest.mark.parametrize("role", ["admin", "superuser"])
async def test_privileged_owner_suspends_as_admin(client, committed, role) -> None:
    team = await make_team(committed)
    owner, token = await user_with_token(committed, superuser=role == "superuser")
    await make_member(committed, team, owner, role=TeamRole(role if role == "admin" else "member"))
    key = await make_key(committed, team, owner)
    await committed.commit()
    suspended = client.patch(
        f"/api/v1/keys/{key.id}", json={"lifecycle": "suspended"}, headers=auth(token)
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["suspended_reason"] == "admin"


@pytest.mark.parametrize("operation", ["create", "reactivate"])
@pytest.mark.parametrize("decision", [Decision.ERROR, Decision.DENY])
@pytest.mark.parametrize("reason", ["owner", "owner_removed"])
async def test_external_membership_error_is_unavailable_not_a_denial(
    client, committed, monkeypatch, operation, decision, reason
) -> None:
    team = await make_team(committed)
    owner, token = await user_with_token(committed, superuser=True)
    key = await make_key(
        committed, team, owner, lifecycle=KeyLifecycle.SUSPENDED, suspended_reason=reason
    )
    await committed.commit()
    membership_checks = []

    async def check(user_id, relation, resource):
        if relation is Relation.MEMBER:
            membership_checks.append((user_id, resource.id))
            return decision
        return Decision.ALLOW

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    if operation == "create":
        response = client.post(
            f"/api/v1/teams/{team.id}/keys", json={"name": "new"}, headers=auth(token)
        )
    else:
        response = client.patch(
            f"/api/v1/keys/{key.id}", json={"lifecycle": "active"}, headers=auth(token)
        )
    expected = 503 if decision is Decision.ERROR else (422 if operation == "create" else 409)
    assert response.status_code == expected, response.text
    if decision is Decision.ERROR:
        assert response.json()["error"]["code"] == "authz_unavailable"
    assert membership_checks == [(str(owner.id), str(team.id))]
    stored = client.get(f"/api/v1/keys/{key.id}", headers=BOOTSTRAP_AUTH).json()
    assert (stored["lifecycle"], stored["suspended_reason"]) == ("suspended", reason)
    keys = client.get(f"/api/v1/teams/{team.id}/keys", headers=BOOTSTRAP_AUTH).json()["items"]
    assert [row["id"] for row in keys] == [str(key.id)]


@pytest.mark.parametrize("caller", ["admin", "superuser", "bootstrap"])
async def test_inactive_owner_key_cannot_be_reactivated(client, committed, caller) -> None:
    team = await make_team(committed)
    owner, _ = await user_with_token(committed)
    await make_member(committed, team, owner)
    admin, token = await user_with_token(committed, superuser=caller == "superuser")
    if caller == "admin":
        await make_member(committed, team, admin, role=TeamRole.ADMIN)
    key = await make_key(committed, team, owner, name="unchanged")
    await committed.commit()
    assert (
        client.patch(
            f"/api/v1/users/{owner.id}", json={"is_active": False}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 200
    )
    headers = BOOTSTRAP_AUTH if caller == "bootstrap" else auth(token)
    path = f"/api/v1/keys/{key.id}"
    denied = client.patch(path, json={"lifecycle": "active", "name": "changed"}, headers=headers)
    assert denied.status_code == 409, denied.text
    assert "not active" in denied.json()["error"]["message"]
    stored = client.get(path, headers=BOOTSTRAP_AUTH).json()
    assert stored["name"] == "unchanged"
    assert (stored["lifecycle"], stored["suspended_reason"]) == ("suspended", "owner_deactivated")
    assert stored["owner_active"] is False
    assert (
        client.patch(
            f"/api/v1/users/{owner.id}", json={"is_active": True}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 200
    )
    restored = client.patch(path, json={"lifecycle": "active"}, headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["suspended_reason"] is None
    assert restored.json()["owner_active"] is True


async def test_key_list_owner_status_is_joined_and_paginated(client, committed) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    active = await make_user(committed)
    inactive = await make_user(committed, is_active=False)
    account = await make_service_account(committed, team)
    expected = {}
    for owner, status in ((active, True), (inactive, False), (account, None)):
        key = await make_key(committed, team, owner)
        expected[str(key.id)] = status
    await committed.commit()
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        # Bootstrap avoids credential and authorization queries, leaving only the
        # team lookup and one joined page query, independent of page size.
        path = f"/api/v1/teams/{team.id}/keys"
        for limit in (1, 50):
            statements.clear()
            response = client.get(path, params={"limit": limit}, headers=BOOTSTRAP_AUTH)
            assert response.status_code == 200, response.text
            assert len(statements) == 2
            assert "JOIN users" in statements[-1]
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert {row["id"]: row["owner_active"] for row in response.json()["items"]} == expected
    page = client.get(path, params={"limit": 1}, headers=auth(token)).json()
    items = list(page["items"])
    while page["next_cursor"] is not None:
        page = client.get(
            path,
            params={"limit": 1, "cursor": page["next_cursor"]},
            headers=auth(token),
        ).json()
        items.extend(page["items"])
    assert [row["id"] for row in items] == sorted(expected)
    assert {row["id"]: row["owner_active"] for row in items} == expected
    filtered = client.get(path, params={"user_id": str(inactive.id)}, headers=auth(token))
    assert filtered.status_code == 200, filtered.text
    assert [row["owner_active"] for row in filtered.json()["items"]] == [False]
    assert client.get(path, params={"cursor": "invalid"}, headers=auth(token)).status_code == 422


async def test_own_key_list_includes_active_owner_status(client, committed) -> None:
    team = await make_team(committed)
    owner, token = await user_with_token(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner)
    await committed.commit()
    response = client.get("/api/v1/users/me/keys", headers=auth(token))
    assert response.status_code == 200, response.text
    assert [(row["id"], row["owner_active"]) for row in response.json()["items"]] == [
        (str(key.id), True)
    ]

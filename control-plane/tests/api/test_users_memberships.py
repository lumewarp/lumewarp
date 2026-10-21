"""Users, personal access tokens and membership grants."""

from __future__ import annotations

import uuid

from tests.api.helpers import (
    BOOTSTRAP_AUTH,
    auth,
    grant,
    org_tree,
    suffix,
    user_with_token,
)


def _user(client, **body):
    body = {"email": f"u-{suffix()}@example.com", "display_name": "U", **body}
    return client.post("/api/v1/users", json=body, headers=BOOTSTRAP_AUTH)


def test_user_crud_and_me(client) -> None:
    resp = _user(client)
    assert resp.status_code == 201, resp.text
    user = resp.json()
    assert user["email"].endswith("@example.com")
    assert user["is_active"] is True

    resp = client.get(f"/api/v1/users/{user['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200
    resp = client.patch(
        f"/api/v1/users/{user['id']}",
        json={"display_name": "Renamed", "is_active": False},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["is_active"] is False
    resp = client.delete(f"/api/v1/users/{user['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/users/{uuid.uuid4()}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 404


async def test_current_user_and_pats(client, committed) -> None:
    boot = client.get("/api/v1/users/me", headers=BOOTSTRAP_AUTH)
    assert boot.status_code == 200
    assert boot.json() == {
        "kind": "superuser",
        "user_id": None,
        "email": None,
        "display_name": "bootstrap",
        "is_superuser": True,
        "grants": [],
    }
    assert client.get("/api/v1/users/me/tokens", headers=BOOTSTRAP_AUTH).json()["items"] == []
    resp = client.post(
        "/api/v1/users/me/tokens",
        json={"name": "bootstrap token"},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 400

    user, secret = await user_with_token(committed)
    await committed.commit()
    me = client.get("/api/v1/users/me", headers=auth(secret))
    assert me.status_code == 200
    assert me.json()["user_id"] == str(user.id)
    assert me.json()["email"] == user.email

    created = client.post(
        "/api/v1/users/me/tokens",
        json={"name": "second", "expires_at": "2099-01-01T00:00:00Z"},
        headers=auth(secret),
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert created.json()["secret"].startswith("lwp-")
    assert "hash" not in token

    listed = client.get("/api/v1/users/me/tokens", headers=auth(secret))
    assert listed.status_code == 200
    assert any(item["id"] == token["id"] for item in listed.json()["items"])

    revoked = client.post(f"/api/v1/users/me/tokens/{token['id']}/revoke", headers=auth(secret))
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    # The revoked token can no longer authenticate.
    assert client.get("/api/v1/users/me", headers=auth(created.json()["secret"])).status_code == 401
    # Creating a token for someone else's id is not possible; the path is self-only.
    assert (
        client.post(
            f"/api/v1/users/me/tokens/{uuid.uuid4()}/revoke", headers=auth(secret)
        ).status_code
        == 404
    )


async def test_membership_lifecycle(client, committed) -> None:
    org, team, _ = await org_tree(committed, prefix="member")
    other_org, other_team, _ = await org_tree(committed, prefix="member-other")
    user, secret = await user_with_token(committed)
    plain, plain_secret = await user_with_token(committed)
    await grant(committed, plain, org, team=team, role="member")
    await committed.commit()

    created = client.post(
        "/api/v1/memberships",
        json={"user_id": str(user.id), "org_id": str(org.id), "role": "admin"},
        headers=BOOTSTRAP_AUTH,
    )
    assert created.status_code == 201, created.text
    membership = created.json()
    assert membership["team_id"] is None
    # Team role without a team is invalid.
    bad = client.post(
        "/api/v1/memberships",
        json={"user_id": str(user.id), "org_id": str(org.id), "role": "viewer"},
        headers=BOOTSTRAP_AUTH,
    )
    assert bad.status_code == 422

    resp = client.post(
        "/api/v1/memberships",
        json={
            "user_id": str(user.id),
            "org_id": str(org.id),
            "team_id": str(team.id),
            "role": "viewer",
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 201
    team_membership = resp.json()
    # Mismatched hierarchy is a 404.
    mismatch = client.post(
        "/api/v1/memberships",
        json={
            "user_id": str(user.id),
            "org_id": str(other_org.id),
            "team_id": str(team.id),
            "role": "viewer",
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert mismatch.status_code == 404

    listed = client.get(f"/api/v1/memberships?org_id={org.id}", headers=BOOTSTRAP_AUTH)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 3

    # A plain team member cannot list or manage org memberships.
    denied = client.get(f"/api/v1/memberships?org_id={org.id}", headers=auth(plain_secret))
    assert denied.status_code == 403
    denied = client.delete(f"/api/v1/memberships/{membership['id']}", headers=auth(plain_secret))
    assert denied.status_code == 403

    updated = client.patch(
        f"/api/v1/memberships/{membership['id']}",
        json={"role": "admin"},
        headers=BOOTSTRAP_AUTH,
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "admin"
    assert (
        client.delete(f"/api/v1/memberships/{membership['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(
            f"/api/v1/memberships/{team_membership['id']}", headers=BOOTSTRAP_AUTH
        ).status_code
        == 204
    )

    # A non-superuser cannot list all memberships without a scope.
    assert client.get("/api/v1/memberships", headers=auth(plain_secret)).status_code == 422


async def test_membership_manage_matrix(client, committed) -> None:
    org, team, _ = await org_tree(committed, prefix="member-manage")
    target, _ = await user_with_token(committed)
    team_admin, team_admin_secret = await user_with_token(committed)
    org_admin, org_admin_secret = await user_with_token(committed)
    org_owner, org_owner_secret = await user_with_token(committed)
    await grant(committed, team_admin, org, team=team, role="admin")
    await grant(committed, org_admin, org, role="admin")
    await grant(committed, org_owner, org, role="owner")
    await committed.commit()

    body = {
        "user_id": str(target.id),
        "org_id": str(org.id),
        "team_id": str(team.id),
        "role": "member",
    }
    assert (
        client.post("/api/v1/memberships", json=body, headers=auth(team_admin_secret)).status_code
        == 403
    )
    created = client.post("/api/v1/memberships", json=body, headers=auth(org_admin_secret))
    assert created.status_code == 201, created.text
    membership_id = created.json()["id"]

    listing = client.get(f"/api/v1/memberships?team_id={team.id}", headers=auth(team_admin_secret))
    assert listing.status_code == 403
    listing = client.get(f"/api/v1/memberships?team_id={team.id}", headers=auth(org_admin_secret))
    assert listing.status_code == 200

    owner_body = {"user_id": str(target.id), "org_id": str(org.id), "role": "owner"}
    assert (
        client.post(
            "/api/v1/memberships", json=owner_body, headers=auth(org_admin_secret)
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/memberships", json=owner_body, headers=auth(org_owner_secret)
        ).status_code
        == 201
    )
    assert (
        client.delete(
            f"/api/v1/memberships/{membership_id}", headers=auth(team_admin_secret)
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/memberships/{membership_id}", headers=auth(org_admin_secret)
        ).status_code
        == 204
    )

"""Admin-issued PATs are installation-superuser-only, one-time secrets and audited."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from lumewarp_control_plane.models import ActorType, AuditLog, PersonalAccessToken
from lumewarp_control_plane.security import hash_secret
from tests.api.helpers import BOOTSTRAP_AUTH, auth, grant, org_tree, user_with_token


@pytest.mark.parametrize("issuer", ["bootstrap", "superuser"])
async def test_superuser_issues_pat_for_another_user(client, committed, issuer) -> None:
    target = client.post(
        "/api/v1/users",
        headers=BOOTSTRAP_AUTH,
        json={"email": f"{uuid.uuid4()}@example.com", "display_name": "New user"},
    ).json()
    actor = None
    headers = BOOTSTRAP_AUTH
    if issuer == "superuser":
        actor, secret = await user_with_token(committed, superuser=True)
        await committed.commit()
        headers = auth(secret)

    issued = client.post(
        f"/api/v1/users/{target['id']}/tokens",
        headers=headers,
        json={"name": "initial access", "expires_at": "2099-01-01T00:00:00Z"},
    )
    assert issued.status_code == 201, issued.text
    assert issued.headers["cache-control"] == "no-store"
    token = issued.json()["token"]
    secret = issued.json()["secret"]
    assert secret.startswith("lwp-")
    assert "secret" not in token and "token_hash" not in token
    assert token["expires_at"].startswith("2099-01-01")
    me = client.get("/api/v1/users/me", headers=auth(secret))
    assert me.status_code == 200
    assert me.json()["user_id"] == target["id"]
    assert me.json()["is_superuser"] is False

    listed = client.get("/api/v1/users/me/tokens", headers=auth(secret))
    assert listed.status_code == 200
    assert secret not in listed.text and "token_hash" not in listed.text
    assert [r["id"] for r in listed.json()["items"]] == [token["id"]]
    # Neither route can retrieve the secret after issuance.
    assert client.get(f"/api/v1/users/{target['id']}/tokens", headers=headers).status_code in (
        404,
        405,
    )

    row = await committed.get(PersonalAccessToken, uuid.UUID(token["id"]))
    assert str(row.user_id) == target["id"]
    assert row.token_hash == hash_secret(secret)
    audit = await committed.scalar(
        select(AuditLog).where(AuditLog.resource_id == token["id"], AuditLog.action == "pat.create")
    )
    assert audit.actor_type == (ActorType.USER if actor else ActorType.SUPERUSER_TOKEN)
    assert audit.actor_id == (str(actor.id) if actor else None)
    assert audit.details == {"name": "initial access", "user_id": target["id"]}
    assert secret not in json.dumps(audit.details)
    assert row.token_hash.hex() not in json.dumps(audit.details)
    await committed.rollback()

    revoked = client.post(f"/api/v1/users/me/tokens/{token['id']}/revoke", headers=auth(secret))
    assert revoked.status_code == 200
    assert client.get("/api/v1/users/me", headers=auth(secret)).status_code == 401


@pytest.mark.parametrize("role", ["owner", "org_admin", "team_admin", "member", "viewer", "none"])
async def test_non_superusers_cannot_issue_for_self_or_others(client, committed, role) -> None:
    org, team, _ = await org_tree(committed)
    user, secret = await user_with_token(committed)
    target, _ = await user_with_token(committed)
    if role in ("owner", "org_admin"):
        await grant(committed, user, org, role="owner" if role == "owner" else "admin")
    elif role != "none":
        await grant(committed, user, org, team=team, role="admin" if role == "team_admin" else role)
    await committed.commit()
    before = await committed.scalar(select(func.count()).select_from(PersonalAccessToken))
    for user_id in (target.id, user.id, uuid.uuid4()):
        denied = client.post(
            f"/api/v1/users/{user_id}/tokens", headers=auth(secret), json={"name": "denied"}
        )
        assert denied.status_code == 403, denied.text
    assert await committed.scalar(select(func.count()).select_from(PersonalAccessToken)) == before


async def test_admin_token_target_and_expiry_validation(client, committed) -> None:
    inactive, _ = await user_with_token(committed, active=False)
    active, _ = await user_with_token(committed)
    await committed.commit()
    for user_id, expected in ((uuid.uuid4(), 404), (inactive.id, 409)):
        assert (
            client.post(
                f"/api/v1/users/{user_id}/tokens", headers=BOOTSTRAP_AUTH, json={"name": "test"}
            ).status_code
            == expected
        )
    path = f"/api/v1/users/{active.id}/tokens"
    for expires_at in ("2000-01-01T00:00:00Z", "2099-01-01T00:00:00"):
        assert (
            client.post(
                path, headers=BOOTSTRAP_AUTH, json={"name": "test", "expires_at": expires_at}
            ).status_code
            == 422
        )
    assert client.post(path, json={"name": "test"}).status_code == 401
    assert client.post(path, headers=auth("bogus"), json={"name": "test"}).status_code == 401

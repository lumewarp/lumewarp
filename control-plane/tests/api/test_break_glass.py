"""Emergency sessions, IAM claims and bearer-token authorization boundaries."""

from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select

from lumewarp_control_plane.auth import break_glass, sessions
from lumewarp_control_plane.authz import Authorizer, Decision
from lumewarp_control_plane.models import (
    ActorType,
    AuditLog,
    InstallationAdmin,
    PersonalAccessToken,
    UserSession,
)
from lumewarp_control_plane.security import SecretKind, generate_secret, hash_secret
from tests.api.helpers import auth, suffix, user_with_token
from tests.api.test_session import idp as idp


def sign_in(client, password="test-break-glass-password"):
    return client.post("/api/v1/session/break-glass", json={"password": password})


def test_disabled_break_glass(client):
    client.app.state.settings.break_glass_password = None
    client.app.state.settings.break_glass_password_hash = None
    assert sign_in(client).status_code == 404
    assert client.get("/api/v1/session").json()["break_glass_enabled"] is False


async def test_failures_rate_limit_audit_and_metrics(client, committed):
    for _ in range(5):
        assert sign_in(client, "wrong").status_code == 401
    assert sign_in(client).status_code == 429
    rows = (
        await committed.scalars(
            select(AuditLog)
            .where(AuditLog.action == "session.break_glass.login")
            .order_by(AuditLog.id)
        )
    ).all()
    # The API database is shared; inspect the attempts made by this app via metrics
    # and the last six audit rows rather than assuming an empty audit table.
    assert [row.details["result"] for row in rows[-6:]] == ["failure"] * 5 + ["rate_limited"]
    assert all(row.actor_type is ActorType.BREAK_GLASS for row in rows[-6:])
    assert "wrong" not in str([row.details for row in rows[-6:]])
    registry = client.app.state.metrics.registry
    assert (
        registry.get_sample_value("lumewarp_cp_break_glass_logins_total", {"result": "failure"})
        == 5
    )
    assert (
        registry.get_sample_value(
            "lumewarp_cp_break_glass_logins_total", {"result": "rate_limited"}
        )
        == 1
    )


def test_global_backoff_delays_but_never_rejects_other_identities(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(break_glass.time, "monotonic", lambda: now[0])
    limiter = break_glass.LoginLimiter()
    for index in range(20):
        assert limiter.reserve(str(index))
    assert limiter.reserve("new-ip")
    assert 0 < limiter.delay() <= break_glass.MAX_GLOBAL_DELAY_SECONDS
    assert limiter.reserve("another-ip")
    now[0] += 60
    assert limiter.reserve("another-ip")
    for _ in range(4):
        assert limiter.reserve("one-ip")
    assert limiter.reserve("one-ip")
    assert not limiter.reserve("one-ip")


async def test_success_is_hashed_cookie_session_with_csrf_and_no_user(
    client, committed, monkeypatch
):
    async def unavailable(*args):
        raise RuntimeError("IAM is down")

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(unavailable, "external"))
    response = sign_in(client)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=3600" in cookie
    info = client.get("/api/v1/session").json()
    assert info["is_break_glass"] and info["authenticated"] and "user" not in info
    secret = client.cookies.get(sessions.COOKIE_NAME)
    row = await committed.scalar(
        select(UserSession).where(UserSession.token_hash == hash_secret(secret))
    )
    assert row.user_id is None and row.kind == "break_glass"
    assert row.csrf_hash == hash_secret(info["csrf_token"])
    assert 3500 < (row.expires_at - datetime.now(UTC)).total_seconds() <= 3600
    assert client.post("/api/v1/teams", json={"name": "no-csrf"}).status_code == 403
    denied_audit = await committed.scalar(
        select(AuditLog)
        .where(AuditLog.action == "session.break_glass.request")
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    assert denied_audit.actor_type is ActorType.BREAK_GLASS
    assert denied_audit.details == {"method": "POST", "path": "/api/v1/teams", "status": 403}
    headers = {"X-Lumewarp-CSRF": info["csrf_token"]}
    org = client.post("/api/v1/orgs", json={"name": "emergency-org-" + suffix()}, headers=headers)
    assert org.status_code == 201, org.text
    created = client.post(
        "/api/v1/teams",
        json={"name": "emergency-" + suffix(), "org_id": org.json()["id"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert (
        client.post(
            f"/api/v1/teams/{created.json()['id']}/keys", json={"name": "no-owner"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/users/me/tokens", json={"name": "no-owner"}, headers=headers
        ).status_code
        == 400
    )
    audit = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "team.create", AuditLog.resource_id == created.json()["id"]
        )
    )
    assert audit.actor_type is ActorType.BREAK_GLASS and audit.actor_id is None
    assert (
        client.app.state.metrics.registry.get_sample_value(
            "lumewarp_cp_break_glass_logins_total", {"result": "success"}
        )
        == 1
    )
    assert client.post("/api/v1/session/logout", headers=headers).status_code == 204
    client.cookies.set(sessions.COOKIE_NAME, secret)
    assert client.get("/api/v1/users").status_code == 401


def test_argon2id_hash_takes_precedence_over_plaintext(client):
    settings = client.app.state.settings
    settings.break_glass_password_hash = SecretStr(
        break_glass.PASSWORD_HASHER.hash("hashed-password")
    )
    assert sign_in(client).status_code == 401
    assert sign_in(client, "hashed-password").status_code == 204


async def test_installation_admin_pat_and_revocation_on_the_next_request(client, committed):
    user, session = await user_with_token(committed, installation_admin=True)
    pat = generate_secret(SecretKind.PAT)
    committed.add(
        PersonalAccessToken(
            user_id=user.id,
            name="installation-admin",
            token_hash=pat.hash,
            display_prefix=pat.display_prefix,
        )
    )
    await committed.commit()
    for headers in (auth(session), auth(pat.secret)):
        assert client.get("/api/v1/users", headers=headers).status_code == 200
        me = client.get("/api/v1/users/me", headers=headers).json()
        assert (me["is_admin"], me["installation_role"]) == (True, "admin")
    # The role is read per request; revoking it applies to the next call of either credential.
    row = await committed.get(InstallationAdmin, user.id)
    await committed.delete(row)
    await committed.commit()
    for headers in (auth(session), auth(pat.secret)):
        assert client.get("/api/v1/users", headers=headers).status_code == 403
        me = client.get("/api/v1/users/me", headers=headers).json()
        assert (me["is_admin"], me["installation_role"]) == (False, None)


async def test_installation_role_never_asks_the_backend(client, committed, monkeypatch):
    _, token = await user_with_token(committed)
    _, admin_token = await user_with_token(committed, installation_admin=True)
    await committed.commit()
    calls = []

    async def check(uid, relation, resource):
        calls.append((uid, relation, resource.type, resource.id))
        return Decision.ERROR

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    # A backend outage can neither grant nor take away an installation role.
    assert client.get("/api/v1/users", headers=auth(token)).status_code == 403
    assert client.get("/api/v1/users", headers=auth(admin_token)).status_code == 200
    assert calls == []


async def test_oidc_claim_does_not_grant_an_installation_role(client, idp):
    idp.claims["groups"] = ["lumewarp-admins"]
    idp.userinfo["groups"] = ["lumewarp-admins"]
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    info = client.get("/api/v1/session").json()
    assert info["user"]["is_admin"] is False
    assert info["user"].get("installation_role") is None
    assert not any(request.url.path.endswith("/userinfo") for request in idp.requests)

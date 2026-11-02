"""OIDC browser flow and cookie/bearer authentication against a migrated Postgres."""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.oidc.core import CodeIDToken
from joserfc.jwk import RSAKey
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, update
from starlette.requests import Request

from lumewarp_control_plane import plugins
from lumewarp_control_plane.api import session as session_api
from lumewarp_control_plane.auth import oidc, sessions
from lumewarp_control_plane.authz import Authorizer, Decision, Relation, Resource
from lumewarp_control_plane.models import (
    OIDCLoginState,
    TeamMember,
    User,
    UserIdentity,
    UserSession,
)
from lumewarp_control_plane.security import hash_secret
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import ADMIN_AUTH, auth, user_with_token
from tests.auth.idp import ISSUER, FakeIdP


@pytest.fixture
def idp(client, monkeypatch):
    settings = client.app.state.settings
    settings.oidc_issuer = ISSUER
    settings.oidc_client_id = "lumewarp"
    settings.oidc_client_secret = SecretStr("client-secret")
    settings.secret_key = SecretStr("test-login-state-secret-key-0123456789abcdef")
    settings.session_cookie_secure = False
    provider = FakeIdP()
    request = Request({"type": "http", "app": client.app})
    remote = oidc.client(request)
    remote.client_kwargs["transport"] = httpx.MockTransport(provider.handle)
    monkeypatch.setattr(client.app.state, "auth_plugins", [], raising=False)
    return provider


def test_disabled_and_anonymous(client):
    response = client.get("/api/v1/session")
    assert response.json() == {
        "oidc_enabled": False,
        "break_glass_enabled": True,
        "authenticated": False,
        "is_break_glass": False,
        "authz_unavailable": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/session/oidc/login").status_code == 404
    assert client.get("/api/v1/session/oidc/callback").status_code == 404


def test_sso_refuses_to_start_without_a_secret_key():
    with pytest.raises(ValidationError, match="LUMEWARP_SECRET_KEY"):
        Settings(oidc_issuer=ISSUER, oidc_client_id="lumewarp", secret_key=None)
    Settings(
        oidc_issuer=ISSUER,
        oidc_client_id="lumewarp",
        secret_key=SecretStr("test-login-state-secret-key-0123456789abcdef"),
    )


def test_missing_secret_key_disables_the_sso_endpoints(client, idp):
    client.app.state.settings.secret_key = None
    login = client.get("/api/v1/session/oidc/login")
    assert login.status_code == 503
    assert login.json()["error"]["code"] == "sso_unconfigured"
    assert client.get("/api/v1/session/oidc/callback").status_code == 503
    assert idp.requests == []


async def test_login_state_secrets_are_not_stored(client, idp, committed):
    idp.login(client)
    row = await committed.scalar(select(OIDCLoginState))
    assert row is not None
    assert {column.name for column in OIDCLoginState.__table__.columns} == {
        "id",
        "state_hash",
        "return_path",
        "redirect_uri",
        "expires_at",
    }
    assert not hasattr(row, "nonce") and not hasattr(row, "code_verifier")


def test_session_openapi_contract(client):
    paths = client.app.openapi()["paths"]
    for path, method, operation_id in [
        ("/api/v1/session", "get", "getSession"),
        ("/api/v1/session/oidc/login", "get", "startOidcLogin"),
        ("/api/v1/session/oidc/callback", "get", "completeOidcLogin"),
        ("/api/v1/session/logout", "post", "logoutSession"),
    ]:
        assert paths[path][method]["operationId"] == operation_id


def test_login_parameters_and_cached_discovery(client, idp):
    client.app.state.settings.public_url = "https://admin.example"
    response = idp.login(client, "/teams?tab=mine")
    params = idp.authorization
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["lumewarp"]
    assert set(params["scope"][0].split()) == {"openid", "email", "profile"}
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == ["https://admin.example/api/v1/session/oidc/callback"]
    assert len(params["state"][0]) >= 43
    assert len(params["nonce"][0]) >= 43
    assert "code_verifier" not in params
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/v1/session/oidc" in cookie and "Max-Age=600" in cookie
    sealed = client.cookies.get(oidc.STATE_COOKIE)
    # nonce, PKCE verifier and state travel encrypted; only the state echo is in the URL.
    assert sealed.startswith(oidc.STATE_COOKIE_VERSION)
    for secret_value in (params["state"][0], params["nonce"][0]):
        assert secret_value not in sealed
    idp.login(client)
    assert sum(r.url.path.endswith("openid-configuration") for r in idp.requests) == 1


async def test_jit_cookie_auth_csrf_and_logout(client, idp, committed):
    idp.claims.update({"is_admin": True, "groups": ["admin"], "roles": ["group_admin"]})
    idp.login(client, "/teams")
    response = idp.callback(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/teams"
    cookie = response.headers.get_list("set-cookie")[-1]
    assert "lumewarp_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=43200" in cookie and "Secure" not in cookie
    info = client.get("/api/v1/session").json()
    assert info["authenticated"] is True
    assert info["oidc_enabled"] is True
    assert info["user"]["email"] == idp.claims["email"]
    assert info["user"]["is_admin"] is False
    assert client.get("/api/v1/users/me").status_code == 200
    assert client.get("/api/v1/users", headers={}).status_code == 403
    assert (
        await committed.scalar(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.user_id == uuid.UUID(info["user"]["id"]))
        )
        == 0
    )
    secret = client.cookies.get(sessions.COOKIE_NAME)
    credential = await committed.scalar(
        select(UserSession).where(UserSession.token_hash == hash_secret(secret))
    )
    assert credential.csrf_hash == hash_secret(info["csrf_token"])
    assert credential.token_hash != secret.encode()
    token_request = next(r for r in idp.requests if r.url.path.endswith("/token"))
    assert token_request.headers["authorization"].startswith("Basic ")
    assert (
        await committed.scalar(
            select(OIDCLoginState).where(
                OIDCLoginState.state_hash == hash_secret(idp.authorization["state"][0])
            )
        )
        is None
    )
    for headers in ({}, {"X-Lumewarp-CSRF": "wrong"}):
        denied = client.post("/api/v1/users/me/tokens", json={"name": "test"}, headers=headers)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "csrf_failed"
    csrf = {"X-Lumewarp-CSRF": info["csrf_token"]}
    created = client.post("/api/v1/users/me/tokens", json={"name": "test"}, headers=csrf)
    assert created.status_code == 201, created.text
    # PAT wins over the cookie, including on writes with no CSRF header.
    bearer = auth(created.json()["secret"])
    assert "csrf_token" not in client.get("/api/v1/session", headers=bearer).json()
    assert (
        client.post("/api/v1/users/me/tokens", json={"name": "bearer"}, headers=bearer).status_code
        == 201
    )
    assert client.post("/api/v1/session/logout", headers=bearer).status_code == 204
    assert client.get("/api/v1/session").json()["authenticated"] is True
    assert client.post("/api/v1/session/logout").status_code == 403
    logout = client.post("/api/v1/session/logout", headers=csrf)
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert client.get("/api/v1/session").json()["authenticated"] is False
    client.cookies.set(sessions.COOKIE_NAME, secret)
    assert client.get("/api/v1/users/me").status_code == 401
    await committed.refresh(credential)
    assert credential.revoked_at is not None


@pytest.mark.parametrize("verified", [True, False])
async def test_link_existing_email(client, idp, committed, verified):
    user, _ = await user_with_token(committed, email=idp.claims["email"].upper())
    await committed.commit()
    idp.claims["email_verified"] = verified
    idp.login(client)
    response = idp.callback(client)
    if verified:
        assert response.headers["location"] == "/"
        info = client.get("/api/v1/session").json()
        assert info["user"]["id"] == str(user.id)
        assert info["user"]["is_admin"] is False
    else:
        assert response.headers["location"] == "/login?error=access_denied"
        assert client.get("/api/v1/session").json()["authenticated"] is False


@pytest.mark.parametrize("verified", [False, None, "true", 1])
async def test_unverified_jit_cannot_preclaim_email(client, idp, committed, verified):
    idp.claims["email_verified"] = verified
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/login?error=access_denied"
    assert await committed.scalar(select(User).where(User.email == idp.claims["email"])) is None


@pytest.mark.parametrize("existing", ["group_admin", "same_issuer", "other_issuer"])
async def test_email_link_cannot_take_over_privileged_or_linked_user(
    client, idp, committed, existing
):
    user, _ = await user_with_token(committed, email=idp.claims["email"])
    if existing == "group_admin":
        idp.claims["groups"] = ["lumewarp-admins"]
    else:
        committed.add(
            UserIdentity(
                user_id=user.id,
                issuer=ISSUER if existing == "same_issuer" else "https://other.example",
                subject="existing-subject",
            )
        )
    await committed.commit()
    idp.login(client)
    response = idp.callback(client)
    expected = "/" if existing == "other_issuer" else "/login?error=access_denied"
    assert response.headers["location"] == expected
    linked = (
        await committed.scalars(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id, UserIdentity.issuer == ISSUER
            )
        )
    ).all()
    if existing == "same_issuer":
        assert [identity.subject for identity in linked] == ["existing-subject"]
    elif existing == "group_admin":
        assert linked == []


async def test_existing_identity_does_not_relink_email(client, idp, committed):
    user, _ = await user_with_token(committed)
    committed.add(
        UserIdentity(user_id=user.id, issuer=ISSUER, subject=idp.claims["sub"], email=user.email)
    )
    await committed.commit()
    idp.claims["email_verified"] = False
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    assert client.get("/api/v1/session").json()["user"]["id"] == str(user.id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "wrong"},
        {"aud": "other"},
        {"exp": 1},
        {"iss": "https://other.example"},
        {"nonce": None},
        {"sub": ""},
        {"aud": ["other"], "azp": "lumewarp"},
        {"aud": ["lumewarp", "other"], "azp": "other"},
    ],
)
def test_invalid_id_token(client, idp, overrides):
    idp.overrides = overrides
    idp.login(client)
    response = idp.callback(client)
    assert response.headers["location"] == "/login?error=oidc_error"
    assert client.get("/api/v1/session").json()["authenticated"] is False


@pytest.mark.parametrize(
    "case", ["bad", "missing_cookie", "replay", "expired", "tampered", "other_key", "other_login"]
)
async def test_invalid_state(client, idp, committed, case):
    idp.login(client)
    state = idp.authorization["state"][0]
    sealed = client.cookies.get(oidc.STATE_COOKIE)
    assert sealed is not None
    if case == "bad":
        result = idp.callback(client, state="wrong")
    elif case == "missing_cookie":
        client.cookies.clear()
        result = idp.callback(client)
    elif case == "tampered":
        index = len(oidc.STATE_COOKIE_VERSION) + 20
        replacement = "A" if sealed[index] != "A" else "B"
        client.cookies.set(
            oidc.STATE_COOKIE,
            sealed[:index] + replacement + sealed[index + 1 :],
            path="/api/v1/session/oidc",
        )
        result = idp.callback(client)
    elif case == "other_key":
        # A cookie sealed with another LUMEWARP_SECRET_KEY must not authenticate.
        client.app.state.settings.secret_key = SecretStr(
            "another-login-state-secret-key-0123456789"
        )
        result = idp.callback(client)
    elif case == "other_login":
        idp.login(client)
        second_cookie = client.cookies.get(oidc.STATE_COOKIE)
        assert second_cookie is not None and second_cookie != sealed
        # The cookie belongs to the second login, so the first state must not complete.
        result = idp.callback(client, state=state)
        assert result.headers["location"] == "/login?error=invalid_state"
        # The failed attempt did not consume the second login.
        client.cookies.set(oidc.STATE_COOKIE, second_cookie, path="/api/v1/session/oidc")
        assert idp.callback(client).headers["location"] == "/"
    elif case == "expired":
        await committed.execute(
            update(OIDCLoginState)
            .where(OIDCLoginState.state_hash == hash_secret(state))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await committed.commit()
        result = idp.callback(client)
    else:
        assert idp.callback(client).headers["location"] == "/"
        client.cookies.set(oidc.STATE_COOKIE, sealed, path="/api/v1/session/oidc")
        result = idp.callback(client)
    assert result.headers["location"] == "/login?error=invalid_state"


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example",
        "https://evil.example",
        "/\\evil.example",
        "/%2fevil.example",
        "/%5cevil.example",
        "/\nevil.example",
        "relative",
    ],
)
def test_open_redirect_rejected(client, idp, path):
    response = client.get("/api/v1/session/oidc/login", params={"next": path})
    assert response.status_code == 422
    assert idp.requests == []


@pytest.mark.parametrize("kind", ["precreated", "identity"])
async def test_inactive_login_denied(client, idp, committed, kind):
    user, _ = await user_with_token(committed, email=idp.claims["email"], active=False)
    if kind == "identity":
        committed.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=idp.claims["sub"]))
    await committed.commit()
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/login?error=access_denied"


def test_jit_disabled(client, idp):
    client.app.state.settings.oidc_jit_users = False
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/login?error=access_denied"


def test_bad_signature(client, idp):
    idp.signing_key = RSAKey.generate_key(2048, parameters={"kid": "test"})
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/login?error=oidc_error"
    assert client.get("/api/v1/session").json()["authenticated"] is False


async def test_identity_and_session_cascade_on_user_delete(client, idp, committed):
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    user_id = uuid.UUID(client.get("/api/v1/session").json()["user"]["id"])
    assert client.delete(f"/api/v1/users/{user_id}", headers=ADMIN_AUTH).status_code == 204
    assert (
        await committed.scalar(select(UserIdentity).where(UserIdentity.user_id == user_id)) is None
    )
    assert await committed.scalar(select(UserSession).where(UserSession.user_id == user_id)) is None
    assert client.get("/api/v1/users/me").status_code == 401


@pytest.mark.parametrize("expired", [True, False])
async def test_session_expiry_and_deactivation(client, idp, committed, expired):
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    info = client.get("/api/v1/session").json()
    if expired:
        await committed.execute(
            update(UserSession)
            .where(UserSession.user_id == uuid.UUID(info["user"]["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await committed.commit()
    else:
        changed = client.patch(
            f"/api/v1/users/{info['user']['id']}",
            json={"is_active": False},
            headers=ADMIN_AUTH,
        )
        assert changed.status_code == 200, changed.text
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/session").json()["authenticated"] is False


@pytest.mark.parametrize("error", ["callback", "token"])
def test_idp_errors_are_sanitized(client, idp, error):
    idp.login(client)
    idp.token_error = error == "token"
    params = {"error": "sensitive upstream details"} if error == "callback" else {}
    response = idp.callback(client, **params)
    assert response.headers["location"] == "/login?error=oidc_error"


@pytest.mark.parametrize("seconds_expired, allowed", [(30, True), (61, False)])
def test_id_token_clock_skew_leeway(client, idp, monkeypatch, seconds_expired, allowed):
    now = int(datetime.now(UTC).timestamp())
    original = CodeIDToken.validate

    def validate(self, **kwargs):
        return original(self, now=now, leeway=kwargs["leeway"])

    monkeypatch.setattr(CodeIDToken, "validate", validate)
    idp.overrides["exp"] = now - seconds_expired
    idp.login(client)
    expected = "/" if allowed else "/login?error=oidc_error"
    assert idp.callback(client).headers["location"] == expected


@pytest.mark.parametrize("failure", ["state", "idp", "denied", "exception"])
def test_callback_warning_contains_only_class_and_code(client, idp, caplog, monkeypatch, failure):
    idp.login(client)
    params = {}
    expected_class, expected_code = "ValueError", "oidc_error"
    if failure == "state":
        params["state"] = "private-state-value"
        expected_code = "invalid_state"
    elif failure == "idp":
        params["error"] = "private-upstream-error"
    elif failure == "denied":
        idp.claims["email_verified"] = False
        expected_class, expected_code = "LoginDenied", "access_denied"
    else:

        async def fail(**kwargs):
            raise RuntimeError("private-secret-content")

        monkeypatch.setattr(client.app.state.oidc_client, "fetch_access_token", fail)
        expected_class = "RuntimeError"
    with caplog.at_level("WARNING", logger=session_api.__name__):
        response = idp.callback(client, **params)
    assert response.headers["location"] == f"/login?error={expected_code}"
    records = [r for r in caplog.records if r.name == session_api.__name__]
    assert len(records) == 1
    assert records[0].args == (expected_class, expected_code)
    assert records[0].exc_info is None
    assert "private" not in records[0].getMessage()


def test_unicode_return_path_is_percent_encoded(client, idp):
    idp.login(client, "/teams/測試?q=café&tab=1#設定")
    response = idp.callback(client)
    assert response.headers["location"] == (
        "/teams/%E6%B8%AC%E8%A9%A6?q=caf%C3%A9&tab=1#%E8%A8%AD%E5%AE%9A"
    )


async def test_login_cleanup_is_bounded_and_retains_recent_sessions(client, idp, committed):
    user, _ = await user_with_token(committed)
    now = datetime.now(UTC)
    expired_states = []
    old_sessions = []
    for _ in range(105):
        state = OIDCLoginState(
            state_hash=hash_secret(uuid.uuid4().hex),
            return_path="/",
            redirect_uri="https://testserver/api/v1/session/oidc/callback",
            expires_at=now - timedelta(days=1),
        )
        credential = UserSession(
            user_id=user.id,
            token_hash=hash_secret(uuid.uuid4().hex),
            csrf_hash=hash_secret("csrf"),
            expires_at=now - timedelta(days=8),
        )
        expired_states.append(state)
        old_sessions.append(credential)
        committed.add_all([state, credential])
    retained = []
    for expires, revoked in [
        (now - timedelta(days=1), None),
        (now + timedelta(days=1), now - timedelta(days=1)),
        (now + timedelta(days=1), None),
    ]:
        credential = UserSession(
            user_id=user.id,
            token_hash=hash_secret(uuid.uuid4().hex),
            csrf_hash=hash_secret("csrf"),
            expires_at=expires,
            revoked_at=revoked,
        )
        committed.add(credential)
        retained.append(credential)
    old_revoked = UserSession(
        user_id=user.id,
        token_hash=hash_secret(uuid.uuid4().hex),
        csrf_hash=hash_secret("csrf"),
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(days=8),
    )
    committed.add(old_revoked)
    await committed.commit()
    idp.login(client)
    assert (
        await committed.scalar(
            select(func.count())
            .select_from(OIDCLoginState)
            .where(OIDCLoginState.id.in_([s.id for s in expired_states]))
        )
        == 5
    )
    assert (
        await committed.scalar(
            select(func.count()).select_from(UserSession).where(UserSession.user_id == user.id)
        )
        == 9
    )  # 106 old rows minus 100, plus three retained.
    for credential in retained:
        await committed.refresh(credential)
    idp.login(client)
    assert (
        await committed.scalar(
            select(func.count()).select_from(UserSession).where(UserSession.user_id == user.id)
        )
        == 3
    )


async def test_plugin_mapping(client, idp, committed, monkeypatch):
    class Mapper:
        async def map_identity(self, claims):
            return plugins.IdentityMapping(
                email="mapped-" + claims["email"], display_name="Mapped Name"
            )

    monkeypatch.setattr(
        client.app.state, "auth_plugins", [plugins.LoadedPlugin("custom", Mapper())]
    )
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    user = client.get("/api/v1/session").json()["user"]
    assert user["email"] == "mapped-" + idp.claims["email"]
    assert user["display_name"] == "Mapped Name"
    assert user["is_admin"] is False


async def test_last_seen_throttle(client, idp, committed):
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    row = await committed.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_secret(client.cookies.get(sessions.COOKIE_NAME))
        )
    )
    first = row.last_seen_at
    assert client.get("/api/v1/users/me").status_code == 200
    await committed.refresh(row)
    assert row.last_seen_at == first
    row.last_seen_at = datetime.now(UTC) - timedelta(minutes=2)
    await committed.commit()
    assert client.get("/api/v1/users/me").status_code == 200
    await committed.refresh(row)
    assert row.last_seen_at > first


def test_secure_cookie_and_public_client(client, idp):
    settings = client.app.state.settings
    settings.session_cookie_secure = True
    settings.oidc_client_secret = None
    remote = client.app.state.oidc_client
    remote.client_secret = None
    remote.client_kwargs["token_endpoint_auth_method"] = "none"
    # HTTPS test origin allows Secure cookies to participate in the callback.
    client.base_url = "https://testserver"
    login_response = idp.login(client)
    assert "Secure" in login_response.headers["set-cookie"]
    assert "HttpOnly" in login_response.headers["set-cookie"]
    response = idp.callback(client)
    assert response.headers["location"] == "/"
    assert "Secure" in response.headers.get_list("set-cookie")[-1]
    token_request = next(r for r in idp.requests if r.url.path.endswith("/token"))
    assert "authorization" not in token_request.headers
    assert parse_qs(token_request.content.decode())["client_id"] == ["lumewarp"]


def test_invalid_bearer_takes_precedence_over_cookie(client, idp):
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    info = client.get("/api/v1/session").json()
    assert info["authenticated"] is True
    assert info["is_break_glass"] is False
    assert client.get("/api/v1/session", headers=auth("invalid")).json() == {
        "oidc_enabled": True,
        "break_glass_enabled": True,
        "is_break_glass": False,
        "authenticated": False,
        "authz_unavailable": False,
    }
    assert client.get("/api/v1/session").json()["authenticated"] is True


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("decision", [Decision.ALLOW, Decision.DENY, Decision.ERROR])
async def test_session_group_admin_uses_backend_with_local_error_fallback(
    client, committed, monkeypatch, local, decision
):
    user, token = await user_with_token(committed, group_admin=local)
    await committed.commit()
    calls = []

    async def check(user_id, relation, resource):
        calls.append((user_id, relation, resource))
        return decision

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    info = client.get("/api/v1/session", headers=auth(token))
    expected = decision is Decision.ALLOW
    assert info.status_code == 200
    assert info.json()["user"]["is_admin"] is expected
    assert info.json()["authz_unavailable"] is (decision is Decision.ERROR)
    assert calls == [(str(user.id), Relation.MEMBER, Resource("group", "lumewarp-admins"))]
    me = client.get("/api/v1/users/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["is_admin"] is expected
    assert me.json()["authz_unavailable"] is (decision is Decision.ERROR)

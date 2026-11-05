"""Installation roles: first-login proxy_admin, admin management and proxy protection."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane import installation, plugins
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.auth import oidc, sessions
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    AuditLog,
    InstallationAdmin,
    InstallationRole,
    PersonalAccessToken,
    Scope,
    User,
    UserIdentity,
    UserSession,
)
from lumewarp_control_plane.security import (
    Principal,
    PrincipalKind,
    SecretKind,
    generate_secret,
    hash_secret,
)
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import auth, suffix, user_with_token
from tests.auth.idp import ISSUER, FakeIdP
from tests.conftest import migrated_database
from tests.factories import make_user

SCOPE = "installation-roles-test"
BREAK_GLASS_PASSWORD = "test-installation-roles"
# Labels the request flow when a lock is gated in a concurrency regression test.
_FLOW: ContextVar[str] = ContextVar("installation_roles_test_flow", default="")


async def _as_flow(name: str, awaitable):
    token = _FLOW.set(name)
    try:
        return await awaitable
    finally:
        _FLOW.reset(token)


@pytest.fixture(scope="module")
async def roles_url() -> AsyncIterator[str]:
    async with migrated_database() as url:
        yield url


@pytest.fixture(scope="module")
async def roles_engine(roles_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(roles_url)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
async def roles_scope(roles_engine: AsyncEngine) -> None:
    async with AsyncSession(roles_engine) as session, session.begin():
        session.add(Scope(name=SCOPE))


@pytest.fixture(scope="module")
def roles_settings(roles_url: str, roles_scope: None) -> Settings:
    return Settings(
        database_url=roles_url,
        background_enabled=False,
        break_glass_password=BREAK_GLASS_PASSWORD,
        dev_mode=True,
        session_cookie_secure=False,
        scope=SCOPE,
    )


@pytest.fixture(scope="module")
def roles_app(roles_settings: Settings):
    app = create_app(roles_settings)
    yield app
    asyncio.run(app.state.engine.dispose())


@pytest.fixture
def roles_client(roles_app):
    # A context manager keeps one portal (and one event loop) for the whole test, so
    # asyncpg connections do not move between loops.
    with TestClient(roles_app) as client:
        yield client


@pytest.fixture
async def role_session(roles_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(roles_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture(autouse=True)
async def clean_installation_roles(roles_engine: AsyncEngine) -> AsyncIterator[None]:
    """Each test starts and ends with no installation roles in this module's database."""
    async with AsyncSession(roles_engine) as session, session.begin():
        await session.execute(delete(InstallationAdmin))
        await session.execute(delete(AuditLog))
    yield
    async with AsyncSession(roles_engine) as session, session.begin():
        await session.execute(delete(InstallationAdmin))
        await session.execute(delete(AuditLog))


def configure_oidc(app) -> FakeIdP:
    settings = app.state.settings
    settings.oidc_issuer = ISSUER
    settings.oidc_client_id = "lumewarp"
    settings.oidc_client_secret = SecretStr("client-secret")
    settings.secret_key = SecretStr("test-login-state-secret-key-0123456789abcdef")
    settings.session_cookie_secure = False
    provider = FakeIdP()
    # A fresh registry per test: the FakeIdP generates a new signing key, and Authlib
    # caches discovery/JWKS per client.
    if hasattr(app.state, "oidc_client"):
        del app.state.oidc_client
    request = oidc_request(app)
    remote = oidc.client(request)
    remote.client_kwargs["transport"] = httpx.MockTransport(provider.handle)
    app.state.auth_plugins = []
    return provider


@pytest.fixture
def roles_idp(roles_app) -> FakeIdP:
    return configure_oidc(roles_app)


def oidc_request(app):
    from starlette.requests import Request

    return Request({"type": "http", "app": app})


def sign_in(client: TestClient, provider: FakeIdP, *, email: str | None = None):
    if email is not None:
        provider.claims["email"] = email
    provider.login(client)
    return provider.callback(client)


def break_glass_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/session/break-glass", json={"password": BREAK_GLASS_PASSWORD})
    assert response.status_code == 204, response.text
    csrf = client.get("/api/v1/session").json()["csrf_token"]
    secret = client.cookies.get(sessions.COOKIE_NAME)
    # Use explicit credentials for the rest of the test: a logged-in jar would also send
    # this cookie to user-credential requests.
    client.cookies.clear()
    return {
        "Cookie": f"{sessions.COOKIE_NAME}={secret}",
        "X-Lumewarp-CSRF": csrf,
    }


async def set_role(engine: AsyncEngine, user_id: uuid.UUID, role: InstallationRole) -> None:
    async with AsyncSession(engine) as session, session.begin():
        session.add(InstallationAdmin(user_id=user_id, role=role))


async def role_of(engine: AsyncEngine, user_id: uuid.UUID) -> InstallationRole | None:
    async with AsyncSession(engine) as session:
        return await session.scalar(
            select(InstallationAdmin.role).where(InstallationAdmin.user_id == user_id)
        )


async def proxy_rows(engine: AsyncEngine) -> list[uuid.UUID]:
    async with AsyncSession(engine) as session:
        return list(
            await session.scalars(
                select(InstallationAdmin.user_id).where(
                    InstallationAdmin.role == InstallationRole.PROXY_ADMIN
                )
            )
        )


async def audit_rows(engine: AsyncEngine, action: str) -> list[AuditLog]:
    async with AsyncSession(engine) as session:
        return list(
            await session.scalars(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
            )
        )


async def _give_identity(
    role_session: AsyncSession, user: User, *, subject: str | None = None
) -> None:
    """Simulate the user's first OIDC login (the role APIs require one)."""
    role_session.add(
        UserIdentity(
            user_id=user.id,
            issuer="https://test-signin.example",
            subject=subject or uuid.uuid4().hex,
            email=user.email,
        )
    )
    await role_session.flush()


async def _session_headers(role_session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    token = await sessions.create_session(role_session, user_id, 3600)
    await role_session.commit()
    return auth("session:" + token)


async def test_first_login_creates_the_proxy_admin(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch
) -> None:
    email = f"initial-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", email)
    response = sign_in(roles_client, roles_idp, email=email)
    assert response.headers["location"] == "/"
    me = roles_client.get("/api/v1/users/me").json()
    assert (me["is_admin"], me["installation_role"]) == (True, "proxy_admin")
    bootstrap = await audit_rows(roles_engine, "installation_admin.bootstrap")
    assert len(bootstrap) == 1
    assert bootstrap[0].details == {"user_id": me["user_id"], "role": "proxy_admin"}
    assert await proxy_rows(roles_engine) == [uuid.UUID(me["user_id"])]
    session = roles_client.get("/api/v1/session").json()
    assert session["user"]["installation_role"] == "proxy_admin"


@pytest.mark.parametrize("case", ["wrong", "unverified", "unset"])
async def test_only_a_matching_verified_configured_email_gets_a_role(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch, case
) -> None:
    email = f"candidate-{suffix()}@example.com"
    if case == "unset":
        monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", None)
    else:
        monkeypatch.setattr(
            roles_app.state.settings,
            "initial_admin_email",
            email if case == "unverified" else f"other-{suffix()}@example.com",
        )
    if case == "unverified":
        roles_idp.claims["email_verified"] = False
    response = sign_in(roles_client, roles_idp, email=email)
    if case == "unverified":
        assert response.headers["location"] == "/login?error=access_denied"
        assert await proxy_rows(roles_engine) == []
        return
    assert response.headers["location"] == "/"
    me = roles_client.get("/api/v1/users/me").json()
    assert (me["is_admin"], me["installation_role"]) == (False, None)
    assert await proxy_rows(roles_engine) == []


async def test_jit_off_still_creates_the_initial_admin_only(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch
) -> None:
    monkeypatch.setattr(roles_app.state.settings, "oidc_jit_users", False)
    initial = f"initial-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", initial)
    assert sign_in(roles_client, roles_idp, email=initial).headers["location"] == "/"
    assert len(await proxy_rows(roles_engine)) == 1

    # Once a proxy_admin exists, the initial-email JIT exception is off too: changing the
    # setting cannot create another account just in time.
    other = f"other-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", other)
    roles_idp.claims["sub"] = str(uuid.uuid4())
    response = sign_in(roles_client, roles_idp, email=other)
    assert response.headers["location"] == "/login?error=access_denied"
    async with AsyncSession(roles_engine) as session:
        assert await session.scalar(select(User).where(User.email == other)) is None


async def test_an_existing_proxy_admin_is_never_replaced_by_the_setting(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch
) -> None:
    first = f"first-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", first)
    assert sign_in(roles_client, roles_idp, email=first).headers["location"] == "/"
    [first_proxy] = await proxy_rows(roles_engine)

    second = f"second-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", second)
    roles_idp.claims["sub"] = str(uuid.uuid4())
    assert sign_in(roles_client, roles_idp, email=second).headers["location"] == "/"
    assert await proxy_rows(roles_engine) == [first_proxy]
    async with AsyncSession(roles_engine) as session:
        second_user = await session.scalar(select(User).where(User.email == second))
    assert second_user is not None
    assert await role_of(roles_engine, second_user.id) is None


async def test_first_login_promotes_a_preassigned_admin_row(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch, role_session
) -> None:
    email = f"promote-{suffix()}@example.com"
    user = await make_user(role_session, email=email)
    role_session.add(
        UserIdentity(user_id=user.id, issuer=ISSUER, subject=roles_idp.claims["sub"], email=email)
    )
    role_session.add(InstallationAdmin(user_id=user.id, role=InstallationRole.ADMIN))
    await role_session.commit()
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", email)
    response = sign_in(roles_client, roles_idp, email=email)
    assert response.headers["location"] == "/"
    assert await role_of(roles_engine, user.id) is InstallationRole.PROXY_ADMIN
    assert await proxy_rows(roles_engine) == [user.id]


async def test_concurrent_first_logins_create_exactly_one_proxy_admin(
    roles_settings: Settings, roles_engine: AsyncEngine, monkeypatch
) -> None:
    app = create_app(roles_settings)
    provider = configure_oidc(app)
    email = f"race-{suffix()}@example.com"
    provider.claims["email"] = email
    monkeypatch.setattr(app.state.settings, "initial_admin_email", email)
    async with AsyncSession(roles_engine, expire_on_commit=False) as session, session.begin():
        user = User(email=email, display_name="Race")
        session.add(user)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=user.id, issuer=ISSUER, subject=provider.claims["sub"], email=email
            )
        )
        user_id = user.id

    transport = httpx.ASGITransport(app=app)

    async def login_and_callback() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.get("/api/v1/session/oidc/login")
            params = parse_qs(urlsplit(started.headers["location"]).query)
            state = params["state"][0]
            code = f"test-code-{uuid.uuid4().hex}"
            provider.by_code[code] = params
            # Each flow has its own client and login-state cookie, so the callback uses the
            # matching nonce and PKCE verifier.
            return await client.get(
                "/api/v1/session/oidc/callback",
                params={"state": state, "code": code},
            )

    try:
        responses = await asyncio.gather(login_and_callback(), login_and_callback())
        assert [response.headers["location"] for response in responses] == ["/", "/"]
        assert await proxy_rows(roles_engine) == [user_id]
        assert len(await audit_rows(roles_engine, "installation_admin.bootstrap")) == 1
    finally:
        await app.state.engine.dispose()


async def test_installation_admins_can_list_assign_and_remove_admins(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    target = await make_user(role_session)
    await _give_identity(role_session, target)
    await role_session.commit()
    headers = auth(admin_token)

    listed = roles_client.get("/api/v1/installation-admins", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {row["user_id"] for row in listed.json()["items"]} == {str(admin.id)}

    created = roles_client.put(
        f"/api/v1/installation-admins/{target.id}", json={"role": "admin"}, headers=headers
    )
    assert created.status_code == 200, created.text
    assert created.json()["role"] == "admin"
    assert created.json()["assigned_by"] == str(admin.id)
    target_token = await sessions.create_session(role_session, target.id, 3600)
    await role_session.commit()
    me = roles_client.get("/api/v1/users/me", headers=auth("session:" + target_token)).json()
    assert me["installation_role"] == "admin"
    # Idempotent: a second PUT keeps the role and writes no second audit row.
    again = roles_client.put(
        f"/api/v1/installation-admins/{target.id}", json={"role": "admin"}, headers=headers
    )
    assert again.status_code == 200
    assigned = await audit_rows(roles_engine, "installation_admin.assign")
    assert len(assigned) == 1
    assert assigned[0].details == {"user_id": str(target.id), "role": "admin"}

    removed = roles_client.delete(f"/api/v1/installation-admins/{target.id}", headers=headers)
    assert removed.status_code == 204, removed.text
    assert await role_of(roles_engine, target.id) is None
    removed_rows = await audit_rows(roles_engine, "installation_admin.remove")
    assert len(removed_rows) == 1
    assert removed_rows[0].details == {"user_id": str(target.id), "role": "admin"}
    assert (
        roles_client.get("/api/v1/users", headers=auth("session:" + target_token)).status_code
        == 403
    )

    # An admin may remove themself.
    assert (
        roles_client.delete(f"/api/v1/installation-admins/{admin.id}", headers=headers).status_code
        == 204
    )
    assert roles_client.get("/api/v1/users", headers=headers).status_code == 403


async def test_assign_never_sets_or_demotes_the_proxy_admin(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    headers = auth(admin_token)

    assert (
        roles_client.put(
            f"/api/v1/installation-admins/{proxy.id}",
            json={"role": "proxy_admin"},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        roles_client.put(
            f"/api/v1/installation-admins/{proxy.id}",
            json={"role": "admin"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        roles_client.delete(f"/api/v1/installation-admins/{proxy.id}", headers=headers).status_code
        == 403
    )
    assert await role_of(roles_engine, proxy.id) is InstallationRole.PROXY_ADMIN


async def test_a_plain_user_has_no_installation_admin_api(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    _, token = await user_with_token(role_session)
    target = await make_user(role_session)
    await role_session.commit()
    headers = auth(token)
    assert roles_client.get("/api/v1/installation-admins", headers=headers).status_code == 403
    assert (
        roles_client.put(
            f"/api/v1/installation-admins/{target.id}",
            json={"role": "admin"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        roles_client.delete(f"/api/v1/installation-admins/{target.id}", headers=headers).status_code
        == 403
    )


async def test_the_proxy_admin_account_is_protected(
    roles_app, roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session, email=f"proxy-{suffix()}@example.com")
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    admin_headers = auth(admin_token)

    forbidden = [
        roles_client.patch(
            f"/api/v1/users/{proxy.id}", json={"is_active": False}, headers=admin_headers
        ),
        roles_client.patch(
            f"/api/v1/users/{proxy.id}",
            json={"email": f"x-{suffix()}@example.com"},
            headers=admin_headers,
        ),
        roles_client.delete(f"/api/v1/users/{proxy.id}", headers=admin_headers),
        roles_client.post(
            f"/api/v1/users/{proxy.id}/tokens", json={"name": "takeover"}, headers=admin_headers
        ),
    ]
    assert [response.status_code for response in forbidden] == [403, 403, 403, 403]
    stored = roles_client.get(f"/api/v1/users/{proxy.id}", headers=admin_headers).json()
    assert stored["is_active"] is True and stored["email"] == proxy.email

    # The proxy_admin itself may change its own account and mint its own token, but
    # cannot delete itself or remove its own role.
    proxy_token = await sessions.create_session(role_session, proxy.id, 3600)
    await role_session.commit()
    proxy_headers = auth("session:" + proxy_token)
    new_email = f"proxy-new-{suffix()}@example.com"
    assert (
        roles_client.patch(
            f"/api/v1/users/{proxy.id}", json={"email": new_email}, headers=proxy_headers
        ).status_code
        == 200
    )
    issued = roles_client.post(
        f"/api/v1/users/{proxy.id}/tokens", json={"name": "own"}, headers=proxy_headers
    )
    assert issued.status_code == 201, issued.text
    assert (
        roles_client.delete(f"/api/v1/users/{proxy.id}", headers=proxy_headers).status_code == 409
    )
    assert (
        roles_client.delete(
            f"/api/v1/installation-admins/{proxy.id}", headers=proxy_headers
        ).status_code
        == 403
    )


async def test_break_glass_can_manage_the_proxy_admin_account(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    proxy = await make_user(role_session, email=f"proxy-{suffix()}@example.com")
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    headers = break_glass_headers(roles_client)
    assert (
        roles_client.patch(
            f"/api/v1/users/{proxy.id}", json={"display_name": "Recovered"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        roles_client.post(
            f"/api/v1/users/{proxy.id}/tokens", json={"name": "recovery"}, headers=headers
        ).status_code
        == 201
    )
    assert (
        roles_client.delete(f"/api/v1/installation-admins/{proxy.id}", headers=headers).status_code
        == 403
    )


async def test_transfer_moves_the_proxy_role(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    proxy = await make_user(role_session)
    first = await make_user(role_session)
    second = await make_user(role_session)
    inactive = await make_user(role_session, is_active=False)
    await _give_identity(role_session, first)
    await _give_identity(role_session, second)
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    proxy_token = await sessions.create_session(role_session, proxy.id, 3600)
    await role_session.commit()
    proxy_headers = auth("session:" + proxy_token)

    # An admin (not the proxy) cannot transfer.
    assert (
        roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(first.id)},
            headers=auth(admin_token),
        ).status_code
        == 403
    )
    # The proxy cannot transfer to itself or to an inactive account.
    assert (
        roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(proxy.id)},
            headers=proxy_headers,
        ).status_code
        == 409
    )
    assert (
        roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(inactive.id)},
            headers=proxy_headers,
        ).status_code
        == 409
    )
    transferred = roles_client.post(
        "/api/v1/installation-admins/transfer",
        json={"user_id": str(first.id)},
        headers=proxy_headers,
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["role"] == "proxy_admin"
    assert await role_of(roles_engine, first.id) is InstallationRole.PROXY_ADMIN
    assert await role_of(roles_engine, proxy.id) is InstallationRole.ADMIN
    # The previous proxy is now a plain admin and cannot transfer again.
    assert (
        roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(second.id)},
            headers=proxy_headers,
        ).status_code
        == 403
    )
    # Break-glass is the recovery path.
    headers = break_glass_headers(roles_client)
    assert (
        roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(second.id)},
            headers=headers,
        ).status_code
        == 200
    )
    assert await role_of(roles_engine, second.id) is InstallationRole.PROXY_ADMIN
    assert await role_of(roles_engine, first.id) is InstallationRole.ADMIN
    transfers = await audit_rows(roles_engine, "installation_admin.transfer")
    assert [row.details for row in transfers] == [
        {"previous_user_id": str(proxy.id), "new_user_id": str(first.id), "revoked": 0},
        {"previous_user_id": str(first.id), "new_user_id": str(second.id), "revoked": 0},
    ]


async def _session_headers(role_session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    token = await sessions.create_session(role_session, user_id, 3600)
    await role_session.commit()
    return auth("session:" + token)


async def test_transfer_revokes_a_pat_issued_before_the_promotion(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session)
    target = await make_user(role_session)
    await _give_identity(role_session, target)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    issued = roles_client.post(
        f"/api/v1/users/{target.id}/tokens",
        json={"name": "pre-promotion"},
        headers=auth(admin_token),
    )
    assert issued.status_code == 201, issued.text
    old_secret = issued.json()["secret"]
    assert roles_client.get("/api/v1/users/me", headers=auth(old_secret)).status_code == 200

    transferred = roles_client.post(
        "/api/v1/installation-admins/transfer",
        json={"user_id": str(target.id)},
        headers=await _session_headers(role_session, proxy.id),
    )
    assert transferred.status_code == 200, transferred.text
    # The admin-issued token would otherwise carry the new role.
    assert roles_client.get("/api/v1/users/me", headers=auth(old_secret)).status_code == 401
    revocations = await audit_rows(roles_engine, "installation_admin.revoke_tokens")
    assert [row.details for row in revocations] == [{"user_id": str(target.id), "revoked": 1}]
    # A login session created after the promotion works, and a token the new proxy issues
    # for itself is a fresh, valid proxy_admin credential.
    new_headers = await _session_headers(role_session, target.id)
    me = roles_client.get("/api/v1/users/me", headers=new_headers).json()
    assert (me["is_admin"], me["installation_role"]) == (True, "proxy_admin")
    fresh = roles_client.post(
        "/api/v1/users/me/tokens", json={"name": "fresh"}, headers=new_headers
    )
    assert fresh.status_code == 201, fresh.text
    fresh_secret = fresh.json()["secret"]
    assert roles_client.get("/api/v1/users", headers=auth(fresh_secret)).status_code == 200


async def test_bootstrap_revokes_a_pat_issued_before_the_first_login(
    roles_app,
    roles_client,
    roles_idp,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    monkeypatch,
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    email = f"bootstrap-pat-{suffix()}@example.com"
    user = await make_user(role_session, email=email)
    role_session.add(
        UserIdentity(user_id=user.id, issuer=ISSUER, subject=roles_idp.claims["sub"], email=email)
    )
    await role_session.commit()
    issued = roles_client.post(
        f"/api/v1/users/{user.id}/tokens",
        json={"name": "pre-bootstrap"},
        headers=auth(admin_token),
    )
    assert issued.status_code == 201, issued.text
    old_secret = issued.json()["secret"]
    assert roles_client.get("/api/v1/users/me", headers=auth(old_secret)).status_code == 200

    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", email)
    response = sign_in(roles_client, roles_idp, email=email)
    assert response.headers["location"] == "/"
    assert roles_client.get("/api/v1/users/me", headers=auth(old_secret)).status_code == 401
    me = roles_client.get("/api/v1/users/me").json()
    assert (me["is_admin"], me["installation_role"]) == (True, "proxy_admin")
    revocations = await audit_rows(roles_engine, "installation_admin.revoke_tokens")
    assert [row.details for row in revocations] == [{"user_id": str(user.id), "revoked": 1}]


@pytest.mark.parametrize("target_first", [True, False])
async def test_transfer_to_an_existing_admin_works_in_either_key_order(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession, target_first: bool
) -> None:
    if target_first:
        target = await make_user(role_session)
        await _give_identity(role_session, target)
        await role_session.commit()
        await set_role(roles_engine, target.id, InstallationRole.ADMIN)
        proxy = await make_user(role_session)
        await role_session.commit()
        await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    else:
        proxy = await make_user(role_session)
        await role_session.commit()
        await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
        target = await make_user(role_session)
        await _give_identity(role_session, target)
        await role_session.commit()
        await set_role(roles_engine, target.id, InstallationRole.ADMIN)
    response = roles_client.post(
        "/api/v1/installation-admins/transfer",
        json={"user_id": str(target.id)},
        headers=await _session_headers(role_session, proxy.id),
    )
    assert response.status_code == 200, response.text
    assert await proxy_rows(roles_engine) == [target.id]
    assert await role_of(roles_engine, target.id) is InstallationRole.PROXY_ADMIN
    assert await role_of(roles_engine, proxy.id) is InstallationRole.ADMIN


async def test_a_protected_update_waits_for_a_concurrent_promotion(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session)
    target = await make_user(role_session)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    headers = auth(admin_token)

    blocker = AsyncSession(roles_engine, expire_on_commit=False)
    await blocker.begin()
    await installation.lock_installation(blocker)
    await blocker.execute(
        update(InstallationAdmin)
        .where(InstallationAdmin.user_id == proxy.id)
        .values(role=InstallationRole.ADMIN)
    )
    await blocker.flush()
    blocker.add(InstallationAdmin(user_id=target.id, role=InstallationRole.PROXY_ADMIN))
    await blocker.flush()

    result: dict[str, httpx.Response] = {}

    def patch_user() -> None:
        result["response"] = roles_client.patch(
            f"/api/v1/users/{target.id}", json={"is_active": False}, headers=headers
        )

    thread = threading.Thread(target=patch_user)
    thread.start()
    try:
        await asyncio.sleep(0.3)
        assert thread.is_alive(), "the protected write must wait for the installation lock"
    finally:
        await blocker.commit()
        await blocker.close()
    thread.join(10)
    assert not thread.is_alive()
    assert result["response"].status_code == 403, result["response"].text
    # The promotion won the race; the target was not deactivated.
    stored = roles_client.get(f"/api/v1/users/{target.id}", headers=headers).json()
    assert stored["is_active"] is True


async def test_transfer_refuses_a_target_deactivated_while_it_waits(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    proxy = await make_user(role_session)
    target = await make_user(role_session)
    await _give_identity(role_session, target)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    headers = await _session_headers(role_session, proxy.id)

    blocker = AsyncSession(roles_engine, expire_on_commit=False)
    await blocker.begin()
    await installation.lock_installation(blocker)
    await blocker.execute(update(User).where(User.id == target.id).values(is_active=False))
    await blocker.flush()

    result: dict[str, httpx.Response] = {}

    def transfer() -> None:
        result["response"] = roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(target.id)},
            headers=headers,
        )

    thread = threading.Thread(target=transfer)
    thread.start()
    try:
        await asyncio.sleep(0.3)
        assert thread.is_alive(), "transfer must wait for the installation lock"
    finally:
        await blocker.commit()
        await blocker.close()
    thread.join(10)
    assert not thread.is_alive()
    assert result["response"].status_code == 409, result["response"].text
    assert await proxy_rows(roles_engine) == [proxy.id]


async def test_jit_off_rejects_a_plugin_mapping_to_the_initial_email(
    roles_app, roles_client, roles_idp, roles_engine: AsyncEngine, monkeypatch
) -> None:
    initial = f"initial-mapped-{suffix()}@example.com"
    monkeypatch.setattr(roles_app.state.settings, "oidc_jit_users", False)
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", initial)

    class Mapper:
        api_version = plugins.PLUGIN_API_VERSION

        async def map_identity(self, claims):
            return plugins.IdentityMapping(email=initial, display_name="Mapped")

    monkeypatch.setattr(
        roles_app.state,
        "auth_plugins",
        [plugins.LoadedPlugin(name="mapped", plugin=Mapper())],
        raising=False,
    )
    roles_idp.claims["email"] = f"other-{suffix()}@example.com"
    roles_idp.claims["email_verified"] = True
    response = sign_in(roles_client, roles_idp)
    assert response.headers["location"] == "/login?error=access_denied"
    async with AsyncSession(roles_engine) as session:
        assert await session.scalar(select(User).where(User.email == initial)) is None


async def test_deleting_an_admin_records_a_role_removal(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    proxy = await make_user(role_session)
    admin = await make_user(role_session)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    await set_role(roles_engine, admin.id, InstallationRole.ADMIN)
    response = roles_client.delete(
        f"/api/v1/users/{admin.id}",
        headers=await _session_headers(role_session, proxy.id),
    )
    assert response.status_code == 204, response.text
    removed = await audit_rows(roles_engine, "installation_admin.remove")
    assert [row.details for row in removed] == [{"user_id": str(admin.id), "role": "admin"}]
    assert await role_of(roles_engine, admin.id) is None


async def test_a_revoked_pat_waiting_for_the_installation_lock_cannot_mint(
    roles_settings: Settings,
    roles_client,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    monkeypatch,
) -> None:
    """A PAT revoked by a promotion while its request waits must not mint a new proxy PAT."""
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session)
    target = await make_user(role_session)
    await _give_identity(role_session, target)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    issued = roles_client.post(
        f"/api/v1/users/{target.id}/tokens",
        json={"name": "before-promotion"},
        headers=auth(admin_token),
    )
    assert issued.status_code == 201, issued.text
    old_secret = issued.json()["secret"]
    proxy_headers = await _session_headers(role_session, proxy.id)
    # A session of the user themself is not a PAT: promotion does not revoke it.
    pre_promotion_session = await _session_headers(role_session, target.id)

    app = create_app(roles_settings)
    original = installation.lock_installation
    held = asyncio.Event()
    waiting = asyncio.Event()
    proceed = asyncio.Event()

    async def scheduled_lock(session):
        if _FLOW.get() == "transfer":
            await original(session)
            held.set()
            await proceed.wait()
            return
        if _FLOW.get() == "own":
            waiting.set()
        await original(session)

    monkeypatch.setattr(installation, "lock_installation", scheduled_lock)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as transfer_client,
            httpx.AsyncClient(transport=transport, base_url="http://test") as own_client,
        ):
            transfer = asyncio.create_task(
                _as_flow(
                    "transfer",
                    transfer_client.post(
                        "/api/v1/installation-admins/transfer",
                        json={"user_id": str(target.id)},
                        headers=proxy_headers,
                    ),
                )
            )
            await asyncio.wait_for(held.wait(), 10)
            # Authenticates against the still-valid PAT, then waits for the transfer's lock.
            own = asyncio.create_task(
                _as_flow(
                    "own",
                    own_client.post(
                        "/api/v1/users/me/tokens",
                        json={"name": "pending"},
                        headers=auth(old_secret),
                    ),
                )
            )
            await asyncio.wait_for(waiting.wait(), 10)
            proceed.set()
            transfer_response, own_response = await asyncio.wait_for(
                asyncio.gather(transfer, own), 15
            )
    finally:
        await app.state.engine.dispose()

    assert transfer_response.status_code == 200, transfer_response.text
    assert own_response.status_code == 401, own_response.text
    assert roles_client.get("/api/v1/users/me", headers=auth(old_secret)).status_code == 401
    assert await proxy_rows(roles_engine) == [target.id]
    # A session that existed before the promotion stays valid (it is not a PAT).
    pre = roles_client.get("/api/v1/users/me", headers=pre_promotion_session).json()
    assert (pre["is_admin"], pre["installation_role"]) == (True, "proxy_admin")
    # A freshly logged-in session of the new proxy is not a PAT and stays valid.
    new_headers = await _session_headers(role_session, target.id)
    me = roles_client.get("/api/v1/users/me", headers=new_headers).json()
    assert (me["is_admin"], me["installation_role"]) == (True, "proxy_admin")
    fresh = roles_client.post(
        "/api/v1/users/me/tokens", json={"name": "fresh"}, headers=new_headers
    )
    assert fresh.status_code == 201, fresh.text
    assert (
        roles_client.get("/api/v1/users", headers=auth(fresh.json()["secret"])).status_code == 200
    )


@pytest.mark.parametrize("change", ["email", "deactivate"])
async def test_bootstrap_rechecks_the_persisted_user_under_the_lock(
    roles_settings: Settings,
    roles_client,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    monkeypatch,
    change: str,
) -> None:
    """A concurrent email change or deactivation must win over a stale login snapshot."""
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    email = f"stale-{suffix()}@example.com"
    target = await make_user(role_session, email=email)
    await role_session.commit()

    app = create_app(roles_settings)
    provider = configure_oidc(app)
    provider.claims["email"] = email
    monkeypatch.setattr(app.state.settings, "initial_admin_email", email)
    role_session.add(
        UserIdentity(user_id=target.id, issuer=ISSUER, subject=provider.claims["sub"], email=email)
    )
    await role_session.commit()

    original = installation.lock_installation
    checked = asyncio.Event()
    proceed = asyncio.Event()

    async def scheduled_lock(session):
        if _FLOW.get() == "oidc":
            checked.set()
            await proceed.wait()
        await original(session)

    monkeypatch.setattr(installation, "lock_installation", scheduled_lock)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as browser,
            httpx.AsyncClient(transport=transport, base_url="http://test") as admin_client,
        ):
            started = await browser.get("/api/v1/session/oidc/login")
            params = parse_qs(urlsplit(started.headers["location"]).query)
            code = f"test-code-{uuid.uuid4().hex}"
            provider.by_code[code] = params
            callback = asyncio.create_task(
                _as_flow(
                    "oidc",
                    browser.get(
                        "/api/v1/session/oidc/callback",
                        params={"state": params["state"][0], "code": code},
                    ),
                )
            )
            await asyncio.wait_for(checked.wait(), 10)
            body = (
                {"is_active": False}
                if change == "deactivate"
                else {"email": f"changed-{suffix()}@example.com"}
            )
            patched = await admin_client.patch(
                f"/api/v1/users/{target.id}", json=body, headers=auth(admin_token)
            )
            assert patched.status_code == 200, patched.text
            proceed.set()
            response = await asyncio.wait_for(callback, 10)
    finally:
        await app.state.engine.dispose()

    assert response.status_code == 302, response.text
    assert response.headers["location"] == (
        "/" if change == "email" else "/login?error=access_denied"
    )
    assert await proxy_rows(roles_engine) == []
    stored = roles_client.get(f"/api/v1/users/{target.id}", headers=auth(admin_token)).json()
    if change == "deactivate":
        assert stored["is_active"] is False
    else:
        assert stored["email"].startswith("changed-")


async def test_oidc_first_login_and_a_protected_write_do_not_deadlock(
    roles_settings: Settings,
    roles_client,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    monkeypatch,
) -> None:
    """The login takes the installation lock before locking the User row; update_user does too.

    The old order was: resolve_user locks the User row, bootstrap waits for the installation
    lock; update_user holds the installation lock and waits for the User row. This exercises
    the same pair now that transfer no longer accepts a never-signed-in target.
    """
    email = f"lock-order-{suffix()}@example.com"
    target = await make_user(role_session, email=email)
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    await role_session.commit()

    app = create_app(roles_settings)
    provider = configure_oidc(app)
    provider.claims["email"] = email
    monkeypatch.setattr(app.state.settings, "initial_admin_email", email)
    original = installation.lock_installation
    row_held = asyncio.Event()
    admin_held = asyncio.Event()
    proceed = asyncio.Event()

    async def scheduled_lock(session):
        if _FLOW.get() == "oidc":
            row_held.set()
            await proceed.wait()
        await original(session)
        if _FLOW.get() == "admin":
            admin_held.set()

    monkeypatch.setattr(installation, "lock_installation", scheduled_lock)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as browser,
            httpx.AsyncClient(transport=transport, base_url="http://test") as admin_client,
        ):
            started = await browser.get("/api/v1/session/oidc/login")
            params = parse_qs(urlsplit(started.headers["location"]).query)
            code = f"test-code-{uuid.uuid4().hex}"
            provider.by_code[code] = params
            callback = asyncio.create_task(
                _as_flow(
                    "oidc",
                    browser.get(
                        "/api/v1/session/oidc/callback",
                        params={"state": params["state"][0], "code": code},
                    ),
                )
            )
            await asyncio.wait_for(row_held.wait(), 10)
            patched = asyncio.create_task(
                _as_flow(
                    "admin",
                    admin_client.patch(
                        f"/api/v1/users/{target.id}",
                        json={"display_name": "Concurrent"},
                        headers=auth(admin_token),
                    ),
                )
            )
            await asyncio.wait_for(admin_held.wait(), 10)
            # Let the protected write's User update meet the callback's row lock (old code).
            await asyncio.sleep(0.1)
            proceed.set()
            callback_response, patch_response = await asyncio.wait_for(
                asyncio.gather(callback, patched), 15
            )
    finally:
        await app.state.engine.dispose()

    assert patch_response.status_code == 200, patch_response.text
    assert callback_response.status_code == 302, callback_response.text
    assert callback_response.headers["location"] == "/"
    assert await proxy_rows(roles_engine) == [target.id]
    stored = roles_client.get(f"/api/v1/users/{target.id}", headers=auth(admin_token)).json()
    assert stored["display_name"] == "Concurrent"


@pytest.mark.parametrize("kind", ["assign", "transfer"])
async def test_an_installation_role_requires_a_prior_login(
    roles_app,
    roles_client,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    roles_idp,
    monkeypatch,
    kind: str,
) -> None:
    """A never-signed-in account cannot receive a role; after one login it can."""
    email = f"needs-login-{suffix()}@example.com"
    target = await make_user(role_session, email=email)
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    proxy = await make_user(role_session)
    await role_session.commit()
    await set_role(roles_engine, proxy.id, InstallationRole.PROXY_ADMIN)
    monkeypatch.setattr(roles_app.state.settings, "initial_admin_email", None)

    if kind == "assign":
        headers = auth(admin_token)
        response = roles_client.put(
            f"/api/v1/installation-admins/{target.id}",
            json={"role": "admin"},
            headers=headers,
        )
    else:
        headers = await _session_headers(role_session, proxy.id)
        response = roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(target.id)},
            headers=headers,
        )
    assert response.status_code == 409, response.text
    assert "sign in once" in response.json()["error"]["message"]

    # Simulate the first OIDC login; resolve_user links the identity to the pre-created user.
    assert sign_in(roles_client, roles_idp, email=email).headers["location"] == "/"
    roles_client.cookies.clear()
    if kind == "assign":
        headers = auth(admin_token)
        response = roles_client.put(
            f"/api/v1/installation-admins/{target.id}",
            json={"role": "admin"},
            headers=headers,
        )
    else:
        headers = await _session_headers(role_session, proxy.id)
        response = roles_client.post(
            "/api/v1/installation-admins/transfer",
            json={"user_id": str(target.id)},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    # A second login works and carries the assigned role.
    assert sign_in(roles_client, roles_idp, email=email).headers["location"] == "/"
    me = roles_client.get("/api/v1/users/me").json()
    assert (me["is_admin"], me["installation_role"]) == (
        True,
        "admin" if kind == "assign" else "proxy_admin",
    )


async def test_a_role_removed_while_waiting_fails_the_request(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    """The cached installation role is re-read and re-required after the lock."""
    admin, admin_token = await user_with_token(role_session, installation_admin=True)
    victim = await make_user(role_session, display_name="Original")
    await role_session.commit()
    headers = auth(admin_token)

    blocker = AsyncSession(roles_engine, expire_on_commit=False)
    await blocker.begin()
    await installation.lock_installation(blocker)
    await blocker.execute(delete(InstallationAdmin).where(InstallationAdmin.user_id == admin.id))
    await blocker.flush()

    result: dict[str, httpx.Response] = {}

    def patch_victim() -> None:
        result["response"] = roles_client.patch(
            f"/api/v1/users/{victim.id}",
            json={"display_name": "Changed"},
            headers=headers,
        )

    thread = threading.Thread(target=patch_victim)
    thread.start()
    try:
        await asyncio.sleep(0.2)
        assert thread.is_alive(), "the request must wait for the installation lock"
    finally:
        await blocker.commit()
        await blocker.close()
    thread.join(10)
    assert not thread.is_alive()
    assert result["response"].status_code == 403, result["response"].text
    async with AsyncSession(roles_engine) as session:
        stored = await session.get(User, victim.id)
        assert stored.display_name == "Original"


async def test_bootstrap_rechecks_a_stale_user_object(
    roles_settings: Settings,
    roles_engine: AsyncEngine,
    role_session: AsyncSession,
    monkeypatch,
) -> None:
    """The in-lock re-read, not the identity-map copy, decides bootstrap eligibility."""
    email = f"stale-object-{suffix()}@example.com"
    user = await make_user(role_session, email=email)
    await role_session.commit()
    stale = await role_session.get(User, user.id)
    assert stale.email == email
    async with AsyncSession(roles_engine) as other, other.begin():
        await other.execute(
            update(User).where(User.id == user.id).values(email=f"changed-{suffix()}@example.com")
        )
    monkeypatch.setattr(roles_settings, "initial_admin_email", email)
    claims = {"email": email, "email_verified": True}
    assert not await installation.bootstrap_initial_admin(
        role_session, claims, stale, roles_settings, locked=False
    )
    assert await proxy_rows(roles_engine) == []


async def test_recheck_rejects_a_logged_out_break_glass_session(
    roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    token = await sessions.create_session(role_session, None, 3600, kind=PrincipalKind.BREAK_GLASS)
    await role_session.commit()
    row = await role_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_secret(token))
    )
    assert row is not None
    row.revoked_at = datetime.now(UTC)
    await role_session.commit()
    with pytest.raises(ApiError) as exc:
        await installation.recheck_principal(
            role_session,
            Principal(kind=PrincipalKind.BREAK_GLASS, token_id=row.id),
        )
    assert exc.value.status == 401


async def test_a_pat_of_an_installation_admin_is_an_installation_admin(
    roles_client, role_session: AsyncSession
) -> None:
    user, _ = await user_with_token(role_session, installation_admin=True)
    pat = generate_secret(SecretKind.PAT)
    role_session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="installation-admin-pat",
            token_hash=pat.hash,
            display_prefix=pat.display_prefix,
        )
    )
    await role_session.commit()
    assert roles_client.get("/api/v1/users", headers=auth(pat.secret)).status_code == 200
    me = roles_client.get("/api/v1/users/me", headers=auth(pat.secret)).json()
    assert (me["is_admin"], me["installation_role"]) == (True, "admin")


async def test_revocation_applies_to_the_next_session_and_pat_request(
    roles_client, roles_engine: AsyncEngine, role_session: AsyncSession
) -> None:
    user, session_token = await user_with_token(role_session, installation_admin=True)
    pat = generate_secret(SecretKind.PAT)
    role_session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="revoked-next-request",
            token_hash=pat.hash,
            display_prefix=pat.display_prefix,
        )
    )
    await role_session.commit()
    for headers in (auth(session_token), auth(pat.secret)):
        assert roles_client.get("/api/v1/users", headers=headers).status_code == 200
    async with AsyncSession(roles_engine) as session, session.begin():
        await session.execute(delete(InstallationAdmin).where(InstallationAdmin.user_id == user.id))
    for headers in (auth(session_token), auth(pat.secret)):
        assert roles_client.get("/api/v1/users", headers=headers).status_code == 403

"""Browser login, session discovery and local logout."""

import asyncio
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, or_, select, update

from lumewarp_control_plane import audit, installation
from lumewarp_control_plane.auth import break_glass, oidc, sessions
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import (
    ActorType,
    InstallationRole,
    OIDCLoginState,
    User,
    UserSession,
)
from lumewarp_control_plane.security import (
    CurrentPrincipal,
    PrincipalKind,
    current_principal,
    hash_secret,
)

router = APIRouter(prefix="/session", tags=["session"])
log = logging.getLogger(__name__)
LOGIN_CLEANUP_LIMIT = 100


class SessionUser(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    installation_role: InstallationRole | None = None


class SessionInfo(BaseModel):
    oidc_enabled: bool
    break_glass_enabled: bool
    authenticated: bool
    is_break_glass: bool = False
    authz_unavailable: bool = False
    user: SessionUser | None = None
    csrf_token: str | None = None


@router.get("", response_model_exclude_none=True, operation_id="getSession")
async def session_info(request: Request, response: Response) -> SessionInfo:
    response.headers["Cache-Control"] = "no-store"
    info = SessionInfo(
        oidc_enabled=bool(request.app.state.settings.oidc_issuer),
        break_glass_enabled=break_glass.enabled(request.app.state.settings),
        authenticated=False,
    )
    try:
        principal = await current_principal(request)
    except ApiError as exc:
        if exc.status != 401:
            raise
        return info
    info.authenticated = True
    info.is_break_glass = principal.is_break_glass
    if principal.user_id is not None:
        async with request.app.state.session_factory() as session:
            user = await session.get(User, principal.user_id)
            authz = RequestAuthz(
                principal, request.app.state.authorizer, request.app.state.session_factory
            )
            role = await authz.installation_role()
            info.user = SessionUser(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                is_admin=role is not None,
                installation_role=role,
            )
    if principal.kind in (PrincipalKind.SESSION, PrincipalKind.BREAK_GLASS):
        info.csrf_token = sessions.csrf_token(request.cookies[sessions.COOKIE_NAME])
    return info


class BreakGlassLogin(BaseModel):
    password: SecretStr = Field(max_length=4096)


@router.post("/break-glass", operation_id="loginBreakGlass")
async def login_break_glass(body: BreakGlassLogin, request: Request) -> Response:
    settings = request.app.state.settings
    if not break_glass.enabled(settings):
        raise not_found()
    ip = break_glass.client_ip(request, settings)
    if not break_glass.client_allowed(ip, settings):
        raise not_found()
    limiter = request.app.state.break_glass_limiter
    result = "rate_limited"
    secret = None
    if limiter.reserve(ip):
        delay = limiter.delay()
        if delay:
            await asyncio.sleep(delay)
        valid = await asyncio.to_thread(
            break_glass.verify, body.password.get_secret_value(), settings
        )
        result = "success" if valid else "failure"
    if result != "rate_limited" or limiter.summarize_rejection():
        async with request.app.state.session_factory() as session, session.begin():
            if result == "success":
                secret = await sessions.create_session(
                    session,
                    None,
                    settings.break_glass_session_ttl_seconds,
                    kind=PrincipalKind.BREAK_GLASS,
                    credential_fingerprint=break_glass.fingerprint(settings),
                )
            audit.record(
                session,
                action="session.break_glass.login",
                resource_type="session",
                actor_type=ActorType.BREAK_GLASS,
                details=(
                    {"result": result, "window_seconds": break_glass.WINDOW_SECONDS}
                    if result == "rate_limited"
                    else {"result": result}
                ),
            )
    request.app.state.metrics.break_glass_logins.labels(result).inc()
    if result == "rate_limited":
        raise ApiError(429, "rate_limited", "too many login attempts")
    if result == "failure":
        raise ApiError(401, "invalid_credentials", "invalid password")
    limiter.success(ip)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        sessions.COOKIE_NAME,
        secret,
        max_age=settings.break_glass_session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


def require_oidc(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.oidc_issuer:
        raise not_found()
    if not settings.secret_key:
        log.error("OIDC is enabled but LUMEWARP_SECRET_KEY is missing")
        raise ApiError(503, "sso_unconfigured", "SSO is not configured")


def clear_state(response: Response, request: Request) -> None:
    response.delete_cookie(
        oidc.STATE_COOKIE,
        path="/api/v1/session/oidc",
        secure=request.app.state.settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


def callback_failure(response: RedirectResponse, code: str, exc: Exception) -> RedirectResponse:
    log.warning("OIDC callback failed: %s %s", type(exc).__name__, code)
    response.headers["location"] = f"/login?error={code}"
    return response


@router.get("/oidc/login", operation_id="startOidcLogin")
async def login(request: Request, next: str = "/") -> RedirectResponse:
    require_oidc(request)
    try:
        path = oidc.return_path(next)
    except ValueError as exc:
        raise ApiError(422, "invalid_next", "next must be a same-site relative path") from exc
    settings = request.app.state.settings
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    redirect_uri = (settings.public_url or str(request.base_url)).rstrip(
        "/"
    ) + "/api/v1/session/oidc/callback"
    try:
        authorization = await oidc.client(request).create_authorization_url(
            redirect_uri, state=state, nonce=nonce, code_verifier=verifier
        )
    except Exception:
        return RedirectResponse("/login?error=oidc_error", status_code=302)
    now = datetime.now(UTC)
    async with request.app.state.session_factory() as session, session.begin():
        await session.execute(
            delete(OIDCLoginState).where(
                OIDCLoginState.id.in_(
                    select(OIDCLoginState.id)
                    .where(OIDCLoginState.expires_at <= now)
                    .order_by(OIDCLoginState.expires_at, OIDCLoginState.id)
                    .limit(LOGIN_CLEANUP_LIMIT)
                )
            )
        )
        cutoff = now - timedelta(days=7)
        await session.execute(
            delete(UserSession).where(
                UserSession.id.in_(
                    select(UserSession.id)
                    .where(or_(UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff))
                    .order_by(UserSession.id)
                    .limit(LOGIN_CLEANUP_LIMIT)
                )
            )
        )
        session.add(
            OIDCLoginState(
                state_hash=hash_secret(state),
                return_path=path,
                redirect_uri=redirect_uri,
                expires_at=now + timedelta(seconds=oidc.STATE_TTL),
            )
        )
    response = RedirectResponse(authorization["url"], status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        oidc.STATE_COOKIE,
        oidc.seal_login_state(
            settings, oidc.LoginState(state=state, nonce=nonce, code_verifier=verifier)
        ),
        max_age=oidc.STATE_TTL,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/api/v1/session/oidc",
    )
    return response


@router.get("/oidc/callback", operation_id="completeOidcLogin")
async def callback(request: Request) -> RedirectResponse:
    require_oidc(request)
    state = request.query_params.get("state", "")
    sealed = request.cookies.get(oidc.STATE_COOKIE, "")
    response = RedirectResponse("/login?error=invalid_state", status_code=302)
    clear_state(response, request)
    try:
        login_state = oidc.open_login_state(request.app.state.settings, sealed)
    except oidc.LoginStateInvalid as exc:
        return callback_failure(response, "invalid_state", exc)
    if not state or not hmac.compare_digest(state.encode(), login_state.state.encode()):
        return callback_failure(response, "invalid_state", ValueError())
    async with request.app.state.session_factory() as session, session.begin():
        saved = await session.scalar(
            delete(OIDCLoginState)
            .where(OIDCLoginState.state_hash == hash_secret(state))
            .returning(OIDCLoginState)
        )
    if saved is None or saved.expires_at <= datetime.now(UTC):
        return callback_failure(response, "invalid_state", ValueError())
    try:
        if request.query_params.get("error") or not request.query_params.get("code"):
            raise ValueError("authorization failed")
        remote = oidc.client(request)
        token = await remote.fetch_access_token(
            code=request.query_params["code"],
            redirect_uri=saved.redirect_uri,
            code_verifier=login_state.code_verifier,
        )
        settings = request.app.state.settings
        claims = await remote.parse_id_token(
            token,
            nonce=login_state.nonce,
            leeway=60,
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer},
                "aud": {"essential": True, "value": settings.oidc_client_id},
            },
        )
        if not claims or not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("missing subject")
        async with request.app.state.session_factory() as session, session.begin():
            # Take the installation lock before resolve_user can lock or link the User row,
            # so this path and transfer/protected writes share one lock order. Ordinary
            # logins skip the lock and stay unserialized.
            bootstrap_locked = await installation.prepare_bootstrap_lock(session, claims, settings)
            user = await oidc.resolve_user(session, claims, request)
            # First login: inside this transaction, so the role and the session commit
            # together. Concurrent first logins produce exactly one proxy_admin. If the
            # pre-check said a proxy_admin exists (or this login cannot bootstrap), this
            # login does not run bootstrap at all.
            if bootstrap_locked:
                await installation.bootstrap_initial_admin(
                    session, claims, user, settings, locked=True
                )
            secret = await sessions.create_session(
                session,
                user.id,
                settings.session_ttl_seconds,
            )
    except oidc.LoginDenied as exc:
        return callback_failure(response, "access_denied", exc)
    except Exception as exc:
        return callback_failure(response, "oidc_error", exc)
    response.headers["location"] = quote(saved.return_path, safe="/%?=&+#:;,@!$'()*[]-._~")
    response.set_cookie(
        sessions.COOKIE_NAME,
        secret,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout", status_code=204, operation_id="logoutSession")
async def logout(request: Request, principal: CurrentPrincipal) -> Response:
    if principal.kind in (PrincipalKind.SESSION, PrincipalKind.BREAK_GLASS):
        async with request.app.state.session_factory() as session, session.begin():
            await session.execute(
                update(UserSession)
                .where(UserSession.id == principal.token_id)
                .values(revoked_at=datetime.now(UTC))
            )
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if principal.kind in (PrincipalKind.SESSION, PrincipalKind.BREAK_GLASS):
        response.delete_cookie(
            sessions.COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.app.state.settings.session_cookie_secure,
        )
    return response

"""Browser login, session discovery and local logout."""

import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, or_, select, update

from lumewarp_control_plane.auth import oidc, sessions
from lumewarp_control_plane.authz import INSTALLATION, Decision, Relation
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import OIDCLoginState, User, UserSession
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
    is_superuser: bool


class SessionInfo(BaseModel):
    oidc_enabled: bool
    authenticated: bool
    user: SessionUser | None = None
    csrf_token: str | None = None


@router.get("", response_model_exclude_none=True, operation_id="getSession")
async def session_info(request: Request, response: Response) -> SessionInfo:
    response.headers["Cache-Control"] = "no-store"
    info = SessionInfo(
        oidc_enabled=bool(request.app.state.settings.oidc_issuer), authenticated=False
    )
    try:
        principal = await current_principal(request)
    except ApiError as exc:
        if exc.status != 401:
            raise
        return info
    info.authenticated = True
    if principal.user_id is not None:
        async with request.app.state.session_factory() as session:
            user = await session.get(User, principal.user_id)
            decision = await request.app.state.authorizer.check(
                str(user.id), Relation.SUPERUSER, INSTALLATION
            )
            info.user = SessionUser(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                is_superuser=(
                    user.is_superuser if decision is Decision.ERROR else decision is Decision.ALLOW
                ),
            )
    if principal.kind is PrincipalKind.SESSION:
        info.csrf_token = sessions.csrf_token(request.cookies[sessions.COOKIE_NAME])
    return info


def require_oidc(request: Request) -> None:
    if not request.app.state.settings.oidc_issuer:
        raise not_found()


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
                nonce=nonce,
                code_verifier=verifier,
                return_path=path,
                redirect_uri=redirect_uri,
                expires_at=now + timedelta(seconds=oidc.STATE_TTL),
            )
        )
    response = RedirectResponse(authorization["url"], status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        oidc.STATE_COOKIE,
        state,
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
    bound = request.cookies.get(oidc.STATE_COOKIE, "")
    response = RedirectResponse("/login?error=invalid_state", status_code=302)
    clear_state(response, request)
    if not state or not bound or not hmac.compare_digest(hash_secret(state), hash_secret(bound)):
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
            code_verifier=saved.code_verifier,
        )
        settings = request.app.state.settings
        claims = await remote.parse_id_token(
            token,
            nonce=saved.nonce,
            leeway=60,
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer},
                "aud": {"essential": True, "value": settings.oidc_client_id},
            },
        )
        if not claims or not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("missing subject")
        async with request.app.state.session_factory() as session, session.begin():
            user = await oidc.resolve_user(session, claims, request)
            secret = await sessions.create_session(session, user.id, settings.session_ttl_seconds)
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
    if principal.kind is PrincipalKind.SESSION:
        async with request.app.state.session_factory() as session, session.begin():
            await session.execute(
                update(UserSession)
                .where(UserSession.id == principal.token_id)
                .values(revoked_at=datetime.now(UTC))
            )
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if principal.kind is PrincipalKind.SESSION:
        response.delete_cookie(
            sessions.COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.app.state.settings.session_cookie_secure,
        )
    return response

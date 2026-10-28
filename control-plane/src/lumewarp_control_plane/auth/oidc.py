"""OIDC client and claim-to-user mapping. Authorization never comes from claims."""

from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import plugins
from lumewarp_control_plane.models import User, UserIdentity

STATE_COOKIE = "lumewarp_oidc_state"
STATE_TTL = 600


class LoginDenied(Exception):
    pass


def return_path(value: str) -> str:
    decoded = unquote(value)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        or urlsplit(decoded).netloc
        or urlsplit(decoded).scheme
    ):
        raise ValueError("next must be a same-site relative path")
    return value


def client(request: Request):
    state = request.app.state
    if not hasattr(state, "oidc_client"):
        settings = state.settings
        oauth = OAuth()
        state.oidc_client = oauth.register(
            "oidc",
            client_id=settings.oidc_client_id,
            client_secret=(
                settings.oidc_client_secret.get_secret_value()
                if settings.oidc_client_secret
                else None
            ),
            server_metadata_url=(
                settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            ),
            client_kwargs={
                "scope": settings.oidc_scopes,
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": (
                    "client_secret_basic" if settings.oidc_client_secret else "none"
                ),
                "timeout": 10,
            },
        )
    return state.oidc_client


async def resolve_user(session: AsyncSession, claims: dict, request: Request) -> User:
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.issuer == claims["iss"], UserIdentity.subject == claims["sub"]
        )
    )
    if identity is not None:
        user = await session.get(User, identity.user_id)
    else:
        if not hasattr(request.app.state, "auth_plugins"):
            request.app.state.auth_plugins = plugins.load(plugins.AUTH)
        loaded = request.app.state.auth_plugins
        if loaded:
            mapping = await loaded[0].plugin.map_identity(claims)
            if not isinstance(mapping, plugins.IdentityMapping):
                raise LoginDenied()
        else:
            mapping = plugins.IdentityMapping(
                email=claims.get("email", ""),
                display_name=claims.get("name") or claims.get("preferred_username") or "",
            )
        if (
            not isinstance(mapping.email, str)
            or not mapping.email
            or len(mapping.email) > 320
            or not isinstance(mapping.display_name, str)
            or len(mapping.display_name) > 200
        ):
            raise LoginDenied()
        user = await session.scalar(
            select(User).where(func.lower(User.email) == mapping.email.lower()).with_for_update()
        )
        if claims.get("email_verified") is not True:
            raise LoginDenied()
        if user is not None:
            linked = await session.scalar(
                select(UserIdentity.id).where(
                    UserIdentity.user_id == user.id, UserIdentity.issuer == claims["iss"]
                )
            )
            if user.is_superuser or linked is not None:
                raise LoginDenied()
        elif request.app.state.settings.oidc_jit_users:
            user = User(email=mapping.email, display_name=mapping.display_name, is_superuser=False)
            session.add(user)
            await session.flush()
        else:
            raise LoginDenied()
        identity = UserIdentity(
            user_id=user.id,
            issuer=claims["iss"],
            subject=claims["sub"],
            email=mapping.email,
        )
        session.add(identity)
    if user is None or not user.is_active:
        raise LoginDenied()
    identity.last_login_at = datetime.now(UTC)
    return user

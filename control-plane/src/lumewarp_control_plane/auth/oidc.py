"""OIDC client and claim-to-user mapping."""

import binascii
import json
import logging
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from authlib.integrations.starlette_client import OAuth
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import installation, plugins
from lumewarp_control_plane.models import InstallationAdmin, User, UserIdentity
from lumewarp_control_plane.settings import Settings

STATE_COOKIE = "lumewarp_oidc_state"
STATE_TTL = 600
log = logging.getLogger(__name__)

# The login-state cookie carries short-lived OIDC secrets (nonce and PKCE verifier). It is
# AES-256-GCM sealed with a key derived from LUMEWARP_SECRET_KEY, so the browser and the
# database only ever hold ciphertext or a hash of the state.
STATE_COOKIE_VERSION = "v1."
_STATE_COOKIE_AAD = b"lumewarp/oidc-login-state/v1"
_STATE_COOKIE_KEY_INFO = b"lumewarp/oidc-login-state-key/v1"
_GCM_NONCE_BYTES = 12
_GCM_KEY_BYTES = 32


class LoginStateInvalid(Exception):
    """The login-state cookie is missing or was not sealed by this server key."""


@dataclass(frozen=True)
class LoginState:
    state: str
    nonce: str
    code_verifier: str


def _login_state_key(settings: Settings) -> bytes:
    secret = settings.secret_key
    if secret is None:
        # Callers check this first to fail closed; reaching here is a programming error.
        raise LoginStateInvalid("no LUMEWARP_SECRET_KEY")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_GCM_KEY_BYTES,
        salt=None,
        info=_STATE_COOKIE_KEY_INFO,
    ).derive(secret.get_secret_value().encode())


def seal_login_state(settings: Settings, login_state: LoginState) -> str:
    """Encrypt and authenticate the cookie value; a fresh GCM nonce is used every time."""
    payload = json.dumps(
        {
            "state": login_state.state,
            "nonce": login_state.nonce,
            "code_verifier": login_state.code_verifier,
        },
        separators=(",", ":"),
    ).encode()
    iv = os.urandom(_GCM_NONCE_BYTES)
    sealed = AESGCM(_login_state_key(settings)).encrypt(iv, payload, _STATE_COOKIE_AAD)
    encoded = urlsafe_b64encode(iv + sealed).rstrip(b"=").decode("ascii")
    return STATE_COOKIE_VERSION + encoded


def open_login_state(settings: Settings, value: str) -> LoginState:
    """Decrypt and validate a login-state cookie; anything wrong raises LoginStateInvalid."""
    if not value.startswith(STATE_COOKIE_VERSION):
        raise LoginStateInvalid("unknown login-state cookie version")
    encoded = value[len(STATE_COOKIE_VERSION) :]
    try:
        raw = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error) as exc:
        raise LoginStateInvalid("invalid login-state cookie encoding") from exc
    if len(raw) <= _GCM_NONCE_BYTES:
        raise LoginStateInvalid("truncated login-state cookie")
    try:
        payload = AESGCM(_login_state_key(settings)).decrypt(
            raw[:_GCM_NONCE_BYTES], raw[_GCM_NONCE_BYTES:], _STATE_COOKIE_AAD
        )
    except (InvalidTag, ValueError) as exc:
        raise LoginStateInvalid("login-state cookie failed authentication") from exc
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise LoginStateInvalid("invalid login-state payload") from exc
    if not isinstance(data, dict) or not all(
        isinstance(data.get(field), str) and data[field]
        for field in ("state", "nonce", "code_verifier")
    ):
        raise LoginStateInvalid("invalid login-state payload")
    return LoginState(state=data["state"], nonce=data["nonce"], code_verifier=data["code_verifier"])


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
    settings = request.app.state.settings
    claim_email = claims.get("email")
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
            # A new IdP identity never takes over an account with an installation role.
            privileged = await session.scalar(
                select(InstallationAdmin.user_id).where(InstallationAdmin.user_id == user.id)
            )
            if privileged is not None or linked is not None:
                raise LoginDenied()
        elif settings.oidc_jit_users or (
            isinstance(claim_email, str)
            # The same conditions as the bootstrap: the verified raw claim email and the
            # mapped email must both be the configured initial admin, and no proxy_admin
            # may exist yet. A plugin cannot mint that account from another IdP email.
            and installation.matches_initial_admin(settings, claim_email)
            and installation.matches_initial_admin(settings, mapping.email)
            and await installation.proxy_admin_id(session) is None
        ):
            # While no proxy_admin exists, the configured initial admin may be created just
            # in time even when JIT is off; everyone else needs a pre-registered account.
            # Once a proxy_admin exists the setting has no effect, including this exception.
            user = User(email=mapping.email, display_name=mapping.display_name)
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

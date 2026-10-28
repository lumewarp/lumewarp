"""Credentials, hashing and the authenticated principal.

Secret formats (prefix + 43 chars of urlsafe base64 = 256 bits of entropy):
- `lw-...`  virtual API key, used by clients against the gateway
- `lwp-...` personal access token, admin API
- `lwg-...` gateway token, gRPC config/usage streams

Only sha256(secret) is stored. A slow password hash (argon2/bcrypt) protects low-entropy
human passwords against offline guessing; these secrets are 256-bit random values, so brute
force is infeasible regardless of hash speed, and the gateway must hash the presented key on
every request in microseconds. sha256 is also what the gateway snapshot carries
(`ApiKey.key_hash`), so CP and gateway agree on one function.

The bootstrap token (`LUMEWARP_BOOTSTRAP_TOKEN`) is an installation-level superuser that
bypasses the authorization backend. It is compared in constant time and never stored.
Authentication only establishes who the caller is; what they may do is `authz`'s job.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select, update

from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.errors import unauthorized
from lumewarp_control_plane.models import PersonalAccessToken, User
from lumewarp_control_plane.settings import Settings


class SecretKind(StrEnum):
    API_KEY = "lw-"
    PAT = "lwp-"
    GATEWAY_TOKEN = "lwg-"


SECRET_BYTES = 32
DISPLAY_CHARS = 6
# PAT last_used_at is refreshed at most this often, so reads don't write on every call.
LAST_USED_GRANULARITY = timedelta(minutes=1)


@dataclass(frozen=True)
class NewSecret:
    """A freshly generated secret. `secret` is shown once and never stored."""

    secret: str
    hash: bytes
    display_prefix: str


def hash_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def generate_secret(kind: SecretKind) -> NewSecret:
    secret = kind.value + secrets.token_urlsafe(SECRET_BYTES)
    return NewSecret(
        secret=secret,
        hash=hash_secret(secret),
        display_prefix=secret[: len(kind.value) + DISPLAY_CHARS],
    )


def secret_kind(secret: str) -> SecretKind | None:
    # Longest prefix first: "lwp-"/"lwg-" before "lw-".
    for kind in sorted(SecretKind, key=lambda k: len(k.value), reverse=True):
        if secret.startswith(kind.value):
            return kind
    return None


def parse_bearer(authorization: str | None) -> str | None:
    """Return the token from `Authorization: Bearer <token>`, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def matches_bootstrap(token: str, settings: Settings) -> bool:
    if settings.bootstrap_token is None:
        return False
    expected = settings.bootstrap_token.get_secret_value()
    if not expected:
        return False
    return hmac.compare_digest(hash_secret(token), hash_secret(expected))


class PrincipalKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    USER = "user"
    SESSION = "session"


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    # None for the bootstrap token.
    user_id: uuid.UUID | None = None
    token_id: uuid.UUID | None = None

    @property
    def is_bootstrap(self) -> bool:
        return self.kind is PrincipalKind.BOOTSTRAP

    @classmethod
    def bootstrap(cls) -> "Principal":
        return cls(kind=PrincipalKind.BOOTSTRAP)


async def authenticate(
    session_factory: SessionFactory, token: str, settings: Settings
) -> Principal | None:
    """Resolve a bearer token to a principal, or None if it is not valid.

    Uses its own short-lived session and transaction (lookup + throttled `last_used_at`
    update), committed and closed before returning, so the request session handed to the
    handler by `db.get_session` is never touched: the handler still owns its transaction.
    """
    if matches_bootstrap(token, settings):
        return Principal.bootstrap()
    if secret_kind(token) is not SecretKind.PAT:
        return None
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(PersonalAccessToken, User)
                .join(User, User.id == PersonalAccessToken.user_id)
                .where(PersonalAccessToken.token_hash == hash_secret(token))
            )
        ).one_or_none()
        if row is None:
            return None
        pat, user = row
        if pat.revoked_at is not None or not user.is_active:
            return None
        if pat.expires_at is not None and pat.expires_at <= now:
            return None
        if pat.last_used_at is None or now - pat.last_used_at > LAST_USED_GRANULARITY:
            await session.execute(
                update(PersonalAccessToken)
                .where(PersonalAccessToken.id == pat.id)
                .values(last_used_at=now)
            )
    return Principal(kind=PrincipalKind.USER, user_id=user.id, token_id=pat.id)


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency: the authenticated caller, or 401. Authentication runs in its own
    session (see `authenticate`); it does not share the request's `get_session` session.
    """
    token = parse_bearer(request.headers.get("authorization"))
    if token is None:
        from lumewarp_control_plane.auth.sessions import COOKIE_NAME, authenticate_session

        cookie = request.cookies.get(COOKIE_NAME)
        principal = (
            await authenticate_session(request.app.state.session_factory, cookie, request)
            if cookie
            else None
        )
    else:
        principal = await authenticate(
            request.app.state.session_factory, token, request.app.state.settings
        )
    if principal is None:
        raise unauthorized()
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

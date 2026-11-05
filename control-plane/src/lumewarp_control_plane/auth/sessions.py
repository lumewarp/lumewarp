"""Opaque cookie sessions; only credential digests are persisted."""

import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.auth import break_glass
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import User, UserSession
from lumewarp_control_plane.security import (
    LAST_USED_GRANULARITY,
    Principal,
    PrincipalKind,
    hash_secret,
)

COOKIE_NAME = "lumewarp_session"


def csrf_token(token: str) -> str:
    # Derivable on reload without storing either raw secret in the database.
    return hmac.digest(token.encode(), b"lumewarp-session-csrf", "sha256").hex()


async def create_session(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    ttl: int,
    *,
    kind: PrincipalKind = PrincipalKind.SESSION,
    credential_fingerprint: bytes | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            user_id=user_id,
            kind=kind.value,
            credential_fingerprint=credential_fingerprint,
            token_hash=hash_secret(token),
            csrf_hash=hash_secret(csrf_token(token)),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
    )
    return token


async def authenticate_session(
    factory: SessionFactory, token: str, request: Request
) -> Principal | None:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        row = (
            await session.execute(
                select(UserSession, User)
                .outerjoin(User, User.id == UserSession.user_id)
                .where(UserSession.token_hash == hash_secret(token))
            )
        ).one_or_none()
        if row is None:
            return None
        credential, user = row
        if credential.revoked_at or credential.expires_at <= now:
            return None
        if credential.kind == PrincipalKind.BREAK_GLASS:
            current_fingerprint = break_glass.fingerprint(request.app.state.settings)
            if (
                current_fingerprint is None
                or credential.credential_fingerprint is None
                or not hmac.compare_digest(current_fingerprint, credential.credential_fingerprint)
            ):
                return None
        if credential.kind != PrincipalKind.BREAK_GLASS and (user is None or not user.is_active):
            return None
        principal = Principal(
            kind=PrincipalKind(credential.kind),
            user_id=user.id if user else None,
            token_id=credential.id,
        )
        # Preserve the validated actor for auditing even when the CSRF check fails.
        request.state.principal = principal
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("X-Lumewarp-CSRF", "")
            if not hmac.compare_digest(hash_secret(supplied), credential.csrf_hash):
                raise ApiError(403, "csrf_failed", "missing or invalid CSRF token")
        if now - credential.last_seen_at > LAST_USED_GRANULARITY:
            await session.execute(
                update(UserSession).where(UserSession.id == credential.id).values(last_seen_at=now)
            )
        return principal

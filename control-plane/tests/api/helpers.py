"""Committed-row builders and HTTP helpers for the API tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ApiKey,
    PersonalAccessToken,
    RequestOutcome,
    ScopeType,
    UsageEvent,
    UsageState,
    UsageTotal,
    User,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from tests.factories import make_user


def auth(secret: str) -> dict[str, str]:
    if secret.startswith("session:"):
        token = secret.removeprefix("session:")
        return {
            "Cookie": f"{sessions.COOKIE_NAME}={token}",
            "X-Lumewarp-CSRF": sessions.csrf_token(token),
        }
    return {"Authorization": f"Bearer {secret}"}


ADMIN_AUTH = auth("session:test-admin-session")


def suffix() -> str:
    return uuid7().hex[-10:]


async def user_with_token(
    session: AsyncSession,
    *,
    group_admin: bool = False,
    email: str | None = None,
    active: bool = True,
) -> tuple[User, str]:
    user = await make_user(
        session,
        email=email,
        display_name="test user",
        is_active=active,
    )
    if group_admin:
        token = await sessions.create_session(session, user.id, 3600, groups=("lumewarp-admins",))
        await session.flush()
        return user, "session:" + token
    new = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="test token",
            token_hash=new.hash,
            display_prefix=new.display_prefix,
        )
    )
    await session.flush()
    return user, new.secret


def usage_event(
    key: ApiKey,
    *,
    ts: datetime | None = None,
    model: str = "mock",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_nano_usd: int = 1000,
    cost_known: bool = True,
) -> UsageEvent:
    ts = ts or datetime.now(UTC)
    return UsageEvent(
        request_id=uuid.uuid4(),
        gateway_id="gw-test",
        ts=ts,
        key_id=key.id,
        team_id=key.team_id,
        owner_kind=key.owner_kind,
        owner_id=key.owner_id,
        model=model,
        deployment="dep",
        route=model,
        status=200,
        stream=False,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_nano_usd=cost_nano_usd,
        cost_known=cost_known,
        duration_ms=10,
        config_version=1,
        usage_state=UsageState.REPORTED,
        outcome=RequestOutcome.COMPLETED,
    )


def usage_total(
    scope_type: ScopeType,
    scope_id: uuid.UUID,
    *,
    window_start: datetime,
    requests: int = 1,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_nano_usd: int = 1000,
    unknown_cost_count: int = 0,
) -> UsageTotal:
    return UsageTotal(
        scope_type=scope_type,
        scope_id=scope_id,
        window_start=window_start,
        request_count=requests,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_nano_usd=cost_nano_usd,
        unknown_cost_count=unknown_cost_count,
    )

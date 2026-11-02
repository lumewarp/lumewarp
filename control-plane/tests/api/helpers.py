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
        token = await sessions.create_session(session, user.id, 3600, admin_group="lumewarp-admins")
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
    requested: str = "mock",
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
        requested=requested,
        model="dep",
        endpoint=requested,
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


async def report_strategies(
    session,
    scope: str,
    gateway_id: str,
    strategies: dict[str, tuple[dict | str, dict | str]],
    *,
    connected: bool = False,
    last_seen_at=None,
    revoked: bool = False,
    capabilities: list[str] | None = None,
) -> uuid.UUID:
    """A gateway as Hello leaves it: a new token, the session and reports of its Hello. Returns
    the token. A strategy maps to (params schema, member schema), each a dict or the raw text
    a gateway sends.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from sqlalchemy.dialects.postgresql import insert

    from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
    from lumewarp_control_plane.compiler.strategy import (
        BUILTIN_STRATEGIES,
        normalize_hello,
        replace_gateway_strategies,
    )
    from lumewarp_control_plane.models import GatewaySession, GatewayToken
    from lumewarp_control_plane.security import SecretKind, generate_secret

    now = datetime.now(UTC)
    if capabilities is None:
        capabilities = [f"strategy:{name}" for name in (*BUILTIN_STRATEGIES, *strategies)]
    secret = generate_secret(SecretKind.GATEWAY_TOKEN)
    token = GatewayToken(
        scope=scope,
        gateway_id=gateway_id,
        name=gateway_id,
        token_hash=secret.hash,
        display_prefix=secret.display_prefix,
        revoked_at=now if revoked else None,
    )
    session.add(token)
    await session.flush()
    values = {
        "connected": connected,
        "capabilities": capabilities,
        "token_id": token.id,
        "last_seen_at": last_seen_at or now,
        "connected_at": now,
    }
    await session.execute(
        insert(GatewaySession)
        .values(scope=scope, gateway_id=gateway_id, **values)
        .on_conflict_do_update(index_elements=["scope", "gateway_id"], set_=values)
    )
    text = lambda schema: schema if isinstance(schema, str) else json.dumps(schema)  # noqa: E731
    reported = [
        cpb.StrategyDescriptor(
            name=name,
            title=name.title(),
            description="Reported.",
            params_schema_json=text(params),
            member_schema_json=text(member),
            digest=hashlib.sha256(text(params).encode() + text(member).encode()).hexdigest(),
        )
        for name, (params, member) in strategies.items()
    ]
    normalized = normalize_hello(cpb.Hello(strategies=reported))
    await replace_gateway_strategies(
        session, scope, gateway_id, token.id, normalized.strategies, now
    )
    await session.commit()
    return token.id

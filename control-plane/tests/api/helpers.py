"""Committed-row builders and HTTP helpers for the API tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ApiKey,
    Membership,
    Org,
    PersonalAccessToken,
    Project,
    RequestOutcome,
    ScopeType,
    Team,
    UsageEvent,
    UsageState,
    UsageTotal,
    User,
)
from lumewarp_control_plane.security import SecretKind, generate_secret


def auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


BOOTSTRAP_AUTH = auth("test-bootstrap-token")


def suffix() -> str:
    return uuid7().hex[-10:]


async def user_with_token(
    session: AsyncSession,
    *,
    superuser: bool = False,
    email: str | None = None,
    active: bool = True,
) -> tuple[User, str]:
    user = User(
        email=email or f"user-{suffix()}@example.com",
        display_name="test user",
        is_superuser=superuser,
        is_active=active,
    )
    session.add(user)
    await session.flush()
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


async def org_tree(session: AsyncSession, *, prefix: str = "api") -> tuple[Org, Team, Project]:
    mark = f"{prefix}-{suffix()}"
    org = Org(name=f"org-{mark}")
    session.add(org)
    await session.flush()
    team = Team(org_id=org.id, name=f"team-{mark}")
    session.add(team)
    await session.flush()
    project = Project(org_id=org.id, team_id=team.id, name=f"proj-{mark}")
    session.add(project)
    await session.flush()
    return org, team, project


async def grant(
    session: AsyncSession,
    user: User,
    org: Org,
    *,
    team: Team | None = None,
    role: str,
) -> Membership:
    row = Membership(
        user_id=user.id,
        org_id=org.id,
        team_id=team.id if team is not None else None,
        role=role,
    )
    session.add(row)
    await session.flush()
    return row


async def key_row(
    session: AsyncSession, project: Project, *, name: str = "key", **kwargs
) -> ApiKey:
    new = generate_secret(SecretKind.API_KEY)
    row = ApiKey(
        org_id=project.org_id,
        team_id=project.team_id,
        project_id=project.id,
        name=name,
        key_hash=new.hash,
        display_prefix=new.display_prefix,
        **kwargs,
    )
    session.add(row)
    await session.flush()
    return row


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
        org_id=key.org_id,
        team_id=key.team_id,
        project_id=key.project_id,
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

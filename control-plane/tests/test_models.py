import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, outbox
from lumewarp_control_plane.models import (
    ActorType,
    AuditLog,
    ConfigOutbox,
    ConfigVersion,
    ConfigVersionKind,
    Deployment,
    KeyLifecycle,
    Membership,
    ModelAccess,
    Org,
    RequestOutcome,
    Scope,
    ScopeType,
    Team,
    Upstream,
    UpstreamKind,
    UsageEvent,
    UsageState,
    UsageTotal,
)
from tests import factories


async def _fails(session: AsyncSession, *rows: object) -> None:
    """Insert `rows` inside a savepoint and expect a constraint error."""
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add_all(rows)
            await session.flush()


def _event(request_id: uuid.UUID, ids, cost: int = 100) -> dict:
    org, team, project = ids
    return dict(
        request_id=request_id,
        gateway_id="gw-1",
        ts=datetime(2026, 10, 22, tzinfo=UTC),
        key_id=uuid.uuid4(),
        org_id=org.id,
        team_id=team.id,
        project_id=project.id,
        model="m",
        deployment="d",
        route="chat_completions",
        status=200,
        stream=False,
        prompt_tokens=10,
        completion_tokens=5,
        cost_nano_usd=cost,
        cost_known=True,
        duration_ms=12,
        config_version=1,
        usage_state=UsageState.REPORTED,
        outcome=RequestOutcome.COMPLETED,
    )


# --------------------------------------------------------------------------- usage


async def test_usage_events_dedupe_on_request_id(session: AsyncSession) -> None:
    ids = await factories.hierarchy(session)
    rid = uuid.uuid4()
    stmt = (
        insert(UsageEvent)
        .values(**_event(rid, ids))
        .on_conflict_do_nothing(index_elements=["request_id"])
        .returning(UsageEvent.id)
    )
    assert (await session.execute(stmt)).scalar_one_or_none() is not None
    assert (await session.execute(stmt)).scalar_one_or_none() is None
    count = await session.scalar(
        select(text("count(*)")).select_from(UsageEvent).where(UsageEvent.request_id == rid)
    )
    assert count == 1

    await _fails(session, UsageEvent(**_event(rid, ids)))


async def test_usage_events_reject_negative(session: AsyncSession) -> None:
    ids = await factories.hierarchy(session)
    await _fails(session, UsageEvent(**_event(uuid.uuid4(), ids, cost=-1)))


async def test_usage_totals_primary_key_and_upsert(session: AsyncSession) -> None:
    scope_id = uuid.uuid4()
    window = datetime(2026, 10, 1, tzinfo=UTC)
    key = dict(scope_type=ScopeType.TEAM, scope_id=scope_id, window_start=window)

    upsert = insert(UsageTotal).values(**key, cost_nano_usd=10, request_count=1)
    upsert = upsert.on_conflict_do_update(
        index_elements=["scope_type", "scope_id", "window_start"],
        set_={
            "cost_nano_usd": UsageTotal.cost_nano_usd + upsert.excluded.cost_nano_usd,
            "request_count": UsageTotal.request_count + upsert.excluded.request_count,
        },
    )
    await session.execute(upsert)
    await session.execute(upsert)
    row = (await session.execute(select(UsageTotal).filter_by(**key))).scalar_one()
    assert (row.cost_nano_usd, row.request_count) == (20, 2)

    # Same scope id at another level or window is a separate row.
    session.add(UsageTotal(**{**key, "scope_type": ScopeType.ORG}))
    session.add(UsageTotal(**{**key, "window_start": datetime(2026, 11, 1, tzinfo=UTC)}))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert(UsageTotal).values(**key))


# --------------------------------------------------------------------------- hierarchy


async def test_org_cannot_inherit(session: AsyncSession) -> None:
    await _fails(session, Org(name="o-inherit", model_access=ModelAccess.INHERIT))


async def test_allowlist_modes(session: AsyncSession) -> None:
    org, team, project = await factories.hierarchy(session)
    # Explicit empty list = deny-all is representable and distinct from inherit/all.
    t = Team(org_id=org.id, name="deny", model_access=ModelAccess.LIST, allowed_models=[])
    session.add(t)
    await session.flush()
    assert (t.model_access, t.allowed_models) == (ModelAccess.LIST, [])
    inherit = Team(org_id=org.id, name="inherit")
    session.add(inherit)
    await session.flush()
    await session.refresh(inherit)
    assert inherit.model_access is ModelAccess.INHERIT

    await _fails(
        session, Team(org_id=org.id, name="bad", model_access=ModelAccess.ALL, allowed_models=["m"])
    )


async def test_budget_and_limit_checks(session: AsyncSession) -> None:
    org, _, _ = await factories.hierarchy(session)
    await _fails(
        session, Team(org_id=org.id, name="t1", budget_nano_usd=10, soft_budget_nano_usd=11)
    )
    await _fails(session, Team(org_id=org.id, name="t2", rpm_limit=0))
    await _fails(session, Team(org_id=org.id, name="t3", budget_nano_usd=-1))


async def test_key_must_match_hierarchy(session: AsyncSession) -> None:
    _, _, project = await factories.hierarchy(session)
    _, other_team, _ = await factories.hierarchy(session)
    key = factories.api_key(project)
    key.team_id = other_team.id
    await _fails(session, key)


async def test_key_hash_and_lifecycle_checks(session: AsyncSession) -> None:
    _, _, project = await factories.hierarchy(session)
    await _fails(session, factories.api_key(project, key_hash=b"\x00" * 31))
    await _fails(session, factories.api_key(project, lifecycle=KeyLifecycle.REVOKED))
    same = hashlib.sha256(b"x").digest()
    session.add(factories.api_key(project, key_hash=same))
    await session.flush()
    await _fails(session, factories.api_key(project, key_hash=same))


async def test_membership_role_levels(session: AsyncSession, engine) -> None:
    from lumewarp_control_plane.models import User

    org, team, _ = await factories.hierarchy(session)
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    session.add(user)
    await session.flush()
    session.add(Membership(user_id=user.id, org_id=org.id, role="owner"))
    session.add(Membership(user_id=user.id, org_id=org.id, team_id=team.id, role="member"))
    await session.flush()

    await _fails(session, Membership(user_id=user.id, org_id=org.id, team_id=team.id, role="owner"))
    await _fails(session, Membership(user_id=user.id, org_id=org.id, role="viewer"))
    # One org-level grant per user and org.
    await _fails(session, Membership(user_id=user.id, org_id=org.id, role="admin"))
    # Team must belong to the org.
    other_org, _, _ = await factories.hierarchy(session)
    await _fails(
        session, Membership(user_id=user.id, org_id=other_org.id, team_id=team.id, role="viewer")
    )


async def test_user_email_case_insensitive_unique(session: AsyncSession) -> None:
    from lumewarp_control_plane.models import User

    session.add(User(email="Andy@Example.com"))
    await session.flush()
    await _fails(session, User(email="andy@example.com"))


# --------------------------------------------------------------------------- routing


async def test_routing_checks(session: AsyncSession) -> None:
    up = Upstream(name="u", kind=UpstreamKind.VLLM, base_url="http://x/v1", api_key_ref="VLLM_KEY")
    session.add(up)
    await session.flush()
    await _fails(
        session,
        Deployment(
            name="d1", upstream_id=up.id, upstream_model="m", price_input_nano_usd_per_mtok=1
        ),
    )
    await _fails(
        session, Upstream(name="u2", kind=UpstreamKind.OPENAI, base_url="x", api_key_ref="$(rm)")
    )


# --------------------------------------------------------------------------- config


async def _version(session: AsyncSession, version: int = 1) -> ConfigVersion:
    payload = b"snapshot"
    row = ConfigVersion(
        scope="default",
        version=version,
        payload=payload,
        digest=hashlib.sha256(payload).digest(),
        size_bytes=len(payload),
        key_count=0,
        source_revision=version,
        kind=ConfigVersionKind.COMPILE,
    )
    session.add(row)
    await session.flush()
    return row


async def test_config_versions_immutable(session: AsyncSession) -> None:
    await factories.scope(session)
    await _version(session)
    where = (ConfigVersion.scope == "default") & (ConfigVersion.version == 1)
    for values in ({"payload": b"other"}, {"source_revision": 9}, {"digest": b"\x01" * 32}):
        with pytest.raises(DBAPIError, match="immutable"):
            async with session.begin_nested():
                await session.execute(update(ConfigVersion).where(where).values(**values))

    # Compaction is the one allowed update.
    await session.execute(
        update(ConfigVersion).where(where).values(payload=None, compacted_at=datetime.now(UTC))
    )
    # payload NULL without compacted_at violates the CHECK.
    await _version(session, 2)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                update(ConfigVersion)
                .where((ConfigVersion.scope == "default") & (ConfigVersion.version == 2))
                .values(payload=None)
            )


async def test_enqueue_recompile(session: AsyncSession) -> None:
    await factories.scope(session)
    r1 = await outbox.enqueue_recompile(session, "default", "org.create")
    r2 = await outbox.enqueue_recompile(session, "default", "key.revoke")
    assert r2 == r1 + 1
    rows = (await session.execute(select(ConfigOutbox).order_by(ConfigOutbox.id))).scalars().all()
    assert [(r.reason, r.source_revision, r.status.value) for r in rows] == [
        ("org.create", r1, "pending"),
        ("key.revoke", r2, "pending"),
    ]
    assert (await session.get(Scope, "default")).source_revision == r2
    with pytest.raises(LookupError):
        await outbox.enqueue_recompile(session, "nope", "x")


async def test_audit_redacts_secrets(session: AsyncSession) -> None:
    row = audit.record(
        session,
        action="key.create",
        resource_type="api_key",
        resource_id=uuid.uuid4(),
        actor_type=ActorType.SYSTEM,
        actor_id="test",
        details={
            "name": "k",
            "secret": "lw-abc",
            "nested": {"Authorization": "Bearer x", "api_key_ref": "OPENAI_KEY"},
            "items": [{"token": "t"}],
        },
    )
    await session.flush()
    stored = await session.get(AuditLog, row.id)
    assert stored.details == {
        "name": "k",
        "secret": "[redacted]",
        "nested": {"Authorization": "[redacted]", "api_key_ref": "OPENAI_KEY"},
        "items": [{"token": "[redacted]"}],
    }

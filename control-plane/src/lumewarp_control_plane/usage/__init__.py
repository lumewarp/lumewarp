"""Usage ingestion and monthly budget evaluation.

A report is validated before any writes, then inserted idempotently with its aggregate
updates, budget transitions, audit rows and outbox work in one caller-owned transaction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, outbox
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import (
    ActorType,
    ApiKey,
    Org,
    Project,
    RequestOutcome,
    ScopeType,
    Team,
    UsageEvent,
    UsageState,
    UsageTotal,
)
from lumewarp_control_plane.settings import Settings, get_settings
from lumewarp_control_plane.windows import month_window

log = logging.getLogger(__name__)
_MAX_BIGINT = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# asyncpg caps one statement at 32767 bind parameters; 21 columns per event row.
_INSERT_CHUNK_EVENTS = 1000
_USAGE_FIELDS = (
    "request_id",
    "gateway_id",
    "ts",
    "started_at",
    "key_id",
    "org_id",
    "team_id",
    "project_id",
    "model",
    "deployment",
    "route",
    "status",
    "stream",
    "prompt_tokens",
    "completion_tokens",
    "cost_nano_usd",
    "cost_known",
    "duration_ms",
    "config_version",
    "usage_state",
    "outcome",
)


class InvalidUsageBatch(ValueError):
    """A malformed or inconsistent report; no part of the batch may be committed."""


@dataclass(frozen=True)
class _PreparedEvent:
    request_id: uuid.UUID
    values: dict[str, object]


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidUsageBatch(f"invalid {field}") from exc


def _timestamp(ms: int, field: str, *, optional: bool = False) -> datetime | None:
    if optional and ms == 0:
        return None
    if not isinstance(ms, int) or ms <= 0:
        raise InvalidUsageBatch(f"invalid {field}")
    try:
        return _EPOCH + timedelta(milliseconds=ms)
    except OverflowError as exc:
        raise InvalidUsageBatch(f"invalid {field}") from exc


def _bounded_int(value: int, field: str, *, maximum: int = _MAX_BIGINT) -> int:
    if not isinstance(value, int) or value < 0 or value > maximum:
        raise InvalidUsageBatch(f"invalid {field}")
    return value


def _prepare_event(gateway_id: str, event: usage_pb2.UsageEvent) -> _PreparedEvent:
    if not event.request_id:
        raise InvalidUsageBatch("request_id is required")
    request_id = _uuid(event.request_id, "request_id")
    key_id = _uuid(event.key_id, "key_id")
    org_id = _uuid(event.org_id, "org_id")
    team_id = _uuid(event.team_id, "team_id")
    project_id = _uuid(event.project_id, "project_id")
    # A rejected request may never reach routing: authentication runs before the body is
    # read (no model) and policy rejections happen before a deployment is chosen.
    rejected = event.outcome == usage_pb2.REQUEST_OUTCOME_REJECTED
    if len(event.model) > 400 or not (event.model or rejected):
        raise InvalidUsageBatch("invalid model")
    if len(event.deployment) > 200 or not (event.deployment or rejected):
        raise InvalidUsageBatch("invalid deployment")
    if not event.route or len(event.route) > 64:
        raise InvalidUsageBatch("invalid route")
    if event.status > 599:
        raise InvalidUsageBatch("invalid status")
    if event.duration_ms > 2**31 - 1:
        raise InvalidUsageBatch("invalid duration_ms")
    if event.config_version > _MAX_BIGINT:
        raise InvalidUsageBatch("invalid config_version")

    state_map = {
        usage_pb2.USAGE_STATE_REPORTED: UsageState.REPORTED,
        usage_pb2.USAGE_STATE_MISSING: UsageState.MISSING,
        usage_pb2.USAGE_STATE_PARTIAL: UsageState.PARTIAL,
        usage_pb2.USAGE_STATE_INVALID: UsageState.INVALID,
    }
    outcome_map = {
        usage_pb2.REQUEST_OUTCOME_COMPLETED: RequestOutcome.COMPLETED,
        usage_pb2.REQUEST_OUTCOME_CLIENT_CANCELLED: RequestOutcome.CLIENT_CANCELLED,
        usage_pb2.REQUEST_OUTCOME_UPSTREAM_ERROR: RequestOutcome.UPSTREAM_ERROR,
        usage_pb2.REQUEST_OUTCOME_REJECTED: RequestOutcome.REJECTED,
    }
    try:
        usage_state = state_map[event.usage_state]
        outcome = outcome_map[event.outcome]
    except KeyError as exc:
        raise InvalidUsageBatch("usage_state and outcome must be specified") from exc

    prompt_tokens = _bounded_int(event.prompt_tokens, "prompt_tokens")
    completion_tokens = _bounded_int(event.completion_tokens, "completion_tokens")
    cost = _bounded_int(event.cost_nano_usd, "cost_nano_usd")
    if event.cost_known and usage_state is not UsageState.REPORTED:
        raise InvalidUsageBatch("known cost requires reported usage")
    if not event.cost_known and cost != 0:
        raise InvalidUsageBatch("unknown cost must be zero")

    values: dict[str, object] = {
        "request_id": request_id,
        "gateway_id": gateway_id,
        "ts": _timestamp(event.timestamp_unix_ms, "timestamp_unix_ms"),
        "started_at": _timestamp(event.started_at_unix_ms, "started_at_unix_ms", optional=True),
        "key_id": key_id,
        "org_id": org_id,
        "team_id": team_id,
        "project_id": project_id,
        "model": event.model,
        "deployment": event.deployment,
        "route": event.route,
        "status": event.status,
        "stream": event.stream,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_nano_usd": cost,
        "cost_known": event.cost_known,
        "duration_ms": event.duration_ms,
        "config_version": event.config_version,
        "usage_state": usage_state,
        "outcome": outcome,
    }
    return _PreparedEvent(request_id=request_id, values=values)


def _same_event(existing: UsageEvent, values: dict[str, object]) -> bool:
    return all(getattr(existing, field) == values[field] for field in _USAGE_FIELDS)


def _add_totals(
    totals: dict[tuple[ScopeType, uuid.UUID, datetime], list[int]],
    values: dict[str, object],
) -> None:
    start, _ = month_window(values["ts"])  # type: ignore[arg-type]
    amounts = (
        int(values["cost_nano_usd"]),
        int(values["prompt_tokens"]),
        int(values["completion_tokens"]),
        1,
        int(not bool(values["cost_known"])),
    )
    for kind, field in (
        (ScopeType.KEY, "key_id"),
        (ScopeType.PROJECT, "project_id"),
        (ScopeType.TEAM, "team_id"),
        (ScopeType.ORG, "org_id"),
    ):
        key = (kind, values[field], start)  # type: ignore[arg-type]
        bucket = totals.setdefault(key, [0, 0, 0, 0, 0])
        for i, amount in enumerate(amounts):
            bucket[i] += amount
            if bucket[i] > _MAX_BIGINT:
                raise InvalidUsageBatch("aggregate value exceeds database range")


async def _validate_key_hierarchy(
    session: AsyncSession, prepared: Sequence[_PreparedEvent]
) -> tuple[set[uuid.UUID], dict[uuid.UUID, ApiKey]]:
    """Return the request ids whose key hierarchy does not match api_keys, and the keys."""
    key_ids = {item.values["key_id"] for item in prepared}
    if not key_ids:
        return set(), {}
    keys = (await session.execute(select(ApiKey).where(ApiKey.id.in_(key_ids)))).scalars().all()
    by_id = {key.id: key for key in keys}
    mismatched: set[uuid.UUID] = set()
    for item in prepared:
        values = item.values
        key = by_id.get(values["key_id"])  # type: ignore[arg-type]
        if key is None or any(
            getattr(key, name) != values[name] for name in ("org_id", "team_id", "project_id")
        ):
            mismatched.add(item.request_id)
    return mismatched, by_id


async def _upsert_totals(
    session: AsyncSession,
    aggregates: dict[tuple[ScopeType, uuid.UUID, datetime], list[int]],
) -> tuple[list[UsageTotal], dict[tuple[ScopeType, uuid.UUID, datetime], int]]:
    # Create and lock aggregate rows in one stable total order. The previous spend is kept
    # under the same row lock so budget threshold crossings remain correct after a policy
    # budget is raised within a month.
    keys = sorted(aggregates, key=lambda k: (k[2], k[0].value, k[1].hex))
    rows: list[UsageTotal] = []
    previous_costs: dict[tuple[ScopeType, uuid.UUID, datetime], int] = {}
    now = datetime.now(UTC)
    for scope_type, scope_id, window_start in keys:
        key = (scope_type, scope_id, window_start)
        ensure = pg_insert(UsageTotal).values(
            scope_type=scope_type,
            scope_id=scope_id,
            window_start=window_start,
        )
        await session.execute(
            ensure.on_conflict_do_nothing(
                index_elements=[UsageTotal.scope_type, UsageTotal.scope_id, UsageTotal.window_start]
            )
        )
        total = (
            await session.execute(
                select(UsageTotal)
                .where(
                    UsageTotal.scope_type == scope_type,
                    UsageTotal.scope_id == scope_id,
                    UsageTotal.window_start == window_start,
                )
                .with_for_update()
            )
        ).scalar_one()
        previous_costs[key] = total.cost_nano_usd
        cost, prompt, completion, requests, unknown = aggregates[key]
        for field, increment in (
            ("cost_nano_usd", cost),
            ("prompt_tokens", prompt),
            ("completion_tokens", completion),
            ("request_count", requests),
            ("unknown_cost_count", unknown),
        ):
            updated = getattr(total, field) + increment
            if updated > _MAX_BIGINT:
                raise InvalidUsageBatch("aggregate value exceeds database range")
            setattr(total, field, updated)
        total.updated_at = now
        rows.append(total)
    await session.flush()
    return rows, previous_costs


async def _budget_policy(
    session: AsyncSession,
    keys: dict[uuid.UUID, ApiKey],
    aggregates: dict[tuple[ScopeType, uuid.UUID, datetime], list[int]],
):
    project_ids = {key.project_id for key in keys.values()}
    team_ids = {key.team_id for key in keys.values()}
    org_ids = {key.org_id for key in keys.values()}
    policies: dict[tuple[ScopeType, uuid.UUID], tuple[int | None, int | None, uuid.UUID]] = {}
    for model, kind, ids in (
        (ApiKey, ScopeType.KEY, set(keys)),
        (Project, ScopeType.PROJECT, project_ids),
        (Team, ScopeType.TEAM, team_ids),
        (Org, ScopeType.ORG, org_ids),
    ):
        if not ids:
            continue
        org_column = model.id if model is Org else model.org_id
        result = await session.execute(
            select(model.id, org_column, model.budget_nano_usd, model.soft_budget_nano_usd).where(
                model.id.in_(ids)
            )
        )
        for entity_id, org_id, hard, soft in result:
            policies[(kind, entity_id)] = (hard, soft, org_id)
    # Make missing rows a consistency failure rather than silently dropping an aggregate.
    for kind, scope_id, _ in aggregates:
        if (kind, scope_id) not in policies:
            raise RuntimeError(
                f"budget scope disappeared during usage ingestion: {kind}:{scope_id}"
            )
    return policies


async def _evaluate_budgets(
    session: AsyncSession,
    gateway_id: str,
    aggregate_rows: Sequence[UsageTotal],
    aggregates: dict[tuple[ScopeType, uuid.UUID, datetime], list[int]],
    previous_costs: dict[tuple[ScopeType, uuid.UUID, datetime], int],
    policies: dict[tuple[ScopeType, uuid.UUID], tuple[int | None, int | None, uuid.UUID]],
) -> bool:
    from lumewarp_control_plane.notifier import BudgetEvent, enqueue_budget_event

    now = datetime.now(UTC)
    current_window = month_window(now)[0]
    hard_current_month_exceeded = False
    for total in aggregate_rows:
        hard, soft, org_id = policies[(total.scope_type, total.scope_id)]
        window_start = total.window_start
        previous_cost = previous_costs[(total.scope_type, total.scope_id, window_start)]
        hard_crossed = (
            hard is not None and total.cost_nano_usd >= hard and total.exceeded_at is None
        )
        # A zero budget is exceeded before any spend, so it can never be "crossed" by cost;
        # it counts once, when the window is first marked exceeded.
        hard_threshold_crossed = (
            hard is not None and previous_cost < hard <= total.cost_nano_usd
        ) or (hard == 0 and hard_crossed)
        soft_crossed = (
            soft is not None and total.cost_nano_usd >= soft and total.soft_notified_at is None
        )
        current = window_start == current_window
        if current and hard_threshold_crossed:
            hard_current_month_exceeded = True
        if not hard_crossed and not soft_crossed:
            continue
        if hard_crossed:
            total.exceeded_at = now
            audit.record(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=f"usage:{gateway_id}",
                action="budget.hard_exceeded",
                resource_type=total.scope_type.value,
                resource_id=total.scope_id,
                org_id=org_id,
                details={
                    "window_start": window_start.isoformat(),
                    "spent_nano_usd": total.cost_nano_usd,
                    "budget_nano_usd": hard,
                },
            )
            if current:
                # Late events for a closed month still mark and audit it, but a
                # "budget exceeded" notification for last month after rollover only confuses.
                await enqueue_budget_event(
                    session,
                    BudgetEvent(
                        kind="hard",
                        scope_type=total.scope_type,
                        scope_id=total.scope_id,
                        org_id=org_id,
                        window_start=window_start,
                        spent_nano_usd=total.cost_nano_usd,
                        budget_nano_usd=hard,
                    ),
                )
        if soft_crossed:
            total.soft_notified_at = now
            if current:
                await enqueue_budget_event(
                    session,
                    BudgetEvent(
                        kind="soft",
                        scope_type=total.scope_type,
                        scope_id=total.scope_id,
                        org_id=org_id,
                        window_start=window_start,
                        spent_nano_usd=total.cost_nano_usd,
                        budget_nano_usd=soft,
                    ),
                )
    return hard_current_month_exceeded


async def _persist_events(
    session: AsyncSession,
    gateway_id: str,
    by_id: dict[uuid.UUID, _PreparedEvent],
    keys: dict[uuid.UUID, ApiKey],
    accepted: int,
    scope_name: str | None,
    rejected: Counter[str],
) -> int:
    # Insert in request_id order so overlapping batches take event row locks in one order.
    ordered = [by_id[request_id] for request_id in sorted(by_id)]
    inserted_ids: set[uuid.UUID] = set()
    for start in range(0, len(ordered), _INSERT_CHUNK_EVENTS):
        chunk = ordered[start : start + _INSERT_CHUNK_EVENTS]
        insert_stmt = pg_insert(UsageEvent).values([item.values for item in chunk])
        inserted_ids.update(
            (
                await session.execute(
                    insert_stmt.on_conflict_do_nothing(
                        index_elements=[UsageEvent.request_id]
                    ).returning(UsageEvent.request_id)
                )
            ).scalars()
        )
    duplicates = set(by_id) - inserted_ids
    if duplicates:
        existing_rows = (
            (await session.execute(select(UsageEvent).where(UsageEvent.request_id.in_(duplicates))))
            .scalars()
            .all()
        )
        existing_by_id = {row.request_id: row for row in existing_rows}
        for request_id in duplicates:
            existing = existing_by_id.get(request_id)
            prepared = by_id[request_id]
            if existing is None or not _same_event(existing, prepared.values):
                # Nothing was inserted for it (ON CONFLICT DO NOTHING); the stored row wins.
                rejected["request_id conflicts with a different stored event"] += 1
                accepted -= 1

    aggregates: dict[tuple[ScopeType, uuid.UUID, datetime], list[int]] = {}
    for request_id in inserted_ids:
        _add_totals(aggregates, by_id[request_id].values)
    if not aggregates:
        return accepted

    total_rows, previous_costs = await _upsert_totals(session, aggregates)
    policies = await _budget_policy(session, keys, aggregates)
    hard_flipped = await _evaluate_budgets(
        session, gateway_id, total_rows, aggregates, previous_costs, policies
    )
    if hard_flipped:
        await outbox.enqueue_recompile(
            session, scope_name or get_settings().scope, "usage budget exceeded"
        )
    return accepted


async def ingest(
    session: AsyncSession,
    gateway_id: str,
    events: Sequence[usage_pb2.UsageEvent],
    *,
    scope_name: str | None = None,
    max_batch_events: int | None = None,
) -> int:
    """Record a batch in the caller's transaction and return its accepted count.

    Validation is per event: an invalid event (bad fields, unknown or hierarchy-mismatched
    key, a request id reused for different content within the batch or against a stored
    event) is skipped and logged, and never costs the rest of the batch. A duplicate request
    id with an identical payload is accepted without re-counting. Only batch-level problems
    (gateway identity, batch size) reject the whole batch. Event timestamps, not receipt
    time, select the UTC month.
    """
    if not isinstance(gateway_id, str) or not gateway_id or len(gateway_id) > 200:
        raise InvalidUsageBatch("invalid gateway identity")
    maximum = (
        get_settings().max_usage_batch_events if max_batch_events is None else max_batch_events
    )
    if len(events) > maximum:
        raise InvalidUsageBatch("usage batch exceeds configured maximum")

    rejected: Counter[str] = Counter()
    by_id: dict[uuid.UUID, _PreparedEvent] = {}
    duplicates: Counter[uuid.UUID] = Counter()
    for event in events:
        try:
            prepared = _prepare_event(gateway_id, event)
        except InvalidUsageBatch as exc:
            rejected[str(exc)] += 1
            continue
        previous = by_id.get(prepared.request_id)
        if previous is None:
            by_id[prepared.request_id] = prepared
        elif any(previous.values[k] != prepared.values[k] for k in _USAGE_FIELDS):
            # The first occurrence is kept; the conflicting copy is dropped.
            rejected["request_id reused with different event data"] += 1
        else:
            duplicates[prepared.request_id] += 1

    mismatched, keys = await _validate_key_hierarchy(session, list(by_id.values()))
    for request_id in mismatched:
        # Identical in-batch copies of a rejected event are rejected with it.
        rejected["usage key hierarchy does not match api_keys"] += 1 + duplicates[request_id]
        del by_id[request_id]

    try:
        if not by_id:
            return 0
        # The savepoint keeps a failed persist from leaving partial usage writes if the
        # caller catches the exception and continues its outer transaction.
        async with session.begin_nested():
            return await _persist_events(
                session,
                gateway_id,
                by_id,
                keys,
                len(events) - rejected.total(),
                scope_name,
                rejected,
            )
    finally:
        if rejected:
            log.warning(
                "usage report from gateway %s: skipped %d of %d events: %s",
                gateway_id,
                rejected.total(),
                len(events),
                dict(rejected),
            )


async def _rollover_once(settings: Settings, session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await outbox.enqueue_recompile(session, settings.scope, "monthly budget rollover")
        audit.record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="budget-rollover",
            action="budget.monthly_rollover",
            resource_type="scope",
            resource_id=settings.scope,
            details={"window_start": month_window(datetime.now(UTC))[0].isoformat()},
        )


async def rollover_scheduler(settings: Settings, session_factory: SessionFactory) -> None:
    """Recompile at startup and each UTC month boundary, retrying transient DB failures."""
    while True:
        try:
            await _rollover_once(settings, session_factory)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("monthly budget rollover failed; retrying")
            await asyncio.sleep(1)

    while True:
        boundary = month_window(datetime.now(UTC))[1]
        delay = max(0.0, (boundary - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        while True:
            try:
                await _rollover_once(settings, session_factory)
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monthly budget rollover failed; retrying")
                await asyncio.sleep(1)

"""Suspend active user keys after a confirmed loss of key ownership; never auto-resume.

A user may own keys in a team while they are a direct member, an installation admin or an
admin of the team's org (`keys.owners`).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import enqueue_recompile, lock_scope
from lumewarp_control_plane.authz import Authorizer, Decision
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.keys.owners import key_owner_decisions
from lumewarp_control_plane.metrics import DISCARD, Metrics
from lumewarp_control_plane.models import ActorType, ApiKey, KeyLifecycle, KeyOwnerKind, User
from lumewarp_control_plane.settings import Settings

log = logging.getLogger(__name__)
CHECK_SLICE_SIZE = 500


async def reconcile_keys(
    session_factory: SessionFactory,
    authorizer: Authorizer,
    settings: Settings,
    *,
    user_ids: Collection[str] | None = None,
    metrics: Metrics = DISCARD,
) -> int:
    """Check distinct active owners outside write transactions, then lock scope before keys.

    Local inactivity independently confirms owner removal, even if IAM is unavailable.
    Both local inactivity and an explicit IAM DENY use the reason ``owner_removed``.
    Returns the number of keys suspended.
    """
    suspended, _ = await _reconcile(
        session_factory, authorizer, settings, user_ids=user_ids, metrics=metrics
    )
    return suspended


async def _reconcile(
    session_factory: SessionFactory,
    authorizer: Authorizer,
    settings: Settings,
    *,
    user_ids: Collection[str] | None,
    metrics: Metrics,
) -> tuple[int, int]:
    """`reconcile_keys`, also returning how many owner checks ended in Decision.ERROR."""
    active = (
        ApiKey.owner_kind == KeyOwnerKind.USER,
        ApiKey.lifecycle == KeyLifecycle.ACTIVE,
    )
    stmt = (
        select(ApiKey.user_id, ApiKey.team_id, User.is_active)
        .join(User, User.id == ApiKey.user_id)
        .where(*active)
        .distinct()
        .order_by(ApiKey.user_id, ApiKey.team_id)
    )
    if user_ids is not None:
        valid_ids = []
        for user_id in user_ids:
            try:
                valid_ids.append(uuid.UUID(user_id))
            except (ValueError, TypeError, AttributeError):
                log.debug("ignoring reconciliation change with invalid user id %r", user_id)
        if not valid_ids:
            return 0, 0
        stmt = stmt.where(ApiKey.user_id.in_(valid_ids))
    async with session_factory() as session:
        pairs = (await session.execute(stmt)).all()
    if not pairs:
        return 0, 0
    removed = []
    errors = 0
    for start in range(0, len(pairs), CHECK_SLICE_SIZE):
        batch = pairs[start : start + CHECK_SLICE_SIZE]
        async with session_factory() as session:
            decisions = await key_owner_decisions(
                session, authorizer, [(user, team) for user, team, _ in batch]
            )
        decided_at = datetime.now(UTC)
        for (user, team, is_active), decision in zip(batch, decisions, strict=True):
            if not is_active or decision is Decision.DENY:
                removed.append((user, team, decided_at))
            elif decision is Decision.ERROR:
                errors += 1
                log.warning(
                    "key reconciliation: authorization unavailable for user %s team %s", user, team
                )
    if not removed:
        return 0, errors
    suspended = 0
    async with session_factory() as session, session.begin():
        await lock_scope(session, settings)
        for user, team, decided_at in removed:
            keys = await session.scalars(
                select(ApiKey)
                .where(
                    *active,
                    ApiKey.user_id == user,
                    ApiKey.team_id == team,
                    ApiKey.created_at < decided_at,
                )
                .order_by(ApiKey.id)
                .with_for_update()
            )
            for key in keys:
                key.lifecycle = KeyLifecycle.SUSPENDED
                key.suspended_reason = "owner_removed"
                audit.record(
                    session,
                    action="key.suspend",
                    resource_type="key",
                    resource_id=key.id,
                    actor_type=ActorType.SYSTEM,
                    actor_id="key_reconciler",
                    team_id=key.team_id,
                    details={"name": key.name, "user_id": user, "reason": "owner_removed"},
                )
                suspended += 1
        if suspended:
            await enqueue_recompile(session, settings, "key.reconcile")
    metrics.record_suspended("owner_removed", suspended)
    return suspended, errors


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_key_reconciler(
    session_factory: SessionFactory,
    authorizer: Authorizer,
    stop: asyncio.Event,
    *,
    poll_seconds: float,
    full_seconds: float,
    settings: Settings,
    metrics: Metrics = DISCARD,
) -> None:
    """Scan at startup and periodically; poll optional changes without losing failed cursors.

    A cursor advances only after the corresponding scan commits. Full-scan deadlines are
    independent of changes errors. Unexpected failures retry with bounded exponential backoff.

    `lumewarp_cp_key_reconcile_last_success_timestamp_seconds` is set after a step that
    raised nothing, got a decision for every owner it checked and, for a backend with
    `changes()`, could read the change stream.
    """
    cursor = None
    next_full = 0.0
    backoff = poll_seconds
    while not stop.is_set():
        try:
            batch = await authorizer.changes(cursor) if authorizer.has_changes else None
            full = monotonic() >= next_full or (batch is not None and batch.reset)
            errors = 0
            if full:
                _, errors = await _reconcile(
                    session_factory, authorizer, settings, user_ids=None, metrics=metrics
                )
                next_full = monotonic() + full_seconds
            elif batch is not None and batch.user_ids:
                _, errors = await _reconcile(
                    session_factory,
                    authorizer,
                    settings,
                    user_ids=batch.user_ids,
                    metrics=metrics,
                )
            if batch is not None:
                cursor = batch.cursor
            if not errors and (batch is not None or not authorizer.has_changes):
                metrics.key_reconcile_last_success.set(time.time())
            backoff = poll_seconds
            delay = max(0.0, next_full - monotonic())
            if authorizer.has_changes:
                delay = min(poll_seconds, delay)
        except Exception:
            log.exception("key reconciler step failed")
            backoff = min(backoff * 2, 5.0)
            delay = backoff
        await _wait(stop, delay)

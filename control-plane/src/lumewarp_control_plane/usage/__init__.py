"""Usage ingestion and budgets (owner: C4).

Behaviour is specified by docs/config-protocol.md ("Usage reporting").
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.settings import Settings


class InvalidUsageBatch(ValueError):
    """The batch is malformed (bad request_id, unknown enum, negative values, more than
    settings.max_usage_batch_events). Nothing is recorded; maps to INVALID_ARGUMENT.
    """


async def ingest(
    session: AsyncSession, gateway_id: str, events: Sequence[usage_pb2.UsageEvent]
) -> int:
    """Record a batch, all-or-nothing, in the caller's transaction.

    - Validates every event first (InvalidUsageBatch).
    - INSERT ... ON CONFLICT (request_id) DO NOTHING; only newly inserted events add their
      cost/tokens once to the key, project, team and org `usage_totals` rows of the event's
      UTC month (`windows.month_window(ts)`), locking totals rows in a fixed order.
    - Evaluates budgets for touched scopes: a scope crossing its hard budget flips its keys
      to BUDGET_EXCEEDED via `outbox.enqueue_recompile`; crossing the soft budget enqueues a
      `notifier_outbox` row (deduped). Sets exceeded_at / soft_notified_at.
    Returns len(events) (already-seen request_ids count as accepted).
    """
    raise NotImplementedError


async def rollover_scheduler(settings: Settings, session_factory: SessionFactory) -> None:
    """Run forever: at startup and at each UTC month boundary, enqueue a recompile so keys
    blocked only by last month's budget are unblocked (and audit it). Never driven by
    incoming usage.
    """
    raise NotImplementedError

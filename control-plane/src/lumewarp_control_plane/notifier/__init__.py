"""Budget notification outbox and notifier implementations.

Notifications are queued in the usage transaction. This worker performs all external I/O,
so a slow or broken destination can never block usage accounting or gateway requests.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import (
    NotifierChannel,
    NotifierOutbox,
    OutboxStatus,
    ScopeType,
)

log = logging.getLogger(__name__)
_MAX_RETRY_SECONDS = 60 * 60
_PLUGIN_TIMEOUT_SECONDS = 10.0
_BATCH_SIZE = 100


@dataclass(frozen=True)
class BudgetEvent:
    kind: Literal["soft", "hard"]
    scope_type: ScopeType
    scope_id: uuid.UUID
    org_id: uuid.UUID
    window_start: datetime
    spent_nano_usd: int
    budget_nano_usd: int

    def to_payload(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "scope_type": self.scope_type.value,
            "scope_id": str(self.scope_id),
            "org_id": str(self.org_id),
            "window_start": self.window_start.astimezone(UTC).isoformat(),
            "spent_nano_usd": self.spent_nano_usd,
            "budget_nano_usd": self.budget_nano_usd,
        }


class NotifierSink(Protocol):
    async def notify(self, event: BudgetEvent) -> None:
        """Deliver one event. Raise to have the outbox retry with backoff."""
        ...


class WebhookNotifier:
    """POST one budget event as JSON to an HTTP(S) webhook endpoint."""

    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError("webhook URL must use http or https")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def notify(self, event: BudgetEvent) -> None:
        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.url, json=event.to_payload())
            response.raise_for_status()


def _dedupe_key(event: BudgetEvent, channel_id: uuid.UUID) -> str:
    month = event.window_start.astimezone(UTC).strftime("%Y-%m")
    return f"budget.{event.kind}:{event.scope_type.value}:{event.scope_id}:{month}:{channel_id}"


async def enqueue_budget_event(session, event: BudgetEvent) -> int:
    """Add one deduped outbox item per applicable enabled channel in the same transaction."""
    channels = (
        (
            await session.execute(
                select(NotifierChannel).where(
                    NotifierChannel.enabled.is_(True),
                    (NotifierChannel.org_id.is_(None) | (NotifierChannel.org_id == event.org_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    inserted = 0
    for channel in channels:
        stmt = pg_insert(NotifierOutbox).values(
            dedupe_key=_dedupe_key(event, channel.id),
            channel_id=channel.id,
            event_type=f"budget.{event.kind}",
            payload=event.to_payload(),
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        result = await session.execute(
            stmt.on_conflict_do_nothing(index_elements=[NotifierOutbox.dedupe_key])
        )
        inserted += result.rowcount or 0
    return inserted


def _event_from_payload(payload: dict) -> BudgetEvent:
    try:
        kind = payload["kind"]
        if kind not in ("soft", "hard"):
            raise ValueError("invalid notification kind")
        window_start = datetime.fromisoformat(payload["window_start"])
        if window_start.tzinfo is None:
            raise ValueError("naive window_start")
        return BudgetEvent(
            kind=kind,
            scope_type=ScopeType(payload["scope_type"]),
            scope_id=uuid.UUID(payload["scope_id"]),
            org_id=uuid.UUID(payload["org_id"]),
            window_start=window_start.astimezone(UTC),
            spent_nano_usd=int(payload["spent_nano_usd"]),
            budget_nano_usd=int(payload["budget_nano_usd"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid budget notification payload") from exc


async def _deliver(
    channel: NotifierChannel, event: BudgetEvent, plugins: dict[str, object]
) -> None:
    if channel.kind == "webhook":
        url = channel.config.get("url") or channel.config.get("webhook_url")
        if not isinstance(url, str):
            raise ValueError("webhook channel has no URL")
        timeout = channel.config.get("timeout_seconds", 5.0)
        if not isinstance(timeout, (int, float)):
            raise ValueError("invalid webhook timeout")
        await WebhookNotifier(url, float(timeout)).notify(event)
        return

    loaded = plugins.get(channel.kind)
    if loaded is None:
        raise LookupError("notifier plugin is unavailable")
    plugin = getattr(loaded, "plugin", loaded)
    async with asyncio.timeout(_PLUGIN_TIMEOUT_SECONDS):
        await plugin.send(event, channel.config)


async def deliver_pending(session_factory: SessionFactory) -> int:
    """Deliver due rows with SKIP LOCKED and capped exponential retries.

    The row lock is held only by this background worker while making a bounded external call;
    concurrent worker instances skip the claimed rows. Each row's delivery state commits with
    the attempt so crashes before commit lead to a safe idempotent retry.
    """
    from lumewarp_control_plane.plugins import NOTIFIER, load

    plugins = {item.name: item for item in load(NOTIFIER)}
    delivered = 0
    async with session_factory() as session, session.begin():
        now = datetime.now(UTC)
        rows = (
            await session.execute(
                select(NotifierOutbox, NotifierChannel)
                .outerjoin(NotifierChannel, NotifierChannel.id == NotifierOutbox.channel_id)
                .where(
                    NotifierOutbox.status == OutboxStatus.PENDING,
                    NotifierOutbox.next_attempt_at <= now,
                )
                .order_by(NotifierOutbox.next_attempt_at, NotifierOutbox.id)
                .limit(_BATCH_SIZE)
                .with_for_update(skip_locked=True, of=NotifierOutbox)
            )
        ).all()
        for row, channel in rows:
            if channel is None:
                row.status = OutboxStatus.FAILED
                row.last_error = "notifier channel is missing"
                continue
            if not channel.enabled:
                row.status = OutboxStatus.DONE
                row.delivered_at = now
                row.last_error = "disabled channel; discarded"
                continue
            try:
                event = _event_from_payload(row.payload)
                await _deliver(channel, event, plugins)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row.attempts += 1
                delay = min(2 ** min(row.attempts - 1, 20), _MAX_RETRY_SECONDS)
                row.next_attempt_at = now + timedelta(seconds=delay)
                # Exception messages can include the webhook URL or plugin configuration.
                row.last_error = type(exc).__name__[:200]
                log.warning(
                    "notifier delivery failed for outbox row %s (%s); retry in %ss",
                    row.id,
                    row.last_error,
                    delay,
                )
            else:
                row.status = OutboxStatus.DONE
                row.delivered_at = datetime.now(UTC)
                row.last_error = None
                delivered += 1
    return delivered

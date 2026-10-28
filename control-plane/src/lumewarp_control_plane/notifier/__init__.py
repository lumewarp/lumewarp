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
_MAX_WEBHOOK_TIMEOUT_SECONDS = 30.0
# A claimed row is invisible to other workers for this long; it must exceed any one delivery.
_CLAIM_LEASE_SECONDS = 2 * _MAX_WEBHOOK_TIMEOUT_SECONDS
_MAX_ATTEMPTS = 20
_BATCH_SIZE = 100


@dataclass(frozen=True)
class BudgetEvent:
    kind: Literal["soft", "hard"]
    scope_type: ScopeType
    scope_id: uuid.UUID
    team_id: uuid.UUID
    window_start: datetime
    spent_nano_usd: int
    budget_nano_usd: int

    def to_payload(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "scope_type": self.scope_type.value,
            "scope_id": str(self.scope_id),
            "team_id": str(self.team_id),
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
        if not 0 < timeout_seconds <= _MAX_WEBHOOK_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be in (0, {_MAX_WEBHOOK_TIMEOUT_SECONDS:g}]")
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def notify(self, event: BudgetEvent) -> None:
        # httpx timeouts are per phase; the outer deadline bounds a slow-drip server too.
        timeout = httpx.Timeout(self.timeout_seconds)
        async with (
            asyncio.timeout(self.timeout_seconds),
            httpx.AsyncClient(timeout=timeout) as client,
        ):
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
                    NotifierChannel.team_id.is_(None) | (NotifierChannel.team_id == event.team_id),
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
            team_id=uuid.UUID(payload["team_id"]),
            window_start=window_start.astimezone(UTC),
            spent_nano_usd=int(payload["spent_nano_usd"]),
            budget_nano_usd=int(payload["budget_nano_usd"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid budget notification payload") from exc


async def _deliver(kind: str, config: dict, event: BudgetEvent, plugins: dict[str, object]) -> None:
    if kind == "webhook":
        url = config.get("url") or config.get("webhook_url")
        if not isinstance(url, str):
            raise ValueError("webhook channel has no URL")
        timeout = config.get("timeout_seconds", 5.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("invalid webhook timeout")
        timeout = min(float(timeout), _MAX_WEBHOOK_TIMEOUT_SECONDS)
        await WebhookNotifier(url, timeout).notify(event)
        return

    loaded = plugins.get(kind)
    if loaded is None:
        raise LookupError("notifier plugin is unavailable")
    plugin = getattr(loaded, "plugin", loaded)
    async with asyncio.timeout(_PLUGIN_TIMEOUT_SECONDS):
        await plugin.send(event, config)


@dataclass(frozen=True)
class _Claimed:
    row_id: int
    attempts: int
    payload: dict
    channel_kind: str
    channel_config: dict


async def _claim(session_factory: SessionFactory) -> list[_Claimed]:
    """Lease due rows in one short transaction; rows without a usable channel finish here."""
    claimed: list[_Claimed] = []
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
            row.next_attempt_at = now + timedelta(seconds=_CLAIM_LEASE_SECONDS)
            claimed.append(
                _Claimed(
                    row.id, row.attempts, dict(row.payload), channel.kind, dict(channel.config)
                )
            )
    return claimed


async def _record_outcome(
    session_factory: SessionFactory, item: _Claimed, error: Exception | None
) -> None:
    async with session_factory() as session, session.begin():
        row = await session.get(NotifierOutbox, item.row_id, with_for_update=True)
        if row is None or row.status is not OutboxStatus.PENDING:
            return
        now = datetime.now(UTC)
        if error is None:
            row.status = OutboxStatus.DONE
            row.delivered_at = now
            row.last_error = None
            return
        row.attempts = item.attempts + 1
        # Exception messages can include the webhook URL or plugin configuration.
        row.last_error = type(error).__name__[:200]
        if row.attempts >= _MAX_ATTEMPTS:
            row.status = OutboxStatus.FAILED
            log.warning(
                "notifier delivery for outbox row %s failed %s times (%s); giving up",
                row.id,
                row.attempts,
                row.last_error,
            )
            return
        delay = min(2 ** min(row.attempts - 1, 20), _MAX_RETRY_SECONDS)
        row.next_attempt_at = now + timedelta(seconds=delay)
        log.warning(
            "notifier delivery failed for outbox row %s (%s); retry in %ss",
            row.id,
            row.last_error,
            delay,
        )


async def deliver_pending(session_factory: SessionFactory) -> int:
    """Deliver due rows with bounded external calls and capped exponential retries.

    Rows are leased (``next_attempt_at`` pushed past the longest delivery) in a short
    transaction, delivered with no transaction open, and each outcome commits on its own.
    A crash or cancel after a delivery but before its commit re-sends that row once the
    lease expires (at-least-once); outcomes already committed are never rolled back.
    After ``_MAX_ATTEMPTS`` failures a row becomes FAILED with its last error.
    """
    from lumewarp_control_plane.plugins import NOTIFIER, load

    plugins = {item.name: item for item in load(NOTIFIER)}
    delivered = 0
    for item in await _claim(session_factory):
        error: Exception | None = None
        try:
            await _deliver(
                item.channel_kind,
                item.channel_config,
                _event_from_payload(item.payload),
                plugins,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        await _record_outcome(session_factory, item, error)
        if error is None:
            delivered += 1
    return delivered

"""Budget notifications (owner: C4).

Events are written to `notifier_outbox` inside the ingest transaction and delivered by a
background loop with retries; ingest never waits on an external call. Channels are
`notifier_channels` rows: kind "webhook" (built-in) or a `lumewarp.notifier` plugin name.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import ScopeType


@dataclass(frozen=True)
class BudgetEvent:
    kind: Literal["soft", "hard"]
    scope_type: ScopeType
    scope_id: uuid.UUID
    org_id: uuid.UUID
    window_start: datetime
    spent_nano_usd: int
    budget_nano_usd: int


class NotifierSink(Protocol):
    async def notify(self, event: BudgetEvent) -> None:
        """Deliver one event. Raise to have the outbox retry with backoff."""
        ...


class WebhookNotifier:
    """Built-in: POST the event as JSON to `url`."""

    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def notify(self, event: BudgetEvent) -> None:
        raise NotImplementedError


async def deliver_pending(session_factory: SessionFactory) -> int:
    """Deliver due `notifier_outbox` rows (SKIP LOCKED), with exponential backoff on
    failure. Returns the number delivered.
    """
    raise NotImplementedError

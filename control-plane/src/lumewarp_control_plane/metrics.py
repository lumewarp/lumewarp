"""Prometheus metrics of the control plane.

Every app owns one `Metrics` (a dedicated `CollectorRegistry`, never prometheus_client's
global one), exposed by `serve` on its own listener (`LUMEWARP_METRICS_BIND`), never on the
admin API port. Labels are bounded enums only: no API key, user, team or gateway ids
(CLAUDE.md rule 5). Counters are incremented after the transaction that makes the event
real has committed.

Code that is called without an app (unit tests, CLI helpers) gets `DISCARD`, a `Metrics`
nobody scrapes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import Counter as _Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    start_http_server,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from wsgiref.simple_server import WSGIServer

    from lumewarp_control_plane.db import SessionFactory

log = logging.getLogger(__name__)
# No `*_created` series: they double the series count and nothing here uses them.
disable_created_metrics()

# How often the DB-backed gauges (outbox backlog, latest config version) are refreshed. A
# scrape never queries the database.
DB_GAUGE_INTERVAL_SECONDS = 5.0
COMPILE_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# Every `suspended_reason` a key can get when it leaves the active state.
KEY_SUSPEND_REASONS = ("owner_removed", "owner_deactivated", "admin", "owner")
# `session.info` key for key suspensions made in a transaction, counted on commit.
_SUSPENDED_INFO_KEY = "lumewarp.keys_suspended"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        r = self.registry
        self.break_glass_logins = Counter(
            "lumewarp_cp_break_glass_logins",
            "Emergency password login attempts.",
            ["result"],
            registry=r,
        )
        for result in ("success", "failure", "rate_limited"):
            self.break_glass_logins.labels(result)
        self.compile_duration = Histogram(
            "lumewarp_cp_compile_duration_seconds",
            "Duration of compiling and validating a config snapshot.",
            buckets=COMPILE_BUCKETS,
            registry=r,
        )
        self.compile_failures = Counter(
            "lumewarp_cp_compile_failures",
            "Config compilations that failed (previous version stays current).",
            registry=r,
        )
        self.config_version = Gauge(
            "lumewarp_cp_config_version",
            "Latest published config version of this control plane's scope.",
            registry=r,
        )
        self.outbox_pending = Gauge(
            "lumewarp_cp_outbox_pending",
            "Pending config outbox entries (refreshed every few seconds).",
            registry=r,
        )
        self.gateway_sessions = Gauge(
            "lumewarp_cp_gateway_sessions",
            "Connected gateway config-stream sessions.",
            registry=r,
        )
        self.gateway_acks = Counter(
            "lumewarp_cp_gateway_acks",
            "Config update replies from gateways.",
            ["result"],
            registry=r,
        )
        self.gateway_strategies_ignored = Counter(
            "lumewarp_cp_gateway_strategies_ignored",
            "Strategies and capabilities in a Hello ignored: over the limit or invalid names.",
            registry=r,
        )
        self.usage_ingested = Counter(
            "lumewarp_cp_usage_events_ingested",
            "Usage events accepted (committed) from gateway reports.",
            registry=r,
        )
        self.usage_skipped = Counter(
            "lumewarp_cp_usage_events_skipped",
            "Usage events skipped by per-event validation.",
            ["reason"],
            registry=r,
        )
        self.authz_checks = Counter(
            "lumewarp_cp_authz_checks",
            "Authorization backend relation checks by decision.",
            ["backend", "decision"],
            registry=r,
        )
        self.keys_suspended = Counter(
            "lumewarp_cp_keys_suspended",
            "API keys moved from active to suspended.",
            ["reason"],
            registry=r,
        )
        self.key_reconcile_last_success = Gauge(
            "lumewarp_cp_key_reconcile_last_success_timestamp_seconds",
            "Unix time of the key reconciler's last fully successful step (0 = never).",
            registry=r,
        )
        for result in ("ack", "nack"):
            self.gateway_acks.labels(result)
        for reason in KEY_SUSPEND_REASONS:
            self.keys_suspended.labels(reason)

    def init_authz_backend(self, backend: str) -> None:
        """Export zero series for a backend so rate() works from the first check."""
        for decision in ("allow", "deny", "error"):
            self.authz_checks.labels(backend, decision)

    def record_authz(self, backend: str, decisions: Mapping[str, int]) -> None:
        for decision, n in decisions.items():
            if n:
                self.authz_checks.labels(backend, decision).inc(n)

    def record_usage(self, accepted: int, skipped: Mapping[str, int]) -> None:
        if accepted:
            self.usage_ingested.inc(accepted)
        for reason, n in skipped.items():
            if n:
                self.usage_skipped.labels(reason).inc(n)

    def record_suspended(self, reason: str, n: int = 1) -> None:
        if n:
            self.keys_suspended.labels(reason).inc(n)

    def count_committed_suspensions(self, session: AsyncSession) -> None:
        """Count suspensions noted with `note_suspended`; call after the commit."""
        noted = session.info.pop(_SUSPENDED_INFO_KEY, None)
        for reason, n in (noted or {}).items():
            self.record_suspended(reason, n)

    def value(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        """Current sample value (0 if absent); for tests and debugging."""
        return self.registry.get_sample_value(name, dict(labels or {})) or 0.0


def note_suspended(session: AsyncSession, reason: str, n: int = 1) -> None:
    """Remember `n` active->suspended key transitions made in `session`'s transaction."""
    if n:
        noted: _Counter[str] = session.info.setdefault(_SUSPENDED_INFO_KEY, _Counter())
        noted[reason] += n


DISCARD = Metrics()


# --------------------------------------------------------------------------- DB gauges


async def refresh_db_gauges(metrics: Metrics, session_factory: SessionFactory, scope: str) -> None:
    """One bounded refresh: a COUNT of pending outbox rows and the latest version."""
    from lumewarp_control_plane.models import ConfigOutbox, ConfigVersion, OutboxStatus

    async with session_factory() as session:
        pending = await session.scalar(
            select(func.count()).where(
                ConfigOutbox.scope == scope, ConfigOutbox.status == OutboxStatus.PENDING
            )
        )
        latest = await session.scalar(
            select(func.max(ConfigVersion.version)).where(ConfigVersion.scope == scope)
        )
    metrics.outbox_pending.set(pending or 0)
    if latest is not None:
        metrics.config_version.set(latest)


async def run_db_gauges(
    metrics: Metrics,
    session_factory: SessionFactory,
    stop: asyncio.Event,
    *,
    scope: str,
    interval_seconds: float = DB_GAUGE_INTERVAL_SECONDS,
) -> None:
    """Refresh the DB-backed gauges every `interval_seconds` until `stop` is set."""
    while not stop.is_set():
        try:
            await refresh_db_gauges(metrics, session_factory, scope)
        except Exception:
            log.warning("metrics: refreshing database gauges failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


# --------------------------------------------------------------------------- listener


class MetricsServer:
    """prometheus_client's threaded HTTP exposition server for one registry."""

    def __init__(self, server: WSGIServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return self._server.server_port

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_metrics_server(metrics: Metrics, host: str, port: int) -> MetricsServer:
    """Serve the text exposition format on host:port (any path). Raises OSError on bind."""
    server, thread = start_http_server(port, addr=host, registry=metrics.registry)
    return MetricsServer(server, thread)

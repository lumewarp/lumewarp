"""Metrics registry contract, the metrics listener and Authorizer decision counting."""

import asyncio

import httpx
import pytest
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError

from lumewarp_control_plane.app import create_app, start_background, stop_background
from lumewarp_control_plane.authz import (
    Authorizer,
    Decision,
    Relation,
    Resource,
    team_resource,
)
from lumewarp_control_plane.metrics import Metrics, start_metrics_server
from lumewarp_control_plane.settings import Settings

# The contract with deploy/prometheus/rules.yaml and the Grafana dashboards.
CONTRACT = {
    "lumewarp_cp_break_glass_logins": ("counter", {"result"}),
    "lumewarp_cp_compile_duration_seconds": ("histogram", set()),
    "lumewarp_cp_compile_failures": ("counter", set()),
    "lumewarp_cp_config_version": ("gauge", set()),
    "lumewarp_cp_gateway_strategies_ignored": ("counter", set()),
    "lumewarp_cp_outbox_pending": ("gauge", set()),
    "lumewarp_cp_gateway_sessions": ("gauge", set()),
    "lumewarp_cp_gateway_acks": ("counter", {"result"}),
    "lumewarp_cp_usage_events_ingested": ("counter", set()),
    "lumewarp_cp_usage_events_skipped": ("counter", {"reason"}),
    "lumewarp_cp_authz_checks": ("counter", {"backend", "decision"}),
    "lumewarp_cp_keys_suspended": ("counter", {"reason"}),
    "lumewarp_cp_key_reconcile_last_success_timestamp_seconds": ("gauge", set()),
    "lumewarp_cp_key_writes": ("counter", {"operation", "result"}),
}


def _families(text: str) -> dict:
    return {f.name: f for f in text_string_to_metric_families(text)}


def _authz(metrics: Metrics, backend: str, decision: str) -> float:
    return metrics.value(
        "lumewarp_cp_authz_checks_total", {"backend": backend, "decision": decision}
    )


def test_registry_matches_alert_and_dashboard_contract() -> None:
    metrics = Metrics()
    metrics.init_authz_backend("builtin")
    metrics.usage_skipped.labels("invalid_event")
    families = _families(generate_latest(metrics.registry).decode())
    assert set(families) == set(CONTRACT)
    for name, (kind, labels) in CONTRACT.items():
        family = families[name]
        assert family.type == kind, name
        for sample in family.samples:
            assert set(sample.labels) - {"le"} == labels, (name, sample)
    # Fresh registries are independent: nothing leaks between apps.
    metrics.compile_failures.inc()
    assert Metrics().value("lumewarp_cp_compile_failures_total") == 0
    assert metrics.value("lumewarp_cp_compile_failures_total") == 1


def test_metrics_bind_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUMEWARP_METRICS_BIND", raising=False)
    assert Settings().metrics_bind == "127.0.0.1:9465"
    assert Settings(metrics_bind="").metrics_bind == ""
    assert Settings(metrics_bind="[::1]:9000").metrics_bind == "[::1]:9000"
    with pytest.raises(ValidationError):
        Settings(metrics_bind="9464")


async def test_metrics_listener_serves_text_format() -> None:
    metrics = Metrics()
    metrics.gateway_acks.labels("nack").inc(3)
    server = start_metrics_server(metrics, "127.0.0.1", 0)
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"http://127.0.0.1:{server.port}/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "version=0.0.4" in resp.headers["content-type"]
        families = _families(resp.text)
        acks = {s.labels["result"]: s.value for s in families["lumewarp_cp_gateway_acks"].samples}
        assert acks["nack"] == 3 and acks["ack"] == 0
        # Only this app's registry: no process/platform collectors from the global one.
        assert set(families) <= set(CONTRACT)
    finally:
        await asyncio.to_thread(server.close)


@pytest.fixture
def stub_background(monkeypatch):
    """Replace every background service except the metrics listener and DB gauges."""
    from lumewarp_control_plane import app as app_module

    class Server:
        async def wait_for_termination(self):
            await asyncio.Event().wait()

        async def stop(self, grace):
            pass

    async def serve(settings, factory, metrics):
        return Server()

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module.grpc_services, "serve", serve)
    monkeypatch.setattr(app_module.grpc_services, "bound_port", lambda server: 0)
    monkeypatch.setattr(app_module.grpc_services, "registry_for", lambda server: None)
    monkeypatch.setattr(app_module.compiler, "run_compiler_loop", idle)
    monkeypatch.setattr(app_module, "run_key_reconciler", idle)
    monkeypatch.setattr(app_module.usage, "rollover_scheduler", idle)
    monkeypatch.setattr(app_module, "_notifier_loop", idle)


async def test_serve_starts_and_stops_metrics_listener(settings: Settings, stub_background):
    """`serve` exposes the app's registry on LUMEWARP_METRICS_BIND, not the admin port."""
    app = create_app(settings.model_copy(update={"metrics_bind": "127.0.0.1:0"}))
    app.state.metrics.outbox_pending.set(-1)
    runtime = await start_background(app)
    try:
        assert runtime.metrics_server is not None
        port = runtime.metrics_server.port
        async with httpx.AsyncClient() as http:
            for _ in range(100):
                families = _families((await http.get(f"http://127.0.0.1:{port}/metrics")).text)
                if families["lumewarp_cp_outbox_pending"].samples[0].value == 0:
                    break
                await asyncio.sleep(0.05)
        # The DB gauge refresher ran against the test database (no pending outbox rows).
        assert families["lumewarp_cp_outbox_pending"].samples[0].value == 0
        assert runtime.tasks["metrics"].done() is False
        backends = {s.labels["backend"] for s in families["lumewarp_cp_authz_checks"].samples}
        assert backends == {"builtin"}
        reasons = {s.labels["reason"] for s in families["lumewarp_cp_usage_events_skipped"].samples}
        assert reasons == {"invalid_event", "request_id_conflict", "key_mismatch"}
    finally:
        await stop_background(runtime, grace=1)
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as http:
            await http.get(f"http://127.0.0.1:{port}/metrics")


def test_disabled_key_reconciler_exports_no_freshness_series(settings: Settings) -> None:
    name = "lumewarp_cp_key_reconcile_last_success_timestamp_seconds"
    enabled = create_app(settings)
    assert name in generate_latest(enabled.state.metrics.registry).decode()
    disabled = create_app(settings.model_copy(update={"key_reconcile_enabled": False}))
    assert name not in generate_latest(disabled.state.metrics.registry).decode()


async def test_serve_without_metrics_bind_has_no_listener(settings: Settings, stub_background):
    app = create_app(settings.model_copy(update={"metrics_bind": ""}))
    runtime = await start_background(app)
    try:
        assert runtime.metrics_server is None
        assert "metrics" not in runtime.tasks
        await asyncio.sleep(0.01)  # let the stubbed services start before stopping them
    finally:
        await stop_background(runtime, grace=1)


async def test_metrics_bind_conflict_aborts_startup(settings: Settings, stub_background):
    busy = start_metrics_server(Metrics(), "127.0.0.1", 0)
    try:
        app = create_app(settings.model_copy(update={"metrics_bind": f"127.0.0.1:{busy.port}"}))
        with pytest.raises(OSError):
            await start_background(app)
    finally:
        await asyncio.to_thread(busy.close)


# --------------------------------------------------------------------------- authz


class _Backend:
    def __init__(self, decisions):
        self.decisions = decisions

    async def check(self, user_id, relation, resource):
        decision = self.decisions[user_id]
        if isinstance(decision, Exception):
            raise decision
        if decision == "slow":
            await asyncio.sleep(1)
        return decision


async def test_authorizer_check_counts_every_decision() -> None:
    metrics = Metrics()
    backend = _Backend(
        {"a": Decision.ALLOW, "d": "deny", "e": RuntimeError("down"), "s": "slow", "x": "bogus"}
    )
    authorizer = Authorizer(backend, "custom", timeout=0.01, metrics=metrics)
    assert _authz(metrics, "custom", "error") == 0
    for user in ("a", "a", "d", "e", "s", "x"):
        await authorizer.check(user, Relation.MEMBER, Resource("group", "lumewarp-admins"))
    assert _authz(metrics, "custom", "allow") == 2
    assert _authz(metrics, "custom", "deny") == 1
    # Exception, timeout and an invalid decision are all errors.
    assert _authz(metrics, "custom", "error") == 3


async def test_authorizer_batch_check_counts_per_item() -> None:
    metrics = Metrics()
    items = [(u, Relation.MEMBER, team_resource("t")) for u in ("a", "d", "e")]

    # Default batch: each item goes through check() and is counted exactly once.
    backend = _Backend({"a": "allow", "d": "deny", "e": RuntimeError("down")})
    fallback = Authorizer(backend, "fallback", metrics=metrics)
    assert await fallback.batch_check(items) == [Decision.ALLOW, Decision.DENY, Decision.ERROR]
    assert [_authz(metrics, "fallback", d) for d in ("allow", "deny", "error")] == [1, 1, 1]

    class Native(_Backend):
        fail = False

        async def batch_check(self, items):
            if self.fail:
                raise RuntimeError("down")
            return ["allow", "allow", "deny"]

    native_backend = Native({})
    native = Authorizer(native_backend, "native", metrics=metrics)
    await native.batch_check(items)
    assert [_authz(metrics, "native", d) for d in ("allow", "deny", "error")] == [2, 1, 0]
    native_backend.fail = True
    assert await native.batch_check(items) == [Decision.ERROR] * 3
    assert _authz(metrics, "native", "error") == 3

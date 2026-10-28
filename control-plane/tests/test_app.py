import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lumewarp_control_plane import app as app_module
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.metrics import Metrics
from lumewarp_control_plane.plugins import GROUPS, discover
from lumewarp_control_plane.settings import Settings


def test_healthz() -> None:
    resp = TestClient(create_app(Settings(background_enabled=False))).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_discover_known_groups() -> None:
    for group in GROUPS:
        assert isinstance(discover(group), dict)


def test_openapi_without_database() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        background_enabled=False,
    )
    spec = create_app(settings).openapi()
    assert spec["info"]["title"] == "Lumewarp control plane"
    assert "/readyz" in spec["paths"]


def test_readyz_ready(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_readyz_database_down() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        background_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "unreachable"


def test_unknown_route_error_shape() -> None:
    resp = TestClient(create_app(Settings(background_enabled=False))).get("/api/v1/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("backend", ["builtin", "external"])
@pytest.mark.parametrize("reconcile_enabled", [True, False])
async def test_background_compiler_receives_backend_membership_mode(
    monkeypatch,
    backend,
    reconcile_enabled,
) -> None:
    compiler_started = asyncio.Event()
    reconciler_started = asyncio.Event()
    grpc_stopped = asyncio.Event()

    class Server:
        async def wait_for_termination(self):
            await grpc_stopped.wait()

        async def stop(self, grace):
            grpc_stopped.set()

    async def serve(settings, factory, metrics):
        assert metrics is app.state.metrics
        return Server()

    async def compiler_loop(factory, stop, *, require_local_membership, **kwargs):
        assert require_local_membership is (backend == "builtin")
        compiler_started.set()
        await stop.wait()

    async def notifier_loop(app, stop):
        await stop.wait()

    async def reconcile_loop(
        factory, authorizer, stop, *, poll_seconds, full_seconds, settings, metrics
    ):
        assert factory is None
        assert metrics is app.state.metrics
        assert authorizer is app.state.authorizer
        assert poll_seconds == settings.key_reconcile_poll_seconds
        assert full_seconds == settings.key_reconcile_full_seconds
        reconciler_started.set()
        await stop.wait()

    async def rollover(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module.grpc_services, "serve", serve)
    monkeypatch.setattr(app_module.grpc_services, "bound_port", lambda server: 0)
    monkeypatch.setattr(app_module.grpc_services, "registry_for", lambda server: None)
    monkeypatch.setattr(app_module.compiler, "run_compiler_loop", compiler_loop)
    monkeypatch.setattr(app_module, "_notifier_loop", notifier_loop)
    monkeypatch.setattr(app_module, "run_key_reconciler", reconcile_loop)
    monkeypatch.setattr(app_module.usage, "rollover_scheduler", rollover)
    failures = []
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=Settings(authz_backend=backend, key_reconcile_enabled=reconcile_enabled),
            authorizer=SimpleNamespace(manages_grants=backend == "builtin"),
            session_factory=None,
            fatal_handler=failures.append,
            metrics=Metrics(),
        )
    )
    runtime = await app_module.start_background(app)
    try:
        await asyncio.wait_for(compiler_started.wait(), 5)
        if reconcile_enabled:
            await asyncio.wait_for(reconciler_started.wait(), 5)
        assert ("key_reconciler" in runtime.tasks) is reconcile_enabled
    finally:
        await app_module.stop_background(runtime)
    assert runtime.failures == {}
    assert failures == []


async def test_lifespan_closes_authorizer_backend_with_background_disabled(monkeypatch):
    closed = []

    class Backend:
        async def aclose(self):
            closed.append(True)

    app = create_app(Settings(background_enabled=False))
    app.state.authorizer = SimpleNamespace(backend=Backend())
    async with app_module.lifespan(app):
        assert app.state.runtime.tasks == {}
        assert closed == []
    assert closed == [True]

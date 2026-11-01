"""FastAPI app factory and process lifespan.

One supervised process: uvicorn (1 worker) + grpc.aio ConfigService/UsageService + the
compiler loop + the budget rollover scheduler + the notifier delivery loop + the Prometheus
metrics listener (`LUMEWARP_METRICS_BIND`, its own port) and its DB gauge refresher, all
started and stopped in the FastAPI lifespan.

Readiness = DB reachable + migrations at head + every background service alive. A
background service that stops on its own is logged and fails readiness; if it is the gRPC
server, the process is asked to exit (`fatal_handler`, SIGTERM by default) because
gateways can no longer get config.
"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from lumewarp_control_plane import __version__, audit, compiler, migrate, notifier, usage
from lumewarp_control_plane import grpc as grpc_services
from lumewarp_control_plane.api import router as api_router
from lumewarp_control_plane.auth.break_glass import LoginLimiter, enabled, validate_credentials
from lumewarp_control_plane.authz import load_authorizer
from lumewarp_control_plane.db import create_engine, create_session_factory
from lumewarp_control_plane.errors import error_body, install_handlers
from lumewarp_control_plane.keys.reconciler import run_key_reconciler
from lumewarp_control_plane.metrics import (
    Metrics,
    MetricsServer,
    run_db_gauges,
    start_metrics_server,
)
from lumewarp_control_plane.settings import Settings, get_settings, split_bind

log = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 2.0
# Deadline for draining gRPC streams and stopping loops on shutdown.
SHUTDOWN_GRACE_SECONDS = 5.0
NOTIFIER_INTERVAL_SECONDS = 1.0
# Services whose unexpected exit makes the process exit, not only unready.
FATAL_SERVICES = frozenset({"grpc"})


def _terminate_process(service: str) -> None:
    log.critical("background service %s stopped; shutting down the process", service)
    os.kill(os.getpid(), signal.SIGTERM)


@dataclass
class Runtime:
    """Background services of one app instance (app.state.runtime)."""

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    grpc_server: object | None = None
    grpc_port: int | None = None
    metrics_server: MetricsServer | None = None
    failures: dict[str, str] = field(default_factory=dict)


async def _notifier_loop(app: FastAPI, stop: asyncio.Event) -> None:
    factory = app.state.session_factory
    while not stop.is_set():
        try:
            await notifier.deliver_pending(factory)
        except Exception:
            log.exception("notifier delivery step failed")
        try:
            await asyncio.wait_for(stop.wait(), NOTIFIER_INTERVAL_SECONDS)
        except TimeoutError:
            pass


def _supervise(
    app: FastAPI, runtime: Runtime, name: str, coro: Awaitable[None]
) -> asyncio.Task[None]:
    async def run() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.failures[name] = type(exc).__name__
            log.exception("background service %s crashed", name)
        else:
            if runtime.stop.is_set():
                return
            runtime.failures[name] = "exited"
            log.error("background service %s exited unexpectedly", name)
        if name in FATAL_SERVICES and not runtime.stop.is_set():
            app.state.fatal_handler(name)

    return asyncio.create_task(run(), name=f"lumewarp:{name}")


async def start_background(app: FastAPI) -> Runtime:
    settings: Settings = app.state.settings
    factory = app.state.session_factory
    metrics: Metrics = app.state.metrics
    runtime = Runtime()
    # Bind errors surface here and abort startup.
    server = await grpc_services.serve(settings, factory, metrics)
    if settings.metrics_bind:
        try:
            runtime.metrics_server = start_metrics_server(
                metrics, *split_bind(settings.metrics_bind)
            )
        except OSError:
            await server.stop(0)
            raise
        log.info("metrics listening on port %s", runtime.metrics_server.port)
    runtime.grpc_server = server
    runtime.grpc_port = grpc_services.bound_port(server)
    registry = grpc_services.registry_for(server)

    def on_published(scope: str, version: int) -> None:
        if registry is not None:
            registry.notify_published(scope, version)

    runtime.tasks["grpc"] = _supervise(app, runtime, "grpc", server.wait_for_termination())
    runtime.tasks["compiler"] = _supervise(
        app,
        runtime,
        "compiler",
        compiler.run_compiler_loop(
            factory,
            runtime.stop,
            scope=settings.scope,
            on_published=on_published,
            require_local_membership=app.state.authorizer.manages_grants,
            metrics=metrics,
        ),
    )
    runtime.tasks["rollover"] = _supervise(
        app, runtime, "rollover", usage.rollover_scheduler(settings, factory)
    )
    if settings.key_reconcile_enabled:
        runtime.tasks["key_reconciler"] = _supervise(
            app,
            runtime,
            "key_reconciler",
            run_key_reconciler(
                factory,
                app.state.authorizer,
                runtime.stop,
                poll_seconds=settings.key_reconcile_poll_seconds,
                full_seconds=settings.key_reconcile_full_seconds,
                settings=settings,
                metrics=metrics,
            ),
        )
    runtime.tasks["notifier"] = _supervise(
        app, runtime, "notifier", _notifier_loop(app, runtime.stop)
    )
    log.info("gRPC listening on port %s", runtime.grpc_port)
    if runtime.metrics_server is not None:
        runtime.tasks["metrics"] = _supervise(
            app,
            runtime,
            "metrics",
            run_db_gauges(metrics, factory, runtime.stop, scope=settings.scope),
        )
    return runtime


async def stop_background(runtime: Runtime, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """Stop accepting streams, signal loops, and wait for everything within `grace`
    seconds; whatever is still running after that is cancelled.
    """
    runtime.stop.set()
    if runtime.metrics_server is not None:
        await asyncio.to_thread(runtime.metrics_server.close)
    if runtime.grpc_server is not None:
        await runtime.grpc_server.stop(grace)
    tasks = list(runtime.tasks.values())
    # The rollover scheduler sleeps until the next month: cancel it right away.
    if (rollover := runtime.tasks.get("rollover")) is not None:
        rollover.cancel()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    runtime = await start_background(app) if settings.background_enabled else Runtime()
    app.state.runtime = runtime
    app.state.tasks = runtime.tasks
    try:
        yield
    finally:
        await stop_background(runtime)
        try:
            if (close := getattr(app.state.authorizer.backend, "aclose", None)) is not None:
                await close()
        finally:
            await app.state.engine.dispose()


def _static_module():
    """The Admin UI static module (U2), or None before it exists."""
    try:
        from lumewarp_control_plane import static  # type: ignore[attr-defined]
    except ImportError:
        return None
    return static


def create_app(
    settings: Settings | None = None,
    *,
    fatal_handler: Callable[[str], None] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    validate_credentials(settings)
    app = FastAPI(title="Lumewarp control plane", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.break_glass_limiter = LoginLimiter()
    if enabled(settings):
        log.warning("Break-glass password login is enabled")
    if settings.break_glass_password:
        log.warning("Break-glass uses a plaintext environment password; use a hash in production")
    app.state.metrics = Metrics()
    for reason in usage.SKIP_REASONS:
        app.state.metrics.usage_skipped.labels(reason)
    if not settings.key_reconcile_enabled:
        # No reconciler: export no freshness series rather than a stale 0 that alerts.
        app.state.metrics.registry.unregister(app.state.metrics.key_reconcile_last_success)
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.authorizer = load_authorizer(settings, app.state.session_factory, app.state.metrics)
    app.state.runtime = Runtime()
    app.state.tasks = {}
    app.state.fatal_handler = fatal_handler or _terminate_process
    install_handlers(app)
    app.include_router(api_router)

    @app.middleware("http")
    async def audit_emergency_action(request: Request, call_next):
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            principal = getattr(request.state, "principal", None)
            if principal is not None and principal.is_break_glass:
                async with app.state.session_factory() as session, session.begin():
                    audit.record(
                        session,
                        action="session.break_glass.request",
                        resource_type="http",
                        principal=principal,
                        details={
                            "method": request.method,
                            "path": request.url.path,
                            "status": status,
                        },
                    )
        return response

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving HTTP."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        """Readiness: DB reachable, schema at the migration head, gRPC server, compiler
        loop, rollover scheduler and notifier loop alive.
        """
        checks = {"database": "ok", "migrations": "ok"}
        try:
            async with asyncio.timeout(READY_TIMEOUT_SECONDS):
                async with app.state.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                    current = await migrate.current_revision(conn)
            head = migrate.head_revision()
            if current != head:
                checks["migrations"] = f"at {current}, head is {head}"
        except Exception as exc:  # any failure means not ready
            log.warning("readiness: database check failed: %s", exc)
            checks["database"] = "unreachable"
            checks["migrations"] = "unknown"
        runtime: Runtime = app.state.runtime
        for name, task in runtime.tasks.items():
            checks[name] = "ok" if not task.done() else runtime.failures.get(name, "stopped")
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    if (static := _static_module()) is not None:
        # Registered before the UI so its SPA fallback never swallows unknown API paths:
        # those stay JSON 404s.
        @app.api_route(
            "/api/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def api_not_found(request: Request, path: str) -> JSONResponse:
            return JSONResponse(error_body("not_found", "Not Found"), status_code=404)

        static.mount_static(app)

    return app

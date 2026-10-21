"""FastAPI app factory and process lifespan.

One process: uvicorn (1 worker) + grpc.aio server + background loops, all started in the
lifespan. Readiness = DB reachable + migrations at head + every background task alive.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from lumewarp_control_plane import __version__, migrate
from lumewarp_control_plane.api import router as api_router
from lumewarp_control_plane.db import SessionFactory, create_engine, create_session_factory
from lumewarp_control_plane.errors import install_handlers
from lumewarp_control_plane.settings import Settings, get_settings

log = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 2.0

# A long-running coroutine; returning or raising makes the process not ready.
BackgroundService = Callable[[Settings, SessionFactory], Awaitable[None]]


def background_services(settings: Settings) -> dict[str, BackgroundService]:
    """Services started by the lifespan when settings.background_enabled.

    Integration (W2b) registers here: "grpc" (wraps grpc.serve and waits for
    termination), "compiler" (loops compiler.publish_pending, woken by
    outbox.RECOMPILE_CHANNEL), "rollover" (usage.rollover_scheduler) and "notifier"
    (loops notifier.deliver_pending).
    """
    return {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    tasks: dict[str, asyncio.Task[None]] = {}
    if settings.background_enabled:
        for name, service in background_services(settings).items():
            tasks[name] = asyncio.create_task(
                service(settings, app.state.session_factory), name=name
            )
    app.state.tasks = tasks
    try:
        yield
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Lumewarp control plane", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.tasks = {}
    install_handlers(app)
    app.include_router(api_router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving HTTP."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        """Readiness: DB reachable, schema at the migration head, background tasks alive."""
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
        for name, task in app.state.tasks.items():
            checks[f"task:{name}"] = "stopped" if task.done() else "ok"
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    return app

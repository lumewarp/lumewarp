"""Start the control plane against a fresh, migrated Postgres database for the Playwright smoke test.

Run via `uv run --project ../control-plane python e2e/serve.py` (see playwright.config.ts). Creates
`lumewarp_test_e2e_<hex>` on the dev Postgres (or LUMEWARP_TEST_DATABASE_URL), migrates and
bootstraps it, seeds a viewer user with a known cookie session, serves until signalled, then
drops the database.
"""

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

from lumewarp_control_plane.auth.sessions import csrf_token
from lumewarp_control_plane.models import User, UserSession
from lumewarp_control_plane.security import hash_secret
from lumewarp_control_plane.settings import DEV_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

HTTP_PORT = os.environ.get("E2E_HTTP_PORT", "8992")
GRPC_PORT = os.environ.get("E2E_GRPC_PORT", "50952")
BREAK_GLASS_PASSWORD = "e2e-break-glass-password"
VIEWER_EMAIL = "viewer@e2e.test"
VIEWER_TOKEN = "e2e-viewer-session-0123456789abcdef"
SERVER_URL = os.environ.get("LUMEWARP_TEST_DATABASE_URL", DEV_DATABASE_URL)


async def admin(sql: str) -> None:
    engine = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def seed_viewer(url: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            user = User(email=VIEWER_EMAIL, display_name="E2E Viewer")
            session.add(user)
            await session.flush()
            session.add(
                UserSession(
                    user_id=user.id,
                    token_hash=hash_secret(VIEWER_TOKEN),
                    csrf_hash=hash_secret(csrf_token(VIEWER_TOKEN)),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    finally:
        await engine.dispose()


def cp(*args: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "lumewarp_control_plane", *args], env=env, check=True
    )


def main() -> None:
    name = f"lumewarp_test_e2e_{uuid.uuid4().hex[:12]}"
    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    url = make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)
    env = {
        **os.environ,
        "LUMEWARP_DATABASE_URL": url,
        "LUMEWARP_BREAK_GLASS_PASSWORD": BREAK_GLASS_PASSWORD,
        "LUMEWARP_DEV_MODE": "true",
        "LUMEWARP_SESSION_COOKIE_SECURE": "false",
        "LUMEWARP_METRICS_BIND": "",
        "LUMEWARP_HTTP_BIND": f"127.0.0.1:{HTTP_PORT}",
        "LUMEWARP_GRPC_BIND": f"127.0.0.1:{GRPC_PORT}",
    }
    server: subprocess.Popen[bytes] | None = None
    try:
        cp("migrate", env=env)
        cp("bootstrap", "--gateway-id", "e2e-gw", env=env)
        asyncio.run(seed_viewer(url))
        server = subprocess.Popen(
            [sys.executable, "-m", "lumewarp_control_plane", "serve"], env=env
        )
        signal.signal(signal.SIGTERM, lambda *_: server and server.terminate())
        signal.signal(signal.SIGINT, lambda *_: server and server.terminate())
        server.wait()
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


if __name__ == "__main__":
    main()

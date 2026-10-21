"""Alembic environment.

Two entry points:
- `migrate.py` passes an open (sync-facade) connection in `config.attributes["connection"]`;
  this is how the CLI, readiness checks and tests run migrations from async code.
- Plain `alembic` CLI (control-plane/alembic.ini): opens its own async engine from
  `LUMEWARP_DATABASE_URL`.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane.models import Base
from lumewarp_control_plane.settings import Settings

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or Settings().database_url
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (SQL script) migrations are not supported")

connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    asyncio.run(_run_async())

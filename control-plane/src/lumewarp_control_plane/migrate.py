"""Run Alembic migrations from async code (CLI, readiness, tests)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("no migrations found")
    return head


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    await _run(engine, lambda cfg: command.upgrade(cfg, revision))


async def downgrade(engine: AsyncEngine, revision: str) -> None:
    await _run(engine, lambda cfg: command.downgrade(cfg, revision))


async def current_revision(conn: AsyncConnection) -> str | None:
    def _current(sync_conn: Connection) -> str | None:
        return MigrationContext.configure(sync_conn).get_current_revision()

    return await conn.run_sync(_current)


async def _run(engine: AsyncEngine, fn) -> None:
    async with engine.connect() as conn:

        def _sync(sync_conn: Connection) -> None:
            cfg = alembic_config()
            cfg.attributes["connection"] = sync_conn
            fn(cfg)

        await conn.run_sync(_sync)
        await conn.commit()

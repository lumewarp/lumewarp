import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane.models import Base
from tests import conftest as fixtures
from tests.conftest import (
    SERVER_URL,
    create_database,
    drop_database,
    migrated_database,
)


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {r[0] for r in rows}


async def test_upgrade_downgrade_upgrade() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine)
        expected = set(Base.metadata.tables)
        assert expected <= await _tables(engine)
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) == migrate.head_revision()

        await migrate.downgrade(engine, "base")
        assert await _tables(engine) == {"alembic_version"}
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) is None
            funcs = await conn.execute(
                text("SELECT 1 FROM pg_proc WHERE proname = 'lumewarp_config_versions_immutable'")
            )
            assert funcs.first() is None

        await migrate.upgrade(engine)
        assert expected <= await _tables(engine)
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_models_match_migrations(engine) -> None:
    """The migrated schema has no drift from models.py (what autogenerate would emit)."""

    def _diff(sync_conn):
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


async def test_enum_check_constraints_exist(engine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname LIKE 'ck_%'")
        )
        names = {r[0] for r in rows}
    for name in (
        "ck_api_keys_key_lifecycle",
        "ck_audit_log_actor_type",
        "ck_usage_events_usage_state",
        "ck_teams_model_access",
        "ck_teams_no_inherit",
        "ck_team_members_team_role",
        "ck_service_accounts_model_access",
        "ck_api_keys_owner_matches_kind",
        "ck_config_versions_compaction",
    ):
        assert name in names


async def _database_exists(url: str) -> bool:
    engine = create_async_engine(SERVER_URL)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": make_url(url).database},
            )
            return rows.first() is not None
    finally:
        await engine.dispose()


async def test_fixture_drops_database_when_migration_fails(monkeypatch) -> None:
    created = []

    async def capture_database() -> str:
        url = await create_database()
        created.append(url)
        return url

    async def boom(*_args, **_kwargs) -> None:
        assert await _database_exists(created[0])
        raise RuntimeError("migration failed")

    monkeypatch.setattr(fixtures, "create_database", capture_database)
    monkeypatch.setattr(migrate, "upgrade", boom)
    with pytest.raises(RuntimeError, match="migration failed"):
        async with migrated_database():
            pass
    assert len(created) == 1
    assert not await _database_exists(created[0])


async def test_drop_database_refuses_non_test_names() -> None:
    with pytest.raises(RuntimeError, match="refusing"):
        await drop_database(SERVER_URL)

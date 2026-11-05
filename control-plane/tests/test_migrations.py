import uuid

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

        # What came after 0010 goes down and comes back.
        await migrate.downgrade(engine, "0010")
        assert "gateway_strategies" not in await _tables(engine)
        await migrate.upgrade(engine)
        assert "gateway_strategies" in await _tables(engine)
        await migrate.downgrade(engine, "0010")

        # 0010 is irreversible: everything below it still goes down.
        with pytest.raises(NotImplementedError, match="irreversible"):
            await migrate.downgrade(engine, "0009")
        async with engine.connect() as conn:
            # Later reversible revisions have already been downgraded when
            # Alembic reaches the irreversible catalog conversion.
            assert await migrate.current_revision(conn) == "0010"
        await drop_database(url)
        url = await create_database()
        await engine.dispose()
        engine = create_async_engine(url)
        await migrate.upgrade(engine, "0009")

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
        "ck_routers_strategy_name",
        "ck_catalog_names_catalog_kind",
        "ck_aliases_target_not_alias",
        "ck_routers_sticky_by",
        "ck_routers_sticky_header_iff",
        "ck_routers_retry_shape",
        "ck_routers_retry_status_range",
        "ck_upstreams_health_check_bounds",
        "ck_upstreams_circuit_breaker_shape",
        "ck_router_splits_name_charset",
    ):
        assert name in names


async def test_downgrade_0003_maps_vllm_load() -> None:
    """0003 -> 0002 drops the routing resilience settings and keeps routers valid."""
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0009")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO routers (id, name, strategy, retry_enabled, retry_on_status)"
                    " VALUES (gen_random_uuid(), 'm', 'vllm_load', true, '{503}')"
                )
            )
        await migrate.downgrade(engine, "0002")
        async with engine.connect() as conn:
            strategy = await conn.scalar(text("SELECT strategy FROM routes"))
            assert strategy == "least_in_flight"
            assert "route_fallbacks" not in await _tables(engine)
        await migrate.upgrade(engine)
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_0004_migrates_audit_and_revokes_emergency_sessions_on_downgrade() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0003")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, is_superuser) "
                    "VALUES (gen_random_uuid(), 'admin@example.com', true)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO audit_log (actor_type, action, resource_type) "
                    "VALUES ('superuser_token', 'test', 'test')"
                )
            )
        await migrate.upgrade(engine, "0004")
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT actor_type FROM audit_log")) == "break_glass"
            await conn.execute(
                text(
                    "INSERT INTO user_sessions (id, kind, token_hash, csrf_hash, expires_at) "
                    "VALUES (gen_random_uuid(), 'break_glass', "
                    "decode(repeat('01',32),'hex'), decode(repeat('02',32),'hex'), "
                    "now() + interval '1 hour')"
                )
            )
        await migrate.downgrade(engine, "0003")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT actor_type FROM audit_log")) == "superuser_token"
            assert await conn.scalar(text("SELECT is_superuser FROM users")) is False
            assert await conn.scalar(text("SELECT count(*) FROM user_sessions")) == 0
        await migrate.upgrade(engine, "0004")
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_0013_refuses_a_metrics_url_with_a_query_without_printing_it() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0012")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO upstreams (id, name, kind, base_url, metrics_url) VALUES ("
                    "gen_random_uuid(), :name, :kind, :base, :metrics)"
                ),
                {
                    "name": "scraped",
                    "kind": "vllm",
                    "base": "http://vllm:8000/v1",
                    "metrics": "http://vllm:8000/metrics?token=private-value",
                },
            )
        with pytest.raises(RuntimeError, match="scraped") as refused:
            await migrate.upgrade(engine)
        assert "private-value" not in str(refused.value)
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) == "0012"
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE upstreams SET metrics_url = NULL"))
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) == migrate.head_revision()
    finally:
        await engine.dispose()
        await drop_database(url)


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


async def test_0006_revokes_sessions_and_discards_unneeded_claims():
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0005")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_sessions "
                    "(id, kind, groups, token_hash, csrf_hash, expires_at) "
                    "VALUES (gen_random_uuid(), 'break_glass', '[\"private-iam-group\"]', "
                    "decode(repeat('01',32),'hex'), decode(repeat('02',32),'hex'), "
                    "now() + interval '1 hour')"
                )
            )
        await migrate.upgrade(engine, "0006")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT revoked_at IS NOT NULL FROM user_sessions"))
            assert await conn.scalar(text("SELECT admin_group FROM user_sessions")) is None
            assert (
                await conn.scalar(text("SELECT credential_fingerprint FROM user_sessions")) is None
            )
        await migrate.downgrade(engine, "0005")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT groups FROM user_sessions")) == []
            assert await conn.scalar(text("SELECT revoked_at IS NOT NULL FROM user_sessions"))
        await migrate.upgrade(engine, "0006")
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_0016_moves_installation_roles_to_postgres() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        # Start at the previous head and create a session row that predates the column drop.
        await migrate.upgrade(engine, "0015")
        user_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (id, email, is_active) VALUES (:id, :email, true)"),
                {"id": user_id, "email": f"existing-{user_id}@example.com"},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_sessions"
                    " (id, user_id, kind, admin_group, token_hash, csrf_hash, expires_at)"
                    " VALUES (gen_random_uuid(), :user_id, 'session', 'legacy-group',"
                    " decode(repeat('01', 32), 'hex'), decode(repeat('02', 32), 'hex'),"
                    " now() + interval '1 hour')"
                ),
                {"user_id": user_id},
            )

        async def columns(table: str) -> set[str]:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = :table"
                    ),
                    {"table": table},
                )
                return {row[0] for row in rows}

        def session_rows(conn):
            return conn.scalar(
                text("SELECT count(*) FROM user_sessions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )

        await migrate.upgrade(engine)
        assert "installation_admins" in await _tables(engine)
        assert await columns("installation_admins") == {
            "user_id",
            "role",
            "assigned_by",
            "assigned_at",
        }
        assert "admin_group" not in await columns("user_sessions")
        async with engine.connect() as conn:
            assert await session_rows(conn) == 1

        await migrate.downgrade(engine, "0015")
        assert "installation_admins" not in await _tables(engine)
        assert "admin_group" in await columns("user_sessions")
        async with engine.connect() as conn:
            assert await session_rows(conn) == 1
            assert (
                await conn.scalar(
                    text("SELECT admin_group FROM user_sessions WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
                is None
            )
        await migrate.upgrade(engine)
        assert "installation_admins" in await _tables(engine)
    finally:
        await engine.dispose()
        await drop_database(url)


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

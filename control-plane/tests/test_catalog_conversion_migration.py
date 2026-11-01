"""0010 converts a populated 0009 database, or fails listing every problem."""

import logging
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from tests.conftest import create_database, drop_database


class Db:
    """A 0009 database written with raw SQL: the ORM only knows the final tables."""

    def __init__(self, engine):
        self.engine = engine
        self.upstream = uuid.uuid4()

    async def seed_base(self) -> None:
        await self.run("INSERT INTO scopes (name, source_revision) VALUES ('default', 4)")
        await self.run(
            "INSERT INTO upstreams (id, name, kind, base_url) "
            "VALUES (:id, 'u', 'openai', 'http://localhost/v1')",
            id=self.upstream,
        )

    async def run(self, sql: str, **params) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    async def rows(self, sql: str, **params) -> list[tuple]:
        async with self.engine.connect() as conn:
            return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]

    async def model(self, name: str, weight: int = 1) -> uuid.UUID:
        id_ = uuid.uuid4()
        await self.run(
            "INSERT INTO models (id, name, upstream_id, upstream_model, weight) "
            "VALUES (:id, :name, :upstream, :real, :weight)",
            id=id_,
            name=name,
            upstream=self.upstream,
            real=f"{name}-real",
            weight=weight,
        )
        return id_

    async def route(self, name: str, *members: uuid.UUID, **columns) -> uuid.UUID:
        id_ = uuid.uuid4()
        extra = "".join(f", {column}" for column in columns)
        values = "".join(f", :{column}" for column in columns)
        await self.run(
            f"INSERT INTO routers (id, name{extra}) VALUES (:id, :name{values})",
            id=id_,
            name=name,
            **columns,
        )
        for position, member in enumerate(members):
            await self.run(
                "INSERT INTO router_models (router_id, model_id, position) "
                "VALUES (:router, :model, :position)",
                router=id_,
                model=member,
                position=position,
            )
        return id_

    async def fallback(self, router: uuid.UUID, target: uuid.UUID) -> None:
        await self.run(
            "INSERT INTO router_fallbacks (router_id, fallback_router_id, position) "
            "VALUES (:router, :target, 0)",
            router=router,
            target=target,
        )

    async def split(self, router: uuid.UUID, *members: uuid.UUID) -> None:
        split = uuid.uuid4()
        await self.run(
            "INSERT INTO router_splits (id, router_id, name, weight, position) "
            "VALUES (:id, :router, 'stable', 1, 0)",
            id=split,
            router=router,
        )
        for position, member in enumerate(members):
            await self.run(
                "INSERT INTO router_split_models (split_id, model_id, position) "
                "VALUES (:split, :model, :position)",
                split=split,
                model=member,
                position=position,
            )

    async def names(self) -> dict[str, str]:
        return dict(await self.rows("SELECT name, kind FROM catalog_names"))


@pytest.fixture
async def db():
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0009")
        database = Db(engine)
        await database.seed_base()
        yield database
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_single_member_route_merges_into_a_model_named_after_it(db) -> None:
    deployment = await db.model("deployment-a", weight=7)
    await db.route("public", deployment)
    await migrate.upgrade(db.engine)
    assert await db.names() == {"public": "model"}
    # The deployment's row lives on, under the route's public name.
    assert await db.rows("SELECT name_id, upstream_id, upstream_model FROM models") == [
        (deployment, db.upstream, "deployment-a-real")
    ]
    assert await db.rows("SELECT name_id FROM routers") == []


async def test_two_routes_sharing_a_deployment_each_get_a_model(db) -> None:
    deployment = await db.model("shared")
    await db.route("one", deployment)
    await db.route("two", deployment)
    await migrate.upgrade(db.engine)
    assert await db.names() == {"one": "model", "two": "model"}
    assert await db.rows(
        "SELECT n.name, m.upstream_model FROM models m "
        "JOIN catalog_names n ON n.id = m.name_id ORDER BY n.name"
    ) == [("one", "shared-real"), ("two", "shared-real")]


async def test_multi_member_route_keeps_members_with_old_weights(db) -> None:
    a = await db.model("a", weight=3)
    b = await db.model("b", weight=9)
    route = await db.route("pool", b, a)
    await migrate.upgrade(db.engine)
    assert await db.names() == {"a": "model", "b": "model", "pool": "router"}
    assert await db.rows(
        "SELECT model_id, weight, position FROM router_members "
        "WHERE router_id = :r ORDER BY position",
        r=route,
    ) == [(b, 9, 0), (a, 3, 1)]
    # Strategy is a string now, and params are an object.
    assert await db.rows("SELECT strategy, params::text FROM routers") == [("weighted", "{}")]


async def test_retry_route_stays_a_router_with_its_settings(db) -> None:
    a = await db.model("a")
    await db.route("guarded", a, retry_enabled=True, retry_max_attempts=3)
    await migrate.upgrade(db.engine)
    assert await db.names() == {"a": "model", "guarded": "router"}
    assert await db.rows("SELECT retry_enabled, retry_max_attempts FROM routers") == [(True, 3)]


async def test_custom_strategy_becomes_a_string_with_params(db) -> None:
    a = await db.model("a")
    b = await db.model("b")
    await db.route(
        "pin",
        a,
        b,
        strategy="custom",
        custom_strategy="header_pin",
    )
    await db.run(
        """UPDATE routers SET strategy_params = '{"headers": ["x-pin"]}' WHERE name = 'pin'"""
    )
    await migrate.upgrade(db.engine)
    rows = await db.rows("SELECT strategy, params->'headers'->>0 FROM routers")
    assert rows == [("header_pin", "x-pin")]


async def test_split_route_keeps_split_members_and_drops_its_own(db) -> None:
    a = await db.model("a", weight=2)
    b = await db.model("b", weight=4)
    route = await db.route("split", b)
    await db.split(route, a)
    await migrate.upgrade(db.engine)
    assert await db.rows("SELECT model_id FROM router_members") == []
    assert await db.rows("SELECT model_id, weight FROM router_split_members") == [(a, 2)]


async def test_single_member_fallback_becomes_that_member(db) -> None:
    a = await db.model("a")
    backup_deployment = await db.model("b")
    main = await db.route("main", a, retry_enabled=True, retry_max_attempts=2)
    backup = await db.route("backup", backup_deployment)
    await db.fallback(main, backup)
    await migrate.upgrade(db.engine)
    assert await db.names() == {"a": "model", "backup": "model", "main": "router"}
    # The backup route merged into its deployment's model, under the route's name.
    assert await db.rows("SELECT router_id, model_id, position FROM router_fallbacks") == [
        (main, backup_deployment, 0)
    ]


async def test_fallback_with_retry_policy_maps_to_its_member(db) -> None:
    a = await db.model("a")
    b = await db.model("b")
    main = await db.route("main", a, retry_enabled=True, retry_max_attempts=2)
    backup = await db.route("backup", b, retry_enabled=True, retry_max_attempts=2)
    await db.fallback(main, backup)
    await migrate.upgrade(db.engine)
    assert await db.rows("SELECT router_id, model_id FROM router_fallbacks") == [(main, b)]


async def test_grants_convert_and_unknown_names_are_dropped_and_counted(db, caplog) -> None:
    a = await db.model("a")
    await db.route("public", a)
    pool_a = await db.model("x")
    pool_b = await db.model("y")
    pool = await db.route("pool", pool_a, pool_b)
    team = uuid.uuid4()
    user = uuid.uuid4()
    account = uuid.uuid4()
    await db.run(
        "INSERT INTO teams (id, name, model_access, allowed_models) "
        "VALUES (:id, 't', 'list', ARRAY['public', 'pool', 'gone'])",
        id=team,
    )
    await db.run("INSERT INTO users (id, email) VALUES (:id, 'u@example.com')", id=user)
    await db.run(
        "INSERT INTO team_members (team_id, user_id, role, model_access, allowed_models) "
        "VALUES (:team, :user, 'member', 'list', ARRAY['public', 'also-gone'])",
        team=team,
        user=user,
    )
    await db.run(
        "INSERT INTO service_accounts (id, team_id, name, model_access, allowed_models) "
        "VALUES (:id, :team, 'sa', 'list', ARRAY['pool'])",
        id=account,
        team=team,
    )
    with caplog.at_level(logging.WARNING):
        await migrate.upgrade(db.engine)
    grants = await db.rows(
        "SELECT name_id, team_id, user_id, service_account_id FROM policy_grants"
    )
    assert set(grants) == {
        (a, team, None, None),
        (pool, team, None, None),
        (a, team, user, None),
        (pool, team, None, account),
    }
    assert await db.rows("SELECT model_access FROM teams") == [("list",)]
    assert any("2" in r.getMessage() and "dropped" in r.getMessage() for r in caplog.records)
    # The column is gone: everything reads `policy_grants` now.
    columns = await db.rows(
        "SELECT column_name FROM information_schema.columns WHERE column_name = 'allowed_models'"
    )
    assert columns == []


async def test_config_versions_are_cleared_and_a_republish_is_queued(db) -> None:
    payload = b"old-snapshot"
    await db.run(
        "INSERT INTO config_versions (scope, version, payload, digest, size_bytes, key_count, "
        "source_revision, kind) VALUES ('default', 3, :payload, :digest, :size, 0, 4, 'compile')",
        payload=payload,
        digest=b"\0" * 32,
        size=len(payload),
    )
    await migrate.upgrade(db.engine)
    rows = await db.rows(
        "SELECT version, payload, catalog_state, compacted_at IS NOT NULL FROM config_versions"
    )
    assert rows == [(3, None, None, True)]
    assert await db.rows("SELECT source_revision FROM scopes") == [(5,)]
    assert await db.rows(
        "SELECT scope, reason FROM config_outbox WHERE reason = 'migration.catalog'"
    ) == [("default", "migration.catalog")]


async def _failure(db) -> str:
    with pytest.raises(RuntimeError, match="catalog conversion failed") as caught:
        await migrate.upgrade(db.engine)
    return str(caught.value)


async def test_route_and_model_with_the_same_name_fail_listing_them(db) -> None:
    a = await db.model("clash")
    b = await db.model("other")
    await db.route("clash", a, b)
    second = await db.model("clash-two")
    third = await db.model("y")
    await db.route("clash-two", second, third)
    message = await _failure(db)
    assert "router and model both named 'clash'" in message
    assert "router and model both named 'clash-two'" in message
    # Nothing changed: still at 0009 with the old tables.
    async with db.engine.connect() as conn:
        assert await migrate.current_revision(conn) == "0009"
    assert await db.rows("SELECT name FROM routers ORDER BY name") == [
        ("clash",),
        ("clash-two",),
    ]


async def test_merged_route_taking_a_model_name_fails(db) -> None:
    kept = await db.model("taken")
    other = await db.model("other")
    await db.route("pool", kept, other)  # "taken" stays a model under its own name
    deployment = await db.model("source")
    await db.route("taken", deployment)  # would merge into a model also named "taken"
    assert "two models would be named 'taken'" in await _failure(db)


async def test_multi_member_fallback_fails_listing_it(db) -> None:
    a = await db.model("a")
    b = await db.model("b")
    main = await db.route("main", a, retry_enabled=True, retry_max_attempts=2)
    pool = await db.route("pool", a, b)
    await db.fallback(main, pool)
    message = await _failure(db)
    assert "router 'main' falls back to 'pool', which has no single member" in message
    assert await db.rows("SELECT count(*) FROM router_fallbacks") == [(1,)]


async def test_split_fallback_fails_too(db) -> None:
    a = await db.model("a")
    main = await db.route("main", a, retry_enabled=True, retry_max_attempts=2)
    target = await db.route("canary")
    await db.split(target, a)
    await db.fallback(main, target)
    assert "falls back to 'canary'" in await _failure(db)


async def test_all_problems_are_listed_together(db) -> None:
    a = await db.model("clash")
    b = await db.model("b")
    await db.route("clash", a, b)
    main = await db.route("main", a, retry_enabled=True, retry_max_attempts=2)
    pool = await db.route("pool", a, b)
    await db.fallback(main, pool)
    message = await _failure(db)
    assert "both named 'clash'" in message and "falls back to 'pool'" in message


async def test_downgrade_raises_irreversible(db) -> None:
    await migrate.upgrade(db.engine)
    with pytest.raises(NotImplementedError, match="irreversible"):
        await migrate.downgrade(db.engine, "0009")

"""The vocabulary-only migrations preserve existing rows and binary snapshots."""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from tests.conftest import create_database, drop_database

# Serialized with main's pre-rename snapshot.proto, not the renamed generated types.
LEGACY_SNAPSHOT = bytes.fromhex(
    "0801121f0a017510011a18687474703a2f2f6c6f63616c686f73743a383132332f7631"
    "1a100a01641201751a0661637475616c2007220d0a067075626c696312016418014a0764656661756c74"
)


async def _constraint_and_index_names(conn: AsyncConnection) -> tuple[set[str], set[str]]:
    constraints = set(
        await conn.scalars(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public'"
            )
        )
    )
    indexes = set(
        await conn.scalars(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
    )
    return constraints, indexes


async def test_0006_rows_survive_rename_roundtrip():
    url = await create_database()
    engine = create_async_engine(url)
    ids = {name: uuid.uuid4() for name in ("upstream", "model", "router", "fallback", "split")}
    try:
        await migrate.upgrade(engine, "0006")
        async with engine.begin() as conn:
            statements = (
                "INSERT INTO scopes (name) VALUES ('default')",
                "INSERT INTO upstreams (id, name, kind, base_url) "
                "VALUES (:upstream, 'u', 'openai', 'http://localhost:8123/v1')",
                "INSERT INTO deployments (id, name, upstream_id, upstream_model, weight) "
                "VALUES (:model, 'd', :upstream, 'actual', 7)",
                "INSERT INTO routes (id, model, retry_enabled, retry_max_attempts) "
                "VALUES (:router, 'public', true, 2), (:fallback, 'backup', false, NULL)",
                "INSERT INTO route_deployments (route_id, deployment_id, position) "
                "VALUES (:router, :model, 0), (:fallback, :model, 0)",
                "INSERT INTO route_fallbacks (route_id, fallback_route_id, position) "
                "VALUES (:router, :fallback, 0)",
                "INSERT INTO route_splits (id, route_id, name, weight, position) "
                "VALUES (:split, :router, 'stable', 100, 0)",
                "INSERT INTO route_split_deployments (split_id, deployment_id, position) "
                "VALUES (:split, :model, 0)",
                "INSERT INTO audit_log (actor_type, action, resource_type, resource_id) "
                "VALUES ('system', 'deployment.create', 'deployment', "
                "CAST(CAST(:model AS uuid) AS text)), "
                "('system', 'route.update', 'route', CAST(CAST(:router AS uuid) AS text))",
                "INSERT INTO usage_events "
                "(request_id, gateway_id, ts, key_id, team_id, owner_kind, owner_id, model, "
                "deployment, route, status, stream, prompt_tokens, completion_tokens, "
                "cost_nano_usd, cost_known, duration_ms, config_version, usage_state, outcome) "
                "VALUES (:router, 'gw', now(), :router, :router, 'user', :router, "
                "'public', 'd', 'chat_completions', 200, false, 10, 5, 42, true, 1, 1, "
                "'reported', 'completed')",
            )
            for sql in statements:
                await conn.execute(text(sql), ids)
            await conn.execute(
                text(
                    "INSERT INTO config_versions "
                    "(scope, version, payload, digest, size_bytes, key_count, "
                    "source_revision, kind) "
                    "VALUES ('default', 1, :payload, :digest, :size, 0, 0, 'compile')"
                ),
                {
                    "payload": LEGACY_SNAPSHOT,
                    "digest": hashlib.sha256(LEGACY_SNAPSHOT).digest(),
                    "size": len(LEGACY_SNAPSHOT),
                },
            )

        async with engine.connect() as conn:
            legacy_constraints, legacy_indexes = await _constraint_and_index_names(conn)

        def renamed(names: set[str]) -> set[str]:
            return {
                name.replace("uq_routes_model", "uq_routers_name")
                .replace("deployment", "model")
                .replace("routes", "routers")
                .replace("route_", "router_")
                for name in names
            }

        for _ in range(2):
            await migrate.upgrade(engine, "0008")
            async with engine.connect() as conn:
                constraints, indexes = await _constraint_and_index_names(conn)
                assert all(
                    "deployment" not in name and "route_" not in name and "routes" not in name
                    for name in constraints | indexes
                )
                assert constraints == renamed(legacy_constraints)
                assert indexes == renamed(legacy_indexes)
                assert "fk_router_split_models_split_id_router_splits" in constraints
                assert (await conn.execute(text("SELECT id, name, weight FROM models"))).one() == (
                    ids["model"],
                    "d",
                    7,
                )
                assert (
                    await conn.execute(
                        text("SELECT requested, model, endpoint, cost_nano_usd FROM usage_events")
                    )
                ).one() == ("public", "d", "chat_completions", 42)
                assert (
                    await conn.execute(
                        text("SELECT router_id, fallback_router_id, position FROM router_fallbacks")
                    )
                ).one() == (ids["router"], ids["fallback"], 0)
                assert (
                    await conn.execute(
                        text("SELECT split_id, model_id, position FROM router_split_models")
                    )
                ).one() == (ids["split"], ids["model"], 0)
                assert set(
                    (await conn.execute(text("SELECT action, resource_type FROM audit_log"))).all()
                ) == {("model.create", "model"), ("router.update", "router")}
                payload, digest = (
                    await conn.execute(text("SELECT payload, digest FROM config_versions"))
                ).one()
                assert payload == LEGACY_SNAPSHOT
                assert digest == hashlib.sha256(payload).digest()
                snapshot = pb.Snapshot.FromString(payload)
                assert snapshot.models[0].name == "d"
                assert snapshot.routers[0].name == "public"
            await migrate.downgrade(engine, "0006")
            async with engine.connect() as conn:
                assert await _constraint_and_index_names(conn) == (
                    legacy_constraints,
                    legacy_indexes,
                )
                assert (
                    await conn.execute(text("SELECT model, deployment, route FROM usage_events"))
                ).one() == ("public", "d", "chat_completions")
                assert set(
                    (await conn.execute(text("SELECT action, resource_type FROM audit_log"))).all()
                ) == {("deployment.create", "deployment"), ("route.update", "route")}
                assert await conn.scalar(text("SELECT id FROM deployments")) == ids["model"]
                assert await conn.scalar(text("SELECT count(*) FROM route_deployments")) == 2

    finally:
        await engine.dispose()
        await drop_database(url)

"""The sidecar migration upgrades a populated previous head without rewriting history."""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from tests.conftest import create_database, drop_database


async def test_populated_0008_upgrade_and_downgrade():
    url = await create_database()
    engine = create_async_engine(url)
    payload = b"previous-snapshot"
    digest = hashlib.sha256(payload).digest()
    ids = {name: uuid.uuid4() for name in ("upstream", "model", "router")}
    try:
        await migrate.upgrade(engine, "0008")
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO scopes (name) VALUES ('default')"))
            for statement in (
                "INSERT INTO upstreams (id, name, kind, base_url) "
                "VALUES (:upstream, 'u', 'openai', 'http://localhost/v1')",
                "INSERT INTO models (id, name, upstream_id, upstream_model, weight) "
                "VALUES (:model, 'm', :upstream, 'actual', 7)",
                "INSERT INTO routers (id, name) VALUES (:router, 'public')",
                "INSERT INTO router_models (router_id, model_id, position) "
                "VALUES (:router, :model, 0)",
            ):
                await conn.execute(text(statement), ids)
            await conn.execute(
                text(
                    "INSERT INTO config_versions (scope, version, payload, digest, size_bytes, "
                    "key_count, source_revision, kind) "
                    "VALUES ('default', 1, :payload, :digest, :size, 0, 0, 'compile')"
                ),
                {"payload": payload, "digest": digest, "size": len(payload)},
            )
        await migrate.upgrade(engine, "0009")
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT payload, digest, catalog_state FROM config_versions")
                )
            ).one() == (payload, digest, None)
            assert (await conn.execute(text("SELECT id, name, weight FROM models"))).one() == (
                ids["model"],
                "m",
                7,
            )
            assert (
                await conn.execute(text("SELECT router_id, model_id FROM router_models"))
            ).one() == (ids["router"], ids["model"])
        await migrate.downgrade(engine, "0008")
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT payload, digest FROM config_versions"))
            ).one() == (payload, digest)
        await migrate.upgrade(engine, "0009")
    finally:
        await engine.dispose()
        await drop_database(url)

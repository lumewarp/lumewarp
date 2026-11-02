"""Reproducible c5 review measurement (not a CI test).

Run ONLY under flock /tmp/lumewarp-e2e.lock:
  uv run python -m tests.usage.measure_review --output /tmp/lumewarp-c5-review-measure.json
Creates/drops a uniquely named test database, seeds 1M rows, compares the 03ad8b7
query/index set to the revised query/index set, and times real 1000-event ingest.
EXPLAIN ANALYZE BUFFERS plans and all trial timings are saved as JSON.
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import String, case, cast, distinct, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import aliased

from lumewarp_control_plane import migrate
from lumewarp_control_plane.api.usage import _events_query
from lumewarp_control_plane.models import CatalogKind, CatalogName, UsageEvent
from lumewarp_control_plane.usage import ingest
from tests.conftest import create_database, drop_database
from tests.usage.test_catalog_provenance import key_for, wire

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
OLD_INDEXES = {
    f"ix_usage_events_{field}_ts": f"({field}, ts)"
    for field in ("model_id", "alias_id", "router_id", "model", "alias", "router", "upstream_model")
}
NEW_INDEXES = {
    "ix_usage_events_model_id_ts": "(model_id, ts)",
    "ix_usage_events_team_model_id_ts": "(team_id, model_id, ts)",
    "ix_usage_events_alias_id_ts": "(alias_id, ts) WHERE alias_id IS NOT NULL",
    "ix_usage_events_router_id_ts": "(router_id, ts) WHERE router_id IS NOT NULL",
    "ix_usage_events_model_ts": "(model, ts)",
    "ix_usage_events_upstream_model_ts": "(upstream_model, ts)",
}


def before_query(groups, filters):
    """03ad8b7 _events_page statement, before pagination (verbatim expressions)."""
    fields = {
        "day": func.date_trunc("day", func.timezone("UTC", UsageEvent.ts)),
        "requested": UsageEvent.requested,
    }
    catalogs = {kind.value: aliased(CatalogName) for kind in CatalogKind}
    refs = {}
    for name, catalog in catalogs.items():
        fields[name] = case(
            (catalog.id.is_not(None), "id:" + cast(catalog.id, String)),
            else_="name:" + getattr(UsageEvent, name),
        )
        refs[name] = func.jsonb_build_object(
            "id", catalog.id, "name", func.coalesce(catalog.name, getattr(UsageEvent, name))
        )
    group_columns = [fields[name] for name in groups]
    aggregates = [
        func.count(),
        func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
        func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
        func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0),
        func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0),
    ]
    aggregates.extend(cast(func.jsonb_agg(distinct(refs[name])), JSONB) for name in catalogs)
    aggregates.append(func.array_agg(distinct(UsageEvent.upstream_model)))
    stmt = select(*group_columns, *aggregates).where(
        UsageEvent.ts >= START, UsageEvent.ts < END, *filters
    )
    for name, catalog in catalogs.items():
        stmt = stmt.outerjoin(
            catalog, (catalog.id == getattr(UsageEvent, f"{name}_id")) & (catalog.kind == name)
        )
    return stmt.group_by(*group_columns).order_by(*group_columns).limit(51)


async def indexes(engine, definitions):
    async with engine.begin() as conn:
        for name in OLD_INDEXES.keys() | NEW_INDEXES.keys():
            await conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        for name, definition in definitions.items():
            await conn.execute(text(f"CREATE INDEX {name} ON usage_events {definition}"))
        await conn.execute(text("ANALYZE usage_events"))


async def explain(conn, stmt):
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    raw = (await conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql))).scalar_one()
    return {"sql": sql, "explain": json.loads(raw) if isinstance(raw, str) else raw}


async def seed(engine):
    async with engine.begin() as conn:
        statements = [
            """INSERT INTO upstreams (id, name, kind, base_url)
              VALUES (md5('upstream')::uuid, 'upstream', 'openai', 'http://localhost/v1')""",
            """INSERT INTO catalog_names (id, name, kind)
              SELECT md5('model-' || i)::uuid, 'model-' || i, 'model'
              FROM generate_series(0,99) i""",
            """INSERT INTO models (name_id, upstream_id, upstream_model, created_at)
              SELECT id, md5('upstream')::uuid, 'upstream-' || substring(name FROM 7), '2026-01-01'
              FROM catalog_names WHERE kind = 'model'""",
            """INSERT INTO catalog_names (id, name, kind)
              SELECT md5('router-' || i)::uuid, 'router-' || i, 'router'
              FROM generate_series(0,9) i""",
            """INSERT INTO routers (name_id, created_at)
              SELECT id, '2026-01-01' FROM catalog_names WHERE kind = 'router'""",
            """INSERT INTO catalog_names (id, name, kind)
              SELECT md5('alias-' || i)::uuid, 'alias-' || i, 'alias'
              FROM generate_series(0,9) i""",
            """INSERT INTO aliases (name_id, target_name_id, target_kind, created_at)
              SELECT id, md5('model-0')::uuid, 'model', '2026-01-01'
              FROM catalog_names WHERE kind = 'alias'""",
            """INSERT INTO usage_events (request_id, gateway_id, ts, key_id, team_id,
                owner_kind, owner_id, requested, model, model_id, alias, alias_id,
                router, router_id, upstream, upstream_model, fallback, attempts,
                endpoint, status, stream, prompt_tokens, completion_tokens,
                cost_nano_usd, cost_known, duration_ms, config_version, usage_state, outcome)
              SELECT md5('request-' || g)::uuid, 'measurement',
                '2026-03-01'::timestamptz + ((g / 2000) % 30) * interval '1 day'
                  + (g % 2000) * interval '43 seconds',
                md5('key-' || ((g / 100) % 20))::uuid,
                md5('team-' || ((g / 100) % 20))::uuid, 'service_account',
                md5('owner-' || ((g / 100) % 20))::uuid,
                CASE WHEN (g / 2000) % 10 = 0 THEN 'alias-' || ((g / 20000) % 10)
                  WHEN (g / 2000) % 5 = 0 THEN 'router-' || ((g / 10000) % 10)
                  ELSE 'model-' || (g % 100) END,
                CASE WHEN g % 7 = 0 THEN 'old-model-' ELSE 'model-' END || (g % 100),
                md5('model-' || (g % 100))::uuid,
                CASE WHEN (g / 2000) % 10 = 0 THEN 'alias-' || ((g / 20000) % 10) ELSE '' END,
                CASE WHEN (g / 2000) % 10 = 0 THEN md5('alias-' || ((g / 20000) % 10))::uuid END,
                CASE WHEN (g / 2000) % 5 = 0 THEN 'router-' || ((g / 10000) % 10) ELSE '' END,
                CASE WHEN (g / 2000) % 5 = 0 THEN md5('router-' || ((g / 10000) % 10))::uuid END,
                'upstream', 'upstream-' || (g % 100), false, 1, 'chat_completions',
                200, false, 10, 5, 123, true, 10, 1, 'reported', 'completed'
              FROM generate_series(1, 1000000) g""",
        ]
        for statement in statements:
            await conn.execute(text(statement))
    # Allow index-only scans and make warm-cache comparisons repeatable.
    async with engine.connect() as conn:
        await conn.commit()
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE usage_events"))


async def measure(output):
    url = await create_database()
    # Docker's default 64MiB /dev/shm cannot support parallel VACUUM/index
    # maintenance here. Query parallelism is left at the server default.
    engine = create_async_engine(
        url, connect_args={"server_settings": {"max_parallel_maintenance_workers": "0"}}
    )
    result = {"seed_rows": 1000000, "query_repeats": 3, "ingest_events": 1000, "ingest_repeats": 5}
    try:
        await migrate.upgrade(engine)
        # Seed with the old index set to match the pre-review schema.
        await indexes(engine, OLD_INDEXES)
        await seed(engine)
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            key = await key_for(session)
        async with engine.connect() as conn:
            team_id = (await conn.execute(text("SELECT md5('team-0')::uuid"))).scalar_one()
            model_id = (await conn.execute(text("SELECT md5('model-0')::uuid"))).scalar_one()
            result["postgres"] = (await conn.execute(text("SELECT version()"))).scalar_one()
            result["settings"] = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT name, setting FROM pg_settings WHERE name IN "
                            "('shared_buffers', 'work_mem', 'max_parallel_workers_per_gather', "
                            "'max_parallel_maintenance_workers', 'jit')"
                        )
                    )
                ).all()
            )
        for stage, definitions in (("before", OLD_INDEXES), ("after", NEW_INDEXES)):
            if stage == "after":
                await indexes(engine, definitions)
            stage_result = {"queries": {}}
            async with engine.connect() as conn:
                for dimension, groups in (("default", ["day", "requested"]), ("model", ["model"])):
                    for scope in ("admin", "team"):
                        filters = [UsageEvent.team_id == team_id] if scope == "team" else []
                        if stage == "before":
                            stmt = before_query(groups, filters)
                        else:
                            stmt, fields, _, _ = _events_query(groups, None, START, END, filters)
                            stmt = stmt.order_by(*fields.values()).limit(51)
                        # One unrecorded warmup, then three measured executions.
                        await explain(conn, stmt)
                        trials = [await explain(conn, stmt) for _ in range(3)]
                        stage_result["queries"][f"{dimension}_{scope}"] = trials
                        print(
                            stage,
                            dimension,
                            scope,
                            [t["explain"][0]["Execution Time"] for t in trials],
                            flush=True,
                        )
                filters = [UsageEvent.team_id == team_id, UsageEvent.model_id == model_id]
                if stage == "before":
                    stmt = before_query(["model"], filters)
                else:
                    stmt, fields, _, _ = _events_query(["model"], None, START, END, filters)
                    stmt = stmt.order_by(*fields.values()).limit(51)
                stage_result["team_model_filter"] = await explain(conn, stmt)
                stage_result["index_bytes"] = (
                    await conn.execute(text("SELECT pg_indexes_size('usage_events')"))
                ).scalar_one()
                stage_result["indexes"] = dict(
                    (
                        await conn.execute(
                            text(
                                "SELECT indexname, indexdef FROM pg_indexes "
                                "WHERE tablename = 'usage_events'"
                            )
                        )
                    ).all()
                )
            latencies = []
            for trial in range(6):
                events = [
                    wire(
                        key,
                        model=f"model-{i % 100}",
                        alias=f"alias-{i % 10}" if i % 10 == 0 else "",
                        router=f"router-{i % 10}" if i % 5 == 0 else "",
                        upstream="upstream",
                        upstream_model=f"upstream-{i % 100}",
                        attempts=1,
                    )
                    for i in range(1000)
                ]
                start = time.perf_counter()
                async with AsyncSession(engine) as session, session.begin():
                    assert await ingest(session, "measurement-ingest", events) == 1000
                elapsed = (time.perf_counter() - start) * 1000
                if trial:
                    latencies.append(elapsed)
            stage_result["ingest_ms"] = latencies
            stage_result["ingest_median_ms"] = statistics.median(latencies)
            result[stage] = stage_result
            print(stage, "ingest_ms", latencies, "median", statistics.median(latencies), flush=True)
            output.write_text(json.dumps(result, indent=2))
    finally:
        await engine.dispose()
        await drop_database(url)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(measure(args.output))

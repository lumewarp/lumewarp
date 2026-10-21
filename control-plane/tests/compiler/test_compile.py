from datetime import UTC, datetime, timedelta

import pytest

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import CompileError, compile_scope, narrow
from lumewarp_control_plane.models import (
    KeyLifecycle,
    ModelAccess,
    RoutingStrategy,
    ScopeType,
    UsageTotal,
)
from tests.compiler.helpers import OCT, SCOPE, add_hierarchy, add_key, add_route, seed_scope

ALL, INHERIT, LIST = ModelAccess.ALL, ModelAccess.INHERIT, ModelAccess.LIST


def lvl(mode: ModelAccess, *models: str) -> dict:
    return {"model_access": mode, "allowed_models": list(models)}


async def compile_(factory, **kw):
    async with factory() as s:
        return await compile_scope(s, SCOPE, now=kw.pop("now", OCT), router_plugins=[], **kw)


def key_of(compiled, key) -> pb.ApiKey:
    return next(k for k in compiled.snapshot.api_keys if k.key_id == str(key.id))


# --------------------------------------------------------------------------- allowlists


@pytest.mark.parametrize(
    ("levels", "allow_all", "models"),
    [
        # Defaults: org ALL, everything below INHERIT -> universal.
        ([lvl(ALL), {}, {}, {}], True, []),
        ([lvl(ALL), lvl(INHERIT), lvl(ALL), lvl(INHERIT)], True, []),
        ([lvl(LIST, "a", "b"), {}, {}, {}], False, ["a", "b"]),
        ([lvl(LIST, "a", "b"), lvl(LIST, "b", "c"), {}, {}], False, ["b"]),
        # ALL below a LIST does not widen it.
        ([lvl(LIST, "a"), lvl(ALL), lvl(ALL), lvl(ALL)], False, ["a"]),
        # Disjoint lists intersect to deny-all, never to "all".
        ([lvl(ALL), lvl(LIST, "a"), lvl(LIST, "b"), {}], False, []),
        # Explicit empty list is deny-all.
        ([lvl(ALL), {}, {}, lvl(LIST)], False, []),
        ([lvl(LIST), lvl(ALL), lvl(ALL), lvl(ALL)], False, []),
        # Empty names are dropped; order and duplicates normalized.
        ([lvl(LIST, "z", "a", "", "a"), {}, {}, {}], False, ["a", "z"]),
    ],
)
async def test_allowlist_narrowing(factory, levels, allow_all, models) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s, org=levels[0], team=levels[1], project=levels[2])
        key = await add_key(s, p, **levels[3])
    k = key_of(await compile_(factory), key)
    assert (k.policy.allow_all_models, list(k.policy.allowed_models)) == (allow_all, models)


def test_narrow_unit() -> None:
    assert narrow(None, INHERIT, []) is None
    assert narrow(frozenset({"a"}), ALL, []) == {"a"}
    assert narrow(None, LIST, []) == frozenset()


# --------------------------------------------------------------------------- limits


async def test_rate_limit_scopes(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        o, t, p = await add_hierarchy(
            s, org={"rpm_limit": 600}, team={"tpm_limit": 10_000, "max_parallel": 4}
        )
        k1 = await add_key(s, p, name="k1", rpm_limit=60)
        k2 = await add_key(s, p, name="k2")
    c = await compile_(factory)
    scopes = {k: [(r.id, r.limit) for r in key_of(c, k).policy.rate_limit_scopes] for k in (k1, k2)}
    shared = [
        (f"org:{o.id}", pb.RateLimit(requests_per_minute=600)),
        (f"team:{t.id}", pb.RateLimit(tokens_per_minute=10_000, max_parallel_requests=4)),
    ]
    assert scopes[k2] == shared
    assert scopes[k1] == [*shared, (f"key:{k1.id}", pb.RateLimit(requests_per_minute=60))]
    assert f"project:{p.id}" not in str(scopes)


# --------------------------------------------------------------------------- status


async def _spend(factory, level: ScopeType, entity_id, cost: int, window: datetime) -> None:
    async with factory() as s, s.begin():
        s.add(UsageTotal(scope_type=level, scope_id=entity_id, window_start=window,
                         cost_nano_usd=cost))  # fmt: skip


async def test_budget_status_and_rollover(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s, project={"budget_nano_usd": 1000})
        key = await add_key(s, p)
        _, _, p2 = await add_hierarchy(s, name="other", project={"budget_nano_usd": 1000})
        other = await add_key(s, p2)
    oct1, nov1 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    await _spend(factory, ScopeType.PROJECT, p.id, 1000, oct1)  # exactly at budget
    await _spend(factory, ScopeType.PROJECT, p2.id, 999, oct1)

    c = await compile_(factory, now=nov1 - timedelta(milliseconds=1))
    k = key_of(c, key)
    assert k.status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert k.policy.budget_window_end_unix_ms == int(nov1.timestamp() * 1000)
    assert key_of(c, other).status == pb.KEY_STATUS_ACTIVE

    # New month: last month's spend no longer blocks.
    c = await compile_(factory, now=nov1)
    assert key_of(c, key).status == pb.KEY_STATUS_ACTIVE
    assert key_of(c, key).policy.budget_window_end_unix_ms == 0


@pytest.mark.parametrize("level", [ScopeType.ORG, ScopeType.TEAM, ScopeType.KEY])
async def test_any_ancestor_budget_blocks(factory, level) -> None:
    await seed_scope(factory)
    budget = {"budget_nano_usd": 5}
    async with factory() as s, s.begin():
        o, t, p = await add_hierarchy(
            s,
            org=budget if level is ScopeType.ORG else None,
            team=budget if level is ScopeType.TEAM else None,
        )
        k1 = await add_key(s, p, name="k1", **(budget if level is ScopeType.KEY else {}))
        k2 = await add_key(s, p, name="k2")
    entity = {ScopeType.ORG: o, ScopeType.TEAM: t, ScopeType.KEY: k1}[level]
    await _spend(factory, level, entity.id, 6, datetime(2026, 10, 1, tzinfo=UTC))
    c = await compile_(factory)
    assert key_of(c, k1).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    expected = pb.KEY_STATUS_ACTIVE if level is ScopeType.KEY else pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, k2).status == expected


async def test_lifecycle_beats_budget(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        o, _, p = await add_hierarchy(s, org={"budget_nano_usd": 0})
        suspended = await add_key(s, p, name="s", lifecycle=KeyLifecycle.SUSPENDED)
        revoked = await add_key(
            s, p, name="r", lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC)
        )
        active = await add_key(s, p, name="a")
    c = await compile_(factory)
    assert key_of(c, suspended).status == pb.KEY_STATUS_SUSPENDED
    assert key_of(c, active).status == pb.KEY_STATUS_BUDGET_EXCEEDED  # budget 0 = no spend
    assert str(revoked.id) not in {k.key_id for k in c.snapshot.api_keys}


async def test_key_fields(factory) -> None:
    await seed_scope(factory)
    expires = datetime(2027, 1, 1, tzinfo=UTC)
    async with factory() as s, s.begin():
        o, t, p = await add_hierarchy(s)
        key = await add_key(s, p, expires_at=expires)
    c = await compile_(factory)
    k = key_of(c, key)
    assert k.key_hash == key.key_hash and len(k.key_hash) == 32
    assert (k.org_id, k.team_id, k.project_id) == (str(o.id), str(t.id), str(p.id))
    assert k.expires_at_unix_ms == int(expires.timestamp() * 1000)
    assert list(c.snapshot.teams) == [
        pb.Team(id=str(t.id), name=t.name, org_id=str(o.id), org_name=o.name)
    ]
    assert c.key_count == 1 and c.snapshot.scope == SCOPE


# --------------------------------------------------------------------------- routing


async def test_routing(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_route(s, "llama", [("llama-b", 5), ("llama-a", None)])
        await add_route(s, "empty", [])
        await add_route(s, "fast", [("llama-a", None)], strategy=RoutingStrategy.LOWEST_LATENCY)
        await add_route(s, "unknown", [("llama-a", None)])
    c = await compile_(factory)
    snap = c.snapshot
    assert [r.model for r in snap.routes] == ["llama"]
    assert list(snap.routes[0].deployments) == ["llama-b", "llama-a"]  # position order kept
    assert snap.routes[0].strategy == pb.ROUTING_STRATEGY_WEIGHTED
    assert len(c.warnings) == 3
    deps = {d.name: d for d in snap.deployments}
    assert not deps["llama-a"].HasField("price")  # unknown, not free
    assert deps["llama-b"].price == pb.Price(input_nano_usd_per_mtok=5, output_nano_usd_per_mtok=5)
    assert [(u.name, u.kind) for u in snap.upstreams] == [("vllm", pb.UPSTREAM_KIND_VLLM)]
    assert snap.upstreams[0].stream_usage_mode == pb.STREAM_USAGE_MODE_INCLUDE_USAGE
    assert list(snap.required_capabilities) == []


async def test_deterministic(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s, org=lvl(LIST, "b", "a"))
        for i in range(5):
            await add_key(s, p, name=f"k{i}", rpm_limit=10 + i)
        await add_route(s, "m", [("d2", 1), ("d1", 2)])
    a = (await compile_(factory)).snapshot.SerializeToString(deterministic=True)
    b = (await compile_(factory)).snapshot.SerializeToString(deterministic=True)
    assert a == b


async def test_limits(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s)
        await add_key(s, p, name="k1")
        await add_key(s, p, name="k2")
    with pytest.raises(CompileError, match="keys exceed"):
        await compile_(factory, max_snapshot_keys=1)
    with pytest.raises(CompileError, match="bytes"):
        await compile_(factory, max_snapshot_bytes=100)
    assert (await compile_(factory, max_snapshot_keys=2)).key_count == 2


async def test_unknown_scope(factory) -> None:
    with pytest.raises(CompileError, match="unknown config scope"):
        await compile_(factory)

"""P2 routing fields in the compiled snapshot and their capabilities.

The capability rule mirrors gateway/crates/lumewarp-gateway/src/snapshot.rs::validate: a
capability is listed iff a field that needs it is set, and a snapshot whose fields need an
unlisted capability is INVALID_CONFIG there (and a SnapshotInvalid here).
"""

import pytest
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.compiler.validate import (
    SUPPORTED_CAPABILITIES,
    SnapshotInvalid,
    capabilities_used,
    validate_snapshot,
)
from lumewarp_control_plane.models import (
    Model,
    Router,
    RouterFallback,
    RouterSplit,
    RouterSplitModel,
    RoutingStrategy,
    StickyBy,
    Upstream,
)
from tests.compiler.helpers import OCT, SCOPE, add_router, seed_scope


async def compile_(factory):
    async with factory() as s:
        return await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)


def router_of(snap: pb.Snapshot, name: str) -> pb.Router:
    return next(r for r in snap.routers if r.name == name)


async def _dep(s, name: str) -> Model:
    return await s.scalar(select(Model).where(Model.name == name))


async def _split(s, router: Router, name: str, weight: int, deps: list[str], position: int):
    split = RouterSplit(router_id=router.id, name=name, weight=weight, position=position)
    s.add(split)
    await s.flush()
    for pos, dep in enumerate(deps):
        d = await _dep(s, dep)
        s.add(RouterSplitModel(split_id=split.id, model_id=d.id, position=pos))
    await s.flush()


def test_supported_capabilities_match_gateway() -> None:
    assert {
        "retry",
        "fallbacks",
        "health_check",
        "circuit_breaker",
        "traffic_split",
        "lowest_latency",
        "vllm_load",
    } == SUPPORTED_CAPABILITIES


async def test_plain_routing_needs_no_capability(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
    snap = (await compile_(factory)).snapshot
    assert list(snap.required_capabilities) == []
    up, router = snap.upstreams[0], snap.routers[0]
    assert not up.HasField("health_check") and not up.HasField("circuit_breaker")
    assert up.metrics_url == ""
    assert not router.HasField("retry") and not router.fallbacks and not router.traffic_split
    assert router.sticky == pb.STICKY_BY_NONE and router.sticky_header == ""


async def test_health_check(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        await add_router(s, "other", [("b", None)], upstream="openai")
        for name, path, unhealthy in (("vllm", "/health", 2), ("openai", None, None)):
            up = await s.scalar(select(Upstream).where(Upstream.name == name))
            up.health_check_path = path
            up.health_check_interval_ms = 5000
            up.health_check_timeout_ms = 1000
            up.health_check_unhealthy_threshold = unhealthy
    snap = (await compile_(factory)).snapshot
    ups = {u.name: u for u in snap.upstreams}
    assert ups["vllm"].health_check == pb.HealthCheck(
        path="/health", interval_ms=5000, timeout_ms=1000, unhealthy_threshold=2
    )
    # NULL path / thresholds are the gateway defaults (0 / "").
    assert ups["openai"].health_check == pb.HealthCheck(interval_ms=5000, timeout_ms=1000)
    assert list(snap.required_capabilities) == ["health_check"]


async def test_circuit_breaker(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        up = await s.scalar(select(Upstream))
        up.circuit_breaker_enabled = True
        up.circuit_failure_threshold = 3
    snap = (await compile_(factory)).snapshot
    assert snap.upstreams[0].HasField("circuit_breaker")
    assert snap.upstreams[0].circuit_breaker == pb.CircuitBreaker(failure_threshold=3)
    assert list(snap.required_capabilities) == ["circuit_breaker"]


async def test_metrics_url_needs_vllm_load(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        up = await s.scalar(select(Upstream))
        up.metrics_url = "http://vllm:8000/metrics"
    snap = (await compile_(factory)).snapshot
    assert snap.upstreams[0].metrics_url == "http://vllm:8000/metrics"
    assert list(snap.required_capabilities) == ["vllm_load"]


async def test_invalid_metrics_url_is_left_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        up = await s.scalar(select(Upstream))
        up.metrics_url = "http://user:pw@vllm/metrics"
    c = await compile_(factory)
    assert c.snapshot.upstreams[0].metrics_url == ""
    assert list(c.snapshot.required_capabilities) == []
    assert any("metrics_url" in w for w in c.warnings)


@pytest.mark.parametrize(
    ("strategy", "wire", "caps"),
    [
        (RoutingStrategy.WEIGHTED, pb.ROUTING_STRATEGY_WEIGHTED, []),
        (RoutingStrategy.LEAST_IN_FLIGHT, pb.ROUTING_STRATEGY_LEAST_IN_FLIGHT, []),
        (RoutingStrategy.LOWEST_LATENCY, pb.ROUTING_STRATEGY_LOWEST_LATENCY, ["lowest_latency"]),
        (RoutingStrategy.VLLM_LOAD, pb.ROUTING_STRATEGY_VLLM_LOAD, ["vllm_load"]),
    ],
)
async def test_strategies(factory, strategy, wire, caps) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)], strategy=strategy)
    snap = (await compile_(factory)).snapshot
    assert snap.routers[0].strategy == wire
    assert list(snap.required_capabilities) == caps


async def test_retry_policy(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        default = await add_router(s, "default", [("a", None)])
        default.retry_enabled = True
        tuned = await add_router(s, "tuned", [("a", None), ("b", None)])
        tuned.retry_enabled = True
        tuned.retry_max_attempts = 3
        tuned.retry_on_status = [503, 500]
        tuned.retry_total_timeout_ms = 9000
    snap = (await compile_(factory)).snapshot
    # max_attempts NULL is emitted as 0 = the gateway default (2 attempts).
    assert router_of(snap, "default").retry == pb.RetryPolicy()
    assert router_of(snap, "default").HasField("retry")
    assert router_of(snap, "tuned").retry == pb.RetryPolicy(
        max_attempts=3, retry_on_status=[500, 503], total_timeout_ms=9000
    )
    assert list(snap.required_capabilities) == ["retry"]


async def test_fallbacks_ordered(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)])
        b = await add_router(s, "zzz-backup", [("b", None)])
        c = await add_router(s, "aaa-backup", [("c", None)])
        s.add(RouterFallback(router_id=main.id, fallback_router_id=b.id, position=0))
        s.add(RouterFallback(router_id=main.id, fallback_router_id=c.id, position=1))
    snap = (await compile_(factory)).snapshot
    assert list(router_of(snap, "main").fallbacks) == ["zzz-backup", "aaa-backup"]
    assert list(snap.required_capabilities) == ["fallbacks"]


async def test_fallback_to_left_out_router_is_dropped(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)])
        empty = await add_router(s, "empty", [])
        s.add(RouterFallback(router_id=main.id, fallback_router_id=empty.id, position=0))
    c = await compile_(factory)
    assert [r.name for r in c.snapshot.routers] == ["main"]
    assert list(c.snapshot.routers[0].fallbacks) == []
    assert list(c.snapshot.required_capabilities) == []
    assert any("fallback 'empty'" in w for w in c.warnings)


async def test_traffic_split_and_sticky(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        router = await add_router(s, "llama", [("stable-a", None)])
        await add_router(s, "canary", [("canary-a", None)])
        await _split(s, router, "stable", 90, ["stable-a"], 0)
        await _split(s, router, "canary", 10, ["canary-a"], 1)
        router.sticky = StickyBy.HEADER
        router.sticky_header = "x-session-id"
    snap = (await compile_(factory)).snapshot
    r = router_of(snap, "llama")
    assert list(r.traffic_split) == [
        pb.TrafficSplit(name="stable", weight=90, models=["stable-a"]),
        pb.TrafficSplit(name="canary", weight=10, models=["canary-a"]),
    ]
    assert r.sticky == pb.STICKY_BY_HEADER and r.sticky_header == "x-session-id"
    assert list(snap.required_capabilities) == ["traffic_split"]


@pytest.mark.parametrize(
    ("sticky", "wire"), [(StickyBy.TEAM, pb.STICKY_BY_TEAM), (StickyBy.KEY, pb.STICKY_BY_KEY)]
)
async def test_sticky_team_key(factory, sticky, wire) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        router = await add_router(s, "llama", [])
        await add_router(s, "other", [("a", None), ("b", None)])
        await _split(s, router, "one", 1, ["a"], 0)
        await _split(s, router, "two", 1, ["b"], 1)
        router.sticky = sticky
    snap = (await compile_(factory)).snapshot
    r = router_of(snap, "llama")
    # A split router may have no own models.
    assert list(r.models) == [] and r.sticky == wire and r.sticky_header == ""
    assert list(snap.required_capabilities) == ["traffic_split"]


async def test_split_without_valid_models_leaves_router_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        router = await add_router(s, "llama", [("a", None)])
        await add_router(s, "bad", [("b", None)], upstream="broken")
        await _split(s, router, "one", 1, ["a"], 0)
        await _split(s, router, "two", 1, ["b"], 1)
        broken = await s.scalar(select(Upstream).where(Upstream.name == "broken"))
        broken.base_url = "ftp://broken"
    c = await compile_(factory)
    assert [r.name for r in c.snapshot.routers] == []
    assert any("split 'two' has no models" in w for w in c.warnings)


async def test_weighted_split_with_zero_weight_leaves_router_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        router = await add_router(s, "llama", [("a", None)])
        await add_router(s, "zero", [("z", None)], strategy=RoutingStrategy.LEAST_IN_FLIGHT)
        (await _dep(s, "z")).weight = 0
        await _split(s, router, "one", 1, ["a"], 0)
        await _split(s, router, "two", 1, ["z"], 1)
    c = await compile_(factory)
    assert [r.name for r in c.snapshot.routers] == ["zero"]
    assert any("zero total weight" in w for w in c.warnings)


async def test_all_capabilities_together(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)], strategy=RoutingStrategy.VLLM_LOAD)
        fb = await add_router(s, "fb", [("b", None)], strategy=RoutingStrategy.LOWEST_LATENCY)
        main.retry_enabled = True
        s.add(RouterFallback(router_id=main.id, fallback_router_id=fb.id, position=0))
        await _split(s, main, "only", 1, ["a"], 0)
        up = await s.scalar(select(Upstream))
        up.health_check_interval_ms = 2000
        up.health_check_timeout_ms = 500
        up.circuit_breaker_enabled = True
    snap = (await compile_(factory)).snapshot
    assert list(snap.required_capabilities) == sorted(SUPPORTED_CAPABILITIES)


# --------------------------------------------------------------------------- validate mirror


def _base() -> pb.Snapshot:
    return pb.Snapshot(
        upstreams=[pb.Upstream(name="u", kind=pb.UPSTREAM_KIND_VLLM, base_url="http://u/v1")],
        models=[
            pb.Model(name="a", upstream="u", upstream_model="m", weight=1),
            pb.Model(name="b", upstream="u", upstream_model="m", weight=1),
        ],
        routers=[
            pb.Router(name="r", models=["a"], strategy=pb.ROUTING_STRATEGY_WEIGHTED),
            pb.Router(name="s", models=["b"], strategy=pb.ROUTING_STRATEGY_WEIGHTED),
        ],
    )


def _check(snap: pb.Snapshot, *, caps: bool = True) -> None:
    if caps:
        del snap.required_capabilities[:]
        snap.required_capabilities.extend(capabilities_used(snap))
    validate_snapshot(snap, max_bytes=1 << 26, max_keys=1000)


def _split_router(snap: pb.Snapshot, *splits: pb.TrafficSplit) -> pb.Router:
    r = snap.routers[0]
    r.traffic_split.extend(splits)
    return r


INVALID = {
    "health interval": lambda s: s.upstreams[0].health_check.CopyFrom(
        pb.HealthCheck(interval_ms=999, timeout_ms=100)
    ),
    "health timeout": lambda s: s.upstreams[0].health_check.CopyFrom(
        pb.HealthCheck(interval_ms=1000, timeout_ms=1000)
    ),
    "health zero timeout": lambda s: s.upstreams[0].health_check.CopyFrom(
        pb.HealthCheck(interval_ms=1000)
    ),
    "health path": lambda s: s.upstreams[0].health_check.CopyFrom(
        pb.HealthCheck(path="/x?y", interval_ms=1000, timeout_ms=10)
    ),
    "metrics url": lambda s: setattr(s.upstreams[0], "metrics_url", "ftp://u/metrics"),
    "retry attempts": lambda s: s.routers[0].retry.CopyFrom(pb.RetryPolicy(max_attempts=6)),
    "retry status": lambda s: s.routers[0].retry.CopyFrom(pb.RetryPolicy(retry_on_status=[600])),
    "self fallback": lambda s: s.routers[0].fallbacks.append("r"),
    "unknown fallback": lambda s: s.routers[0].fallbacks.append("nope"),
    "sticky without split": lambda s: setattr(s.routers[0], "sticky", pb.STICKY_BY_TEAM),
    "header without name": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, models=["a"])),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_HEADER),
    ),
    "header with other sticky": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, models=["a"])),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_KEY),
        setattr(s.routers[0], "sticky_header", "x-a"),
    ),
    "bad header name": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, models=["a"])),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_HEADER),
        setattr(s.routers[0], "sticky_header", "x a"),
    ),
    "split name charset": lambda s: _split_router(
        s, pb.TrafficSplit(name="no space", weight=1, models=["a"])
    ),
    "split name length": lambda s: _split_router(
        s, pb.TrafficSplit(name="x" * 33, weight=1, models=["a"])
    ),
    "split duplicate": lambda s: _split_router(
        s,
        pb.TrafficSplit(name="x", weight=1, models=["a"]),
        pb.TrafficSplit(name="x", weight=1, models=["b"]),
    ),
    "split empty": lambda s: _split_router(s, pb.TrafficSplit(name="x", weight=1)),
    "split unknown model": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=1, models=["zz"])
    ),
    "split repeats model": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=1, models=["a", "a"])
    ),
    "split weights zero": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=0, models=["a"])
    ),
    "too many splits": lambda s: _split_router(
        s, *(pb.TrafficSplit(name=f"s{i}", weight=1, models=["a"]) for i in range(17))
    ),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_validate_rejects_what_the_gateway_rejects(mutate) -> None:
    snap = _base()
    mutate(snap)
    with pytest.raises(SnapshotInvalid):
        _check(snap)


def test_validate_accepts_full_p2_snapshot() -> None:
    snap = _base()
    snap.upstreams[0].health_check.CopyFrom(pb.HealthCheck(interval_ms=1000, timeout_ms=999))
    snap.upstreams[0].circuit_breaker.CopyFrom(pb.CircuitBreaker())
    snap.upstreams[0].metrics_url = "http://u/metrics"
    r = _split_router(
        snap,
        *(pb.TrafficSplit(name=f"s-{i}.x_", weight=i, models=["a", "b"]) for i in range(16)),
    )
    r.sticky = pb.STICKY_BY_HEADER
    r.sticky_header = "x-session"
    r.retry.CopyFrom(pb.RetryPolicy(max_attempts=5, retry_on_status=[400, 599]))
    r.fallbacks.append("s")
    snap.routers[1].strategy = pb.ROUTING_STRATEGY_VLLM_LOAD
    _check(snap)
    assert list(snap.required_capabilities) == sorted(SUPPORTED_CAPABILITIES - {"lowest_latency"})


@pytest.mark.parametrize(
    ("mutate", "cap"),
    [
        (
            lambda s: s.upstreams[0].health_check.CopyFrom(
                pb.HealthCheck(interval_ms=1000, timeout_ms=1)
            ),
            "health_check",
        ),
        (lambda s: s.upstreams[0].circuit_breaker.CopyFrom(pb.CircuitBreaker()), "circuit_breaker"),
        (lambda s: setattr(s.upstreams[0], "metrics_url", "http://u/m"), "vllm_load"),
        (lambda s: s.routers[0].retry.CopyFrom(pb.RetryPolicy()), "retry"),
        (lambda s: s.routers[0].fallbacks.append("s"), "fallbacks"),
        (
            lambda s: _split_router(s, pb.TrafficSplit(name="x", weight=1, models=["a"])),
            "traffic_split",
        ),
        (
            lambda s: setattr(s.routers[0], "strategy", pb.ROUTING_STRATEGY_LOWEST_LATENCY),
            "lowest_latency",
        ),
        (lambda s: setattr(s.routers[0], "strategy", pb.ROUTING_STRATEGY_VLLM_LOAD), "vllm_load"),
    ],
)
def test_field_without_capability_is_invalid(mutate, cap) -> None:
    snap = _base()
    mutate(snap)
    assert capabilities_used(snap) == [cap]
    with pytest.raises(SnapshotInvalid, match="require capabilities"):
        _check(snap, caps=False)
    _check(snap)


def test_unknown_capability_is_invalid() -> None:
    snap = _base()
    snap.required_capabilities.append("jwt")
    with pytest.raises(SnapshotInvalid, match="unsupported"):
        _check(snap, caps=False)

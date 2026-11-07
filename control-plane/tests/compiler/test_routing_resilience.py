"""Routing resilience fields in the compiled snapshot and their capabilities.

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
    RouterSplitMember,
    StickyBy,
    Upstream,
)
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, add_router, seed_scope


def _m(*names: str) -> list[pb.RouterMember]:
    return [pb.RouterMember(model=name, weight=1) for name in names]


async def compile_(factory):
    async with factory() as s:
        return await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)


def router_of(snap: pb.Snapshot, name: str) -> pb.Router:
    return next(r for r in snap.routers if r.name == name)


async def _dep(s, name: str) -> Model:
    return await s.scalar(select(Model).where(Model.name == name))


async def _split(
    s,
    router: Router,
    name: str,
    weight: int,
    deps: list[str],
    position: int,
    member_weight: int = 1,
):
    split = RouterSplit(router_id=router.id, name=name, weight=weight, position=position)
    s.add(split)
    await s.flush()
    for pos, dep in enumerate(deps):
        d = await _dep(s, dep)
        s.add(
            RouterSplitMember(split_id=split.id, model_id=d.id, weight=member_weight, position=pos)
        )
    await s.flush()


def test_supported_capabilities_match_gateway() -> None:
    assert {
        "retry",
        "fallbacks",
        "health_check",
        "circuit_breaker",
        "traffic_split",
        "vllm_load",
        "rotation_grace",
        "encrypted_api_key",
        "model_limits",
        "default_body",
        "reasoning_effort",
        "model_chat_check",
        "strategy:weighted",
        "strategy:least_in_flight",
        "strategy:lowest_latency",
        "strategy:vllm_load",
    } == SUPPORTED_CAPABILITIES


async def test_plain_routing_needs_only_its_strategy(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
    snap = (await compile_(factory)).snapshot
    assert list(snap.required_capabilities) == ["strategy:weighted"]
    up, router = snap.upstreams[0], router_of(snap, "llama")
    assert not up.HasField("health_check") and not up.HasField("circuit_breaker")
    assert up.metrics_url == ""
    assert not router.HasField("retry") and not router.fallbacks and not router.traffic_split
    assert router.sticky == pb.STICKY_BY_NONE and router.sticky_header == ""


async def test_health_check(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        await add_router(s, "other", [("b", None)], upstream="openai")
        for name, url, unhealthy in (
            ("vllm", "http://vllm.internal:8000/health", 2),
            ("openai", None, None),
        ):
            up = await s.scalar(select(Upstream).where(Upstream.name == name))
            up.health_url = url
            up.health_check_interval_ms = 5000
            up.health_check_timeout_ms = 1000
            up.health_check_unhealthy_threshold = unhealthy
    snap = (await compile_(factory)).snapshot
    ups = {u.name: u for u in snap.upstreams}
    assert ups["vllm"].health_check == pb.HealthCheck(
        url="http://vllm.internal:8000/health",
        interval_ms=5000,
        timeout_ms=1000,
        unhealthy_threshold=2,
    )
    # NULL url / thresholds are the gateway defaults (0 / "").
    assert ups["openai"].health_check == pb.HealthCheck(interval_ms=5000, timeout_ms=1000)
    assert list(snap.required_capabilities) == ["health_check", "strategy:weighted"]


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
    assert list(snap.required_capabilities) == ["circuit_breaker", "strategy:weighted"]


async def test_metrics_url_needs_vllm_load(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        up = await s.scalar(select(Upstream))
        up.metrics_url = "http://vllm:8000/metrics"
    snap = (await compile_(factory)).snapshot
    assert snap.upstreams[0].metrics_url == "http://vllm:8000/metrics"
    assert list(snap.required_capabilities) == ["strategy:weighted", "vllm_load"]


async def test_invalid_metrics_url_is_left_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)])
        up = await s.scalar(select(Upstream))
        up.metrics_url = "http://user:pw@vllm/metrics"
    c = await compile_(factory)
    assert c.snapshot.upstreams[0].metrics_url == ""
    assert list(c.snapshot.required_capabilities) == ["strategy:weighted"]
    assert any("metrics_url" in w for w in c.warnings)


@pytest.mark.parametrize(
    "strategy",
    ["weighted", "least_in_flight", "lowest_latency", "vllm_load"],
)
async def test_strategies(factory, strategy) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "llama", [("a", None)], strategy=strategy)
    snap = (await compile_(factory)).snapshot
    assert router_of(snap, "llama").strategy == strategy
    assert list(snap.required_capabilities) == [f"strategy:{strategy}"]


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
    assert list(snap.required_capabilities) == ["retry", "strategy:weighted"]


async def test_fallbacks_ordered(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)])
        b = await factories.make_model(s, "zzz-backup")
        c = await factories.make_model(s, "aaa-backup")
        s.add(RouterFallback(router_id=main.id, model_id=b.id, position=0))
        s.add(RouterFallback(router_id=main.id, model_id=c.id, position=1))
    snap = (await compile_(factory)).snapshot
    # Fallbacks are models, in the snapshot on their own, with no router around them.
    assert list(router_of(snap, "main").fallbacks) == ["zzz-backup", "aaa-backup"]
    assert [r.name for r in snap.routers] == ["main"]
    assert {"zzz-backup", "aaa-backup"} <= {m.name for m in snap.models}
    assert list(snap.required_capabilities) == ["fallbacks", "strategy:weighted"]


async def test_fallback_to_left_out_model_is_dropped(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)])
        broken = await factories.make_upstream(s, "broken", base_url="ftp://broken")
        bad = await factories.make_model(s, "bad", broken)
        s.add(RouterFallback(router_id=main.id, model_id=bad.id, position=0))
    c = await compile_(factory)
    assert [r.name for r in c.snapshot.routers] == ["main"]
    assert list(router_of(c.snapshot, "main").fallbacks) == []
    assert list(c.snapshot.required_capabilities) == ["strategy:weighted"]
    assert any("fallback 'bad'" in w for w in c.warnings)


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
        pb.TrafficSplit(name="stable", weight=90, members=_m("stable-a")),
        pb.TrafficSplit(name="canary", weight=10, members=_m("canary-a")),
    ]
    assert r.sticky == pb.STICKY_BY_HEADER and r.sticky_header == "x-session-id"
    assert list(snap.required_capabilities) == ["strategy:weighted", "traffic_split"]


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
    assert list(r.members) == [] and r.sticky == wire and r.sticky_header == ""
    assert list(snap.required_capabilities) == ["strategy:weighted", "traffic_split"]


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
        await add_router(s, "zero", [("z", None)], strategy="least_in_flight")
        await _split(s, router, "one", 1, ["a"], 0)
        await _split(s, router, "two", 1, ["z"], 1, member_weight=0)
    c = await compile_(factory)
    assert [r.name for r in c.snapshot.routers] == ["zero"]
    assert any("zero total weight" in w for w in c.warnings)


async def test_all_capabilities_together(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        main = await add_router(s, "main", [("a", None)], strategy="vllm_load")
        await add_router(s, "fast", [("b", None)], strategy="lowest_latency")
        main.retry_enabled = True
        b = await _dep(s, "b")
        s.add(RouterFallback(router_id=main.id, model_id=b.id, position=0))
        await _split(s, main, "only", 1, ["a"], 0)
        up = await s.scalar(select(Upstream))
        up.health_check_interval_ms = 2000
        up.health_check_timeout_ms = 500
        up.circuit_breaker_enabled = True
        up.metrics_url = "http://vllm:8000/metrics"
        up.api_key_ciphertext = b"\x01" + b"0" * 40
        a = await _dep(s, "a")
        a.max_output_tokens = 100
        a.default_body = {"top_k": 20}
        a.reasoning_mode = "strip"
        a.chat_check_interval_ms = 30000
        a.chat_check_timeout_ms = 5000
    snap = (await compile_(factory)).snapshot
    assert list(snap.required_capabilities) == sorted(
        SUPPORTED_CAPABILITIES - {"strategy:weighted", "strategy:least_in_flight", "rotation_grace"}
    )


# --------------------------------------------------------------------------- validate mirror


def _base() -> pb.Snapshot:
    return pb.Snapshot(
        upstreams=[pb.Upstream(name="u", kind=pb.UPSTREAM_KIND_VLLM, base_url="http://u/v1")],
        models=[
            pb.Model(name="a", upstream="u", upstream_model="m"),
            pb.Model(name="b", upstream="u", upstream_model="m"),
        ],
        routers=[
            pb.Router(name="r", members=_m("a"), strategy="weighted"),
            pb.Router(name="s", members=_m("b"), strategy="weighted"),
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
    "chat check interval": lambda s: s.models[0].chat_check.CopyFrom(
        pb.ChatCheck(interval_ms=999, timeout_ms=100)
    ),
    "chat check timeout": lambda s: s.models[0].chat_check.CopyFrom(
        pb.ChatCheck(interval_ms=2000, timeout_ms=2000)
    ),
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
        pb.HealthCheck(url="http://u/x?y", interval_ms=1000, timeout_ms=10)
    ),
    "metrics url": lambda s: setattr(s.upstreams[0], "metrics_url", "ftp://u/metrics"),
    "retry attempts": lambda s: s.routers[0].retry.CopyFrom(pb.RetryPolicy(max_attempts=6)),
    "retry status": lambda s: s.routers[0].retry.CopyFrom(pb.RetryPolicy(retry_on_status=[600])),
    "self fallback": lambda s: s.routers[0].fallbacks.append("r"),
    "fallback is a router": lambda s: s.routers[0].fallbacks.append("s"),
    "fallback is an alias": lambda s: (
        s.aliases.append(pb.Alias(name="al", target="a")),
        s.routers[0].fallbacks.append("al"),
    ),
    "member is a router": lambda s: s.routers[0].members.append(pb.RouterMember(model="s")),
    "member is an alias": lambda s: (
        s.aliases.append(pb.Alias(name="al", target="a")),
        s.routers[0].members.append(pb.RouterMember(model="al", weight=1)),
    ),
    "member repeated": lambda s: s.routers[0].members.append(pb.RouterMember(model="a", weight=1)),
    "member params not an object": lambda s: setattr(s.routers[0].members[0], "params_json", "[]"),
    "alias to an alias": lambda s: s.aliases.extend(
        [pb.Alias(name="x", target="a"), pb.Alias(name="y", target="x")]
    ),
    "alias to nothing": lambda s: s.aliases.append(pb.Alias(name="x", target="nope")),
    "alias named like a model": lambda s: s.aliases.append(pb.Alias(name="a", target="r")),
    "alias named like a router": lambda s: s.aliases.append(pb.Alias(name="r", target="a")),
    "router named like a model": lambda s: setattr(s.routers[0], "name", "a"),
    "duplicate alias": lambda s: s.aliases.extend(
        [pb.Alias(name="x", target="a"), pb.Alias(name="x", target="b")]
    ),
    "built-in with params": lambda s: setattr(s.routers[0], "params_json", '{"x":1}'),
    "strategy name": lambda s: setattr(s.routers[0], "strategy", "Has Space"),
    "unknown fallback": lambda s: s.routers[0].fallbacks.append("nope"),
    "sticky without split": lambda s: setattr(s.routers[0], "sticky", pb.STICKY_BY_TEAM),
    "header without name": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, members=_m("a"))),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_HEADER),
    ),
    "header with other sticky": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, members=_m("a"))),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_KEY),
        setattr(s.routers[0], "sticky_header", "x-a"),
    ),
    "bad header name": lambda s: (
        _split_router(s, pb.TrafficSplit(name="x", weight=1, members=_m("a"))),
        setattr(s.routers[0], "sticky", pb.STICKY_BY_HEADER),
        setattr(s.routers[0], "sticky_header", "x a"),
    ),
    "split name charset": lambda s: _split_router(
        s, pb.TrafficSplit(name="no space", weight=1, members=_m("a"))
    ),
    "split name length": lambda s: _split_router(
        s, pb.TrafficSplit(name="x" * 33, weight=1, members=_m("a"))
    ),
    "split duplicate": lambda s: _split_router(
        s,
        pb.TrafficSplit(name="x", weight=1, members=_m("a")),
        pb.TrafficSplit(name="x", weight=1, members=_m("b")),
    ),
    "split empty": lambda s: _split_router(s, pb.TrafficSplit(name="x", weight=1)),
    "split unknown model": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=1, members=_m("zz"))
    ),
    "split repeats model": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=1, members=_m("a", "a"))
    ),
    "split weights zero": lambda s: _split_router(
        s, pb.TrafficSplit(name="x", weight=0, members=_m("a"))
    ),
    "too many splits": lambda s: _split_router(
        s, *(pb.TrafficSplit(name=f"s{i}", weight=1, members=_m("a")) for i in range(17))
    ),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_validate_rejects_what_the_gateway_rejects(mutate) -> None:
    snap = _base()
    mutate(snap)
    with pytest.raises(SnapshotInvalid):
        _check(snap)


def test_validate_accepts_full_routing_snapshot() -> None:
    snap = _base()
    snap.upstreams[0].health_check.CopyFrom(pb.HealthCheck(interval_ms=1000, timeout_ms=999))
    snap.upstreams[0].circuit_breaker.CopyFrom(pb.CircuitBreaker())
    snap.upstreams[0].metrics_url = "http://u/metrics"
    r = _split_router(
        snap,
        *(pb.TrafficSplit(name=f"s-{i}.x_", weight=i, members=_m("a", "b")) for i in range(16)),
    )
    r.sticky = pb.STICKY_BY_HEADER
    r.sticky_header = "x-session"
    r.retry.CopyFrom(pb.RetryPolicy(max_attempts=5, retry_on_status=[400, 599]))
    r.fallbacks.append("b")
    snap.routers[1].strategy = "vllm_load"
    snap.upstreams[0].api_key_ciphertext = b"\x01" + b"0" * 40
    snap.models[0].max_output_tokens = 100
    snap.models[0].default_body_json = '{"top_k":20}'
    snap.models[0].reasoning_effort.CopyFrom(
        pb.ReasoningEffort(
            mode=pb.REASONING_EFFORT_MODE_MAPPED,
            fragments_json={"none": '{"chat_template_kwargs":{"enable_thinking":false}}'},
            default_level="none",
        )
    )
    snap.models[0].chat_check.CopyFrom(pb.ChatCheck(interval_ms=1000, timeout_ms=999))
    _check(snap)
    assert list(snap.required_capabilities) == sorted(
        SUPPORTED_CAPABILITIES
        - {"strategy:least_in_flight", "strategy:lowest_latency", "rotation_grace"}
    )


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
        (lambda s: s.routers[0].fallbacks.append("b"), "fallbacks"),
        (
            lambda s: _split_router(s, pb.TrafficSplit(name="x", weight=1, members=_m("a"))),
            "traffic_split",
        ),
        (
            lambda s: setattr(s.routers[0], "strategy", "lowest_latency"),
            "strategy:lowest_latency",
        ),
        (lambda s: setattr(s.routers[0], "strategy", "vllm_load"), "strategy:vllm_load"),
    ],
)
def test_field_without_capability_is_invalid(mutate, cap) -> None:
    snap = _base()
    mutate(snap)
    assert cap in capabilities_used(snap)
    del snap.required_capabilities[:]
    snap.required_capabilities.extend(c for c in capabilities_used(snap) if c != cap)
    with pytest.raises(SnapshotInvalid, match="require capabilities"):
        _check(snap, caps=False)
    _check(snap)


def test_unknown_capability_is_invalid() -> None:
    snap = _base()
    snap.required_capabilities.extend(["jwt", "strategy:weighted"])
    with pytest.raises(SnapshotInvalid, match="unsupported"):
        _check(snap, caps=False)

"""Regressions for the R2-C3 gate findings and R2-C2 B1."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import (
    CompileError,
    compact_versions,
    compile_scope,
    publish_pending,
    rollback_routing,
    store_version,
)
from lumewarp_control_plane.compiler.compile import expiry_ms
from lumewarp_control_plane.compiler.validate import (
    SnapshotInvalid,
    upstream_url_problem,
    validate_snapshot,
)
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigOutbox,
    ConfigVersion,
    Deployment,
    GatewaySession,
    KeyLifecycle,
    RoutingStrategy,
    Upstream,
)
from lumewarp_control_plane.plugins import (
    MAX_WEIGHT,
    DeploymentView,
    LoadedPlugin,
    RouteView,
    WeightOverride,
    apply_router_plugins,
)
from tests.compiler.helpers import (
    OCT,
    SCOPE,
    add_hierarchy,
    add_key,
    add_route,
    load_version,
    seed_scope,
    touch,
)


class Fixed:
    api_version = 1

    def __init__(self, weight: int, deployment: str = "d") -> None:
        self.out = [WeightOverride(deployment, weight)]

    def adjust(self, routes):
        return self.out


def lp(plugin, name: str = "p") -> LoadedPlugin:
    return LoadedPlugin(name=name, plugin=plugin)


async def _outbox(factory) -> list[str]:
    async with factory() as s:
        return [
            r.value for r in await s.scalars(select(ConfigOutbox.status).order_by(ConfigOutbox.id))
        ]


async def _revoke(factory, key) -> None:
    async with factory() as s, s.begin():
        await s.execute(
            update(ApiKey)
            .where(ApiKey.id == key.id)
            .values(lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC))
        )


# --------------------------------------------------------------------------- 1 / B1


async def test_zero_weight_plugin_does_not_block_revocation(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s)
        key = await add_key(s, p)
        await add_route(s, "m", [("d", None)])
    await touch(factory)
    assert await publish_pending(factory, SCOPE, router_plugins=[]) == 1

    await _revoke(factory, key)
    await touch(factory)
    assert await publish_pending(factory, SCOPE, router_plugins=[lp(Fixed(0))]) == 2
    snap = await load_version(factory, 2)
    assert [d.weight for d in snap.deployments] == [1]  # plugin output discarded
    assert [r.model for r in snap.routes] == ["m"]
    assert list(snap.api_keys) == []  # the revocation went out
    assert await _outbox(factory) == ["done", "done"]


async def test_source_zero_weight_weighted_route_is_left_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s)
        key = await add_key(s, p)
        await add_route(s, "zero", [("z1", None), ("z2", None)])
        await add_route(s, "fine", [("ok", None)])
        await add_route(s, "lif", [("z1", None)], strategy=RoutingStrategy.LEAST_IN_FLIGHT)
        await s.execute(
            update(Deployment).where(Deployment.name.in_(["z1", "z2"])).values(weight=0)
        )
    await _revoke(factory, key)
    await touch(factory)
    assert await publish_pending(factory, SCOPE, router_plugins=[]) == 1
    snap = await load_version(factory, 1)
    # least_in_flight ignores weights, so only the weighted route is dropped.
    assert [r.model for r in snap.routes] == ["fine", "lif"]
    assert list(snap.api_keys) == []
    async with factory() as s:
        c = await compile_scope(s, SCOPE, now=OCT, router_plugins=[])
    assert any("zero total weight" in w for w in c.warnings)


async def test_plugin_cannot_zero_a_weighted_route_but_may_zero_one_member() -> None:
    routes = [
        RouteView(
            "m",
            "weighted",
            (
                DeploymentView("a", "u", "x", 1, None, None),
                DeploymentView("b", "u", "x", 1, None, None),
            ),
        )
    ]
    assert await apply_router_plugins(routes, [lp(Fixed(0, "a"))]) == {"a": 0}
    both = [lp(Fixed(0, "a"), "1"), lp(Fixed(0, "b"), "2")]
    assert await apply_router_plugins(routes, both) == {"a": 0}  # second would zero the route


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://vllm:8000/v1", True),
        ("https://api.openai.com/v1", True),
        ("ftp://x/v1", False),
        ("http:///v1", False),
        ("http://user:pw@host/v1", False),
        ("http://host/v1?x=1", False),
        ("http://host/v1#frag", False),
        ("http://host/v 1", False),
        ("http://hóst/v1", False),
        ("http://host:99999/v1", False),
        ("", False),
    ],
)
def test_upstream_url_rules(url: str, ok: bool) -> None:
    assert (upstream_url_problem(url) is None) is ok


async def test_invalid_upstream_url_left_out_with_its_routes(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_route(s, "bad", [("bad-d", None)], upstream="bad")
        await add_route(s, "mixed", [("good-d", None), ("bad-d", None)])
        await s.execute(
            update(Upstream).where(Upstream.name == "bad").values(base_url="http://h/v1?q")
        )
        await s.execute(
            update(Deployment).where(Deployment.name == "good-d").values(upstream_model="")
        )
        await add_route(s, "good", [("fine-d", None)])
    async with factory() as s:
        c = await compile_scope(s, SCOPE, now=OCT, router_plugins=[])
    snap = c.snapshot
    assert [u.name for u in snap.upstreams] == ["vllm"]
    assert [d.name for d in snap.deployments] == ["fine-d"]
    assert [r.model for r in snap.routes] == ["good"]
    assert len(c.warnings) >= 4


def _valid_snapshot() -> pb.Snapshot:
    return pb.Snapshot(
        scope="s",
        upstreams=[pb.Upstream(name="u", kind=pb.UPSTREAM_KIND_VLLM, base_url="http://u/v1")],
        deployments=[pb.Deployment(name="d", upstream="u", upstream_model="m", weight=1)],
        routes=[pb.ModelRoute(model="a", deployments=["d"], strategy=pb.ROUTING_STRATEGY_WEIGHTED)],
        teams=[pb.Team(id="t", org_id="o")],
        api_keys=[
            pb.ApiKey(
                key_id="k",
                key_hash=b"\x01" * 32,
                org_id="o",
                team_id="t",
                status=pb.KEY_STATUS_ACTIVE,
                policy=pb.EffectivePolicy(
                    rate_limit_scopes=[pb.RateLimitScope(id="team:t", limit=pb.RateLimit())]
                ),
            )
        ],
    )


def _mutations():
    def k(s):
        return s.api_keys[0]

    return {
        "capability": lambda s: s.required_capabilities.append("x"),
        "upstream kind": lambda s: setattr(s.upstreams[0], "kind", 0),
        "stream mode": lambda s: setattr(s.upstreams[0], "stream_usage_mode", 9),
        "base url": lambda s: setattr(s.upstreams[0], "base_url", "http://a@b/v1"),
        "dup upstream": lambda s: s.upstreams.append(s.upstreams[0]),
        "empty upstream": lambda s: setattr(s.upstreams[0], "name", ""),
        "unknown upstream": lambda s: setattr(s.deployments[0], "upstream", "x"),
        "empty model": lambda s: setattr(s.deployments[0], "upstream_model", ""),
        "dup deployment": lambda s: s.deployments.append(s.deployments[0]),
        "reserved alias": lambda s: setattr(s.routes[0], "model", "unknown"),
        "strategy 0": lambda s: setattr(s.routes[0], "strategy", 0),
        "lowest latency": lambda s: setattr(s.routes[0], "strategy", 3),
        "no deployments": lambda s: s.routes[0].ClearField("deployments"),
        "unknown deployment": lambda s: s.routes[0].deployments.append("x"),
        "zero weight": lambda s: setattr(s.deployments[0], "weight", 0),
        "dup route": lambda s: s.routes.append(s.routes[0]),
        "dup team": lambda s: s.teams.append(s.teams[0]),
        "empty team": lambda s: setattr(s.teams[0], "id", ""),
        "hash len": lambda s: setattr(k(s), "key_hash", b"\x01" * 31),
        "dup hash": lambda s: s.api_keys.append(
            pb.ApiKey(key_id="k2", key_hash=b"\x01" * 32, status=1, policy=pb.EffectivePolicy())
        ),  # fmt: skip
        "dup key": lambda s: s.api_keys.append(s.api_keys[0]),
        "empty key id": lambda s: setattr(k(s), "key_id", ""),
        "status 0": lambda s: setattr(k(s), "status", 0),
        "negative expiry": lambda s: setattr(k(s), "expires_at_unix_ms", -1),
        "unknown team": lambda s: setattr(k(s), "team_id", "x"),
        "org mismatch": lambda s: setattr(k(s), "org_id", "x"),
        "no policy": lambda s: k(s).ClearField("policy"),
        "empty allowed model": lambda s: k(s).policy.allowed_models.append(""),
        "scope without limit": lambda s: k(s).policy.rate_limit_scopes[0].ClearField("limit"),
        "repeated scope": lambda s: k(s).policy.rate_limit_scopes.append(
            k(s).policy.rate_limit_scopes[0]
        ),
        "conflicting scope": lambda s: s.api_keys.append(
            pb.ApiKey(
                key_id="k3",
                key_hash=b"\x03" * 32,
                status=1,
                policy=pb.EffectivePolicy(
                    rate_limit_scopes=[
                        pb.RateLimitScope(id="team:t", limit=pb.RateLimit(requests_per_minute=1))
                    ]
                ),
            )
        ),
    }


def test_validate_accepts_valid() -> None:
    validate_snapshot(_valid_snapshot(), max_bytes=1 << 20, max_keys=10)


@pytest.mark.parametrize("name", list(_mutations()))
def test_validate_mirrors_gateway(name: str) -> None:
    snap = _valid_snapshot()
    _mutations()[name](snap)
    with pytest.raises(SnapshotInvalid):
        validate_snapshot(snap, max_bytes=1 << 20, max_keys=10)


async def test_store_version_refuses_invalid_snapshot(factory) -> None:
    await seed_scope(factory)
    snap = _valid_snapshot()
    snap.scope = SCOPE
    snap.deployments[0].weight = 0
    async with factory() as s, s.begin():
        with pytest.raises(CompileError, match="zero total weight"):
            await store_version(s, SCOPE, snap, version=1, source_revision=1)


# --------------------------------------------------------------------------- 2


@pytest.mark.parametrize(
    ("expires_at", "wire"),
    [
        (None, 0),
        (datetime(1970, 1, 1, tzinfo=UTC), 1),
        (datetime(1969, 12, 31, tzinfo=UTC), 1),
        (datetime(1970, 1, 1, microsecond=500, tzinfo=UTC), 1),
        (datetime(1970, 1, 1, microsecond=1000, tzinfo=UTC), 1),
        (datetime(1970, 1, 1, microsecond=2999, tzinfo=UTC), 2),
        (datetime(2027, 1, 1, tzinfo=UTC) + timedelta(microseconds=999), 1798761600000),
    ],
)
def test_expiry_ms(expires_at, wire) -> None:
    assert expiry_ms(expires_at) == wire


async def test_epoch_expiry_is_not_never(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        _, _, p = await add_hierarchy(s)
        epoch = await add_key(s, p, name="epoch", expires_at=datetime(1970, 1, 1, tzinfo=UTC))
        old = await add_key(s, p, name="old", expires_at=datetime(1900, 1, 1, tzinfo=UTC))
    async with factory() as s:
        c = await compile_scope(s, SCOPE, now=OCT, router_plugins=[])
    wire = {k.key_id: k.expires_at_unix_ms for k in c.snapshot.api_keys}
    assert wire == {str(epoch.id): 1, str(old.id): 1}


# --------------------------------------------------------------------------- 3

ROUTES = [RouteView("m", "weighted", (DeploymentView("d", "u", "x", 1, None, None),))]


class SlowSequence:
    """A Sequence whose element access is slow; iteration happens via __getitem__."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def __len__(self) -> int:
        return 1

    def __getitem__(self, i: int):
        if i >= 1:
            raise IndexError
        time.sleep(self.delay)
        return WeightOverride("d", 9)


class Endless:
    """An iterator that never finishes and never yields."""

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(0.05)
        return self.__next__()


class EndlessValid:
    def __iter__(self):
        while True:
            yield WeightOverride("d", 2)


class ReturnsSlow:
    api_version = 1

    def __init__(self, result) -> None:
        self.result = result

    def adjust(self, routes):
        return self.result


class AsyncReturnsSlow(ReturnsSlow):
    async def adjust(self, routes):
        return self.result


async def _timed(plugins, timeout: float = 0.05) -> tuple[dict, float, int]:
    """Run plugins while counting event-loop ticks to prove the loop is not blocked."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    t = asyncio.create_task(ticker())
    start = time.monotonic()
    result = await apply_router_plugins(ROUTES, plugins, timeout_seconds=timeout)
    elapsed = time.monotonic() - start
    stop.set()
    await t
    return result, elapsed, ticks


@pytest.mark.parametrize("cls", [ReturnsSlow, AsyncReturnsSlow])
async def test_slow_result_iteration_is_under_the_deadline(cls) -> None:
    result, elapsed, ticks = await _timed([lp(cls(SlowSequence(0.3)))])
    assert result == {}
    assert elapsed < 0.2
    assert ticks >= 3  # the event loop kept running


@pytest.mark.parametrize("cls", [ReturnsSlow, AsyncReturnsSlow])
async def test_endless_iterators_are_isolated(cls) -> None:
    result, elapsed, _ = await _timed([lp(cls(Endless()), "a"), lp(cls(EndlessValid()), "b")])
    assert result == {} and elapsed < 0.5


async def test_subclassed_values_rejected() -> None:
    class Sneaky(str):
        pass

    plugin = ReturnsSlow([WeightOverride(Sneaky("d"), 2)])
    assert await apply_router_plugins(ROUTES, [lp(plugin)]) == {}
    assert await apply_router_plugins(ROUTES, [lp(ReturnsSlow([WeightOverride("d", True)]))]) == {}


# --------------------------------------------------------------------------- 4


async def test_max_plugin_weight_round_trips_through_rollback(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_route(s, "m", [("d", None)])
    await touch(factory)
    assert await publish_pending(factory, SCOPE, router_plugins=[lp(Fixed(MAX_WEIGHT))]) == 1
    assert (await load_version(factory, 1)).deployments[0].weight == MAX_WEIGHT
    async with factory() as s, s.begin():
        assert await rollback_routing(s, SCOPE, 1) == 2
    async with factory() as s:
        assert await s.scalar(select(Deployment.weight)) == MAX_WEIGHT

    # One past the column range is not accepted from plugins.
    assert await apply_router_plugins(ROUTES, [lp(Fixed(MAX_WEIGHT + 1))]) == {}


async def test_rollback_to_overweight_version_is_a_compile_error(factory) -> None:
    await seed_scope(factory)
    snap = _valid_snapshot()
    snap.scope = SCOPE
    snap.ClearField("api_keys")
    snap.ClearField("teams")
    snap.deployments[0].weight = 2**31  # valid uint32, as older versions could carry
    async with factory() as s, s.begin():
        await store_version(s, SCOPE, snap, version=1, source_revision=0)
    async with factory() as s, s.begin():
        with pytest.raises(CompileError, match="cannot be restored"):
            await rollback_routing(s, SCOPE, 1)


# --------------------------------------------------------------------------- 5


async def test_compaction_reads_pins_and_updates_in_one_statement(factory) -> None:
    """The old code read session pins into a Python set, then updated in a second
    statement, so a gateway that connected in between lost its base. Compaction is now one
    UPDATE with the pin check inside it: there is no window between reading and writing.
    """
    await seed_scope(factory)
    async with factory() as s, s.begin():
        for v in range(1, 5):
            await store_version(s, SCOPE, pb.Snapshot(scope=SCOPE), version=v, source_revision=v)
        s.add(GatewaySession(scope=SCOPE, gateway_id="a", connected=True, acked_version=3))
    async with factory() as s, s.begin():
        real_execute, statements = s.execute, []

        async def execute(statement, *args, **kwargs):
            statements.append(statement)
            return await real_execute(statement, *args, **kwargs)

        s.execute = execute
        assert await compact_versions(s, SCOPE, retain=1) == 2  # v1, v2
    assert len(statements) == 1
    async with factory() as s:
        kept = {
            v.version
            for v in await s.scalars(
                select(ConfigVersion).where(ConfigVersion.payload.is_not(None))
            )
        }
    assert kept == {3, 4}


async def test_compaction_sees_sessions_committed_before_the_statement(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        for v in range(1, 5):
            await store_version(s, SCOPE, pb.Snapshot(scope=SCOPE), version=v, source_revision=v)
    async with factory() as s, s.begin():
        await s.execute(select(1))  # transaction already running
        async with factory() as other, other.begin():
            other.add(GatewaySession(scope=SCOPE, gateway_id="b", connected=True, acked_version=1))
        assert await compact_versions(s, SCOPE, retain=1) == 2  # 2 and 3 go, 1 is pinned
    async with factory() as s:
        row = await s.get(ConfigVersion, (SCOPE, 1))
        assert row.payload is not None

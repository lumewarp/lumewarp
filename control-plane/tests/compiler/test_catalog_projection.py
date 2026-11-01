"""TODO(c2): remove with the projection. New tables onto the current snapshot shape."""

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import RouterSplit, RouterSplitMember
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, seed_scope


async def _compile(factory):
    async with factory() as session:
        return await compile_scope(session, SCOPE, now=OCT, require_local_membership=True)


def _pools(snapshot) -> dict[str, list[tuple[str, int]]]:
    weights = {m.name: m.weight for m in snapshot.models}
    return {r.name: [(m, weights[m]) for m in r.models] for r in snapshot.routers}


async def test_each_model_is_also_a_single_member_router(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_model(s, "m")
    snap = (await _compile(factory)).snapshot
    assert [(m.name, m.weight) for m in snap.models] == [("m", 1)]
    router = snap.routers[0]
    assert (router.name, list(router.models)) == ("m", ["m"])
    assert router.strategy == pb.ROUTING_STRATEGY_WEIGHTED
    assert not router.HasField("retry") and not router.fallbacks


async def test_equal_weights_need_no_synthetic_model(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        b = await factories.make_model(s, "b")
        await factories.make_router(s, "r", [(a, 5), (b, 5)])
        await factories.make_router(s, "other", [(a, 5)])
    snap = (await _compile(factory)).snapshot
    assert sorted(m.name for m in snap.models) == ["a", "b"]
    assert {m.name: m.weight for m in snap.models} == {"a": 5, "b": 5}
    assert _pools(snap)["r"] == [("a", 5), ("b", 5)]
    # A model's own router is the only member there, so the shared weight has no effect.
    assert list(next(r for r in snap.routers if r.name == "a").models) == ["a"]


async def test_differing_weights_split_into_synthetic_models(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a", upstream_model="real-a")
        b = await factories.make_model(s, "b")
        await factories.make_router(s, "heavy", [(a, 9), (b, 1)])
        await factories.make_router(s, "light", [(a, 1), (b, 1)])
    snap = (await _compile(factory)).snapshot
    pools = _pools(snap)
    # Every pool keeps its own ratio, whichever weight became the canonical one.
    assert [w for _, w in pools["heavy"]] == [9, 1]
    assert [w for _, w in pools["light"]] == [1, 1]
    synthetic = [m for m in snap.models if m.name.startswith("a [weight ")]
    assert len(synthetic) == 1
    assert synthetic[0].upstream_model == "real-a"
    # The model's own router stays on the canonical name.
    assert pools["a"][0][0] == "a"


async def test_zero_weight_in_a_non_weighted_pool_needs_no_synthetic_model(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_router(s, "w", [(a, 3)])
        await factories.make_router(s, "lif", [(a, 0)], strategy="least_in_flight")
    snap = (await _compile(factory)).snapshot
    assert [m.name for m in snap.models] == ["a"]
    assert list(next(r for r in snap.routers if r.name == "lif").models) == ["a"]


async def test_split_members_carry_their_own_weights(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        b = await factories.make_model(s, "b")
        await factories.make_router(s, "plain", [(a, 1)])
        router = await factories.make_router(s, "split")
        split = RouterSplit(router_id=router.id, name="one", weight=1, position=0)
        s.add(split)
        await s.flush()
        s.add(RouterSplitMember(split_id=split.id, model_id=a.id, weight=4, position=0))
        s.add(RouterSplitMember(split_id=split.id, model_id=b.id, weight=1, position=1))
    snap = (await _compile(factory)).snapshot
    weights = {m.name: m.weight for m in snap.models}
    wire = next(r for r in snap.routers if r.name == "split").traffic_split[0]
    assert [weights[m] for m in wire.models] == [4, 1]


async def test_alias_copies_its_target(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        b = await factories.make_model(s, "b")
        fallback = await factories.make_model(s, "backup")
        router = await factories.make_router(
            s,
            "r",
            [a, b],
            fallbacks=[fallback],
            strategy="lowest_latency",
            retry_enabled=True,
            retry_max_attempts=2,
        )
        await factories.make_alias(s, "to-router", router)
        await factories.make_alias(s, "to-model", a)
    snap = (await _compile(factory)).snapshot
    routers = {r.name: r for r in snap.routers}
    copied = pb.Router()
    copied.CopyFrom(routers["to-router"])
    copied.name = "r"
    assert copied == routers["r"]
    assert list(routers["to-model"].models) == ["a"]
    assert list(routers["r"].fallbacks) == ["backup"]
    assert list(routers["backup"].models) == ["backup"]


async def test_grants_expand_to_current_names(factory) -> None:
    from tests.compiler.helpers import add_key

    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_alias(s, "al", a)
        team = await factories.make_team(s, model_access="list", allowed_models=["a", "al"])
        owner = await factories.make_service_account(s, team)
        await add_key(s, team, owner)
    snap = (await _compile(factory)).snapshot
    assert list(snap.api_keys[0].policy.allowed_models) == ["a", "al"]

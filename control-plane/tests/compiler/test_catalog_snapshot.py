"""The three kinds on the wire: models, aliases and routers, each under its own name."""

import json

from sqlalchemy import select

from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.compiler.delta import diff
from lumewarp_control_plane.models import (
    CatalogName,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
)
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, add_key, seed_scope


async def _compile(factory):
    async with factory() as session:
        return await compile_scope(session, SCOPE, now=OCT, require_local_membership=True)


def _members(router) -> list[tuple[str, int]]:
    return [(m.model, m.weight) for m in router.members]


async def test_a_model_is_on_the_wire_without_a_router(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_model(s, "m", upstream_model="real")
    snap = (await _compile(factory)).snapshot
    assert [(m.name, m.upstream_model) for m in snap.models] == [("m", "real")]
    assert list(snap.routers) == []
    assert list(snap.aliases) == []
    assert list(snap.required_capabilities) == []


async def test_the_same_model_keeps_its_weight_per_router(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a", upstream_model="real-a")
        b = await factories.make_model(s, "b")
        await factories.make_router(s, "heavy", [(a, 9), (b, 1)])
        await factories.make_router(s, "light", [(a, 1), (b, 1)])
    snap = (await _compile(factory)).snapshot
    routers = {r.name: r for r in snap.routers}
    assert _members(routers["heavy"]) == [("a", 9), ("b", 1)]
    assert _members(routers["light"]) == [("a", 1), ("b", 1)]
    # One entry per model, however many routers hold it.
    assert [m.name for m in snap.models] == ["a", "b"]


async def test_zero_weight_outside_weighted_stays_a_member(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_router(s, "lif", [(a, 0)], strategy="least_in_flight")
    snap = (await _compile(factory)).snapshot
    assert _members(snap.routers[0]) == [("a", 0)]
    assert list(snap.required_capabilities) == ["strategy:least_in_flight"]


async def test_member_and_router_params_are_json_objects(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_router(
            s, "r", [(a, 2)], strategy="header_pin", params={"headers": ["x-pin"]}
        )
        member = await s.scalar(select(RouterMember))
        member.params = {"pin": "a"}
    snap = (await _compile(factory)).snapshot
    assert json.loads(snap.routers[0].params_json) == {"headers": ["x-pin"]}
    assert json.loads(snap.routers[0].members[0].params_json) == {"pin": "a"}
    assert list(snap.required_capabilities) == ["strategy:header_pin"]


async def test_split_members_carry_their_own_weights(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        b = await factories.make_model(s, "b")
        router = await factories.make_router(s, "split")
        split = RouterSplit(router_id=router.id, name="one", weight=1, position=0)
        s.add(split)
        await s.flush()
        s.add(RouterSplitMember(split_id=split.id, model_id=a.id, weight=4, position=0))
        s.add(RouterSplitMember(split_id=split.id, model_id=b.id, weight=1, position=1))
    snap = (await _compile(factory)).snapshot
    wire = snap.routers[0].traffic_split[0]
    assert [(m.model, m.weight) for m in wire.members] == [("a", 4), ("b", 1)]
    assert list(snap.routers[0].members) == []
    assert list(snap.required_capabilities) == ["strategy:weighted", "traffic_split"]


async def test_alias_points_at_its_target_and_copies_nothing(factory) -> None:
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
    assert {(al.name, al.target) for al in snap.aliases} == {
        ("to-router", "r"),
        ("to-model", "a"),
    }
    assert [al.name for al in snap.aliases] == sorted(al.name for al in snap.aliases)
    assert [r.name for r in snap.routers] == ["r"]
    assert list(snap.routers[0].fallbacks) == ["backup"]
    assert list(snap.required_capabilities) == [
        "fallbacks",
        "retry",
        "strategy:lowest_latency",
    ]


async def test_grants_expand_to_current_names(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_alias(s, "al", a)
        team = await factories.make_team(s, model_access="list", allowed_models=["a", "al"])
        owner = await factories.make_service_account(s, team)
        await add_key(s, team, owner)
    snap = (await _compile(factory)).snapshot
    assert list(snap.api_keys[0].policy.allowed_models) == ["a", "al"]


async def _rename(factory, old: str, new: str) -> None:
    async with factory() as s, s.begin():
        row = await s.scalar(select(CatalogName).where(CatalogName.name == old))
        row.name = new


async def test_renaming_a_model_reaches_routers_aliases_and_keys_in_one_delta(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        b = await factories.make_model(s, "b")
        await factories.make_router(s, "r", [a], fallbacks=[b])
        await factories.make_alias(s, "al", a)
        team = await factories.make_team(s, model_access="list", allowed_models=["a"])
        owner = await factories.make_service_account(s, team)
        await add_key(s, team, owner)
    before = (await _compile(factory)).snapshot
    await _rename(factory, "a", "renamed")
    after = (await _compile(factory)).snapshot
    delta = diff(before, after)
    assert {m.name for m in delta.upsert_models} == {"renamed"}
    assert list(delta.remove_models) == ["a"]
    assert [m.model for m in delta.upsert_routers[0].members] == ["renamed"]
    assert [(al.name, al.target) for al in delta.upsert_aliases] == [("al", "renamed")]
    assert list(delta.remove_aliases) == []
    assert [list(k.policy.allowed_models) for k in delta.upsert_api_keys] == [["renamed"]]


async def test_renaming_an_alias_upserts_and_removes_it_and_touches_its_keys(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        await factories.make_alias(s, "al", a)
        team = await factories.make_team(s, model_access="list", allowed_models=["al"])
        owner = await factories.make_service_account(s, team)
        await add_key(s, team, owner)
    before = (await _compile(factory)).snapshot
    await _rename(factory, "al", "nick")
    after = (await _compile(factory)).snapshot
    delta = diff(before, after)
    assert [(al.name, al.target) for al in delta.upsert_aliases] == [("nick", "a")]
    assert list(delta.remove_aliases) == ["al"]
    assert [list(k.policy.allowed_models) for k in delta.upsert_api_keys] == [["nick"]]
    assert list(delta.upsert_models) == [] and list(delta.upsert_routers) == []


async def test_renaming_a_router_changes_its_aliases_and_nothing_else(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        a = await factories.make_model(s, "a")
        router = await factories.make_router(s, "r", [a])
        await factories.make_alias(s, "al", router)
    before = (await _compile(factory)).snapshot
    await _rename(factory, "r", "r2")
    after = (await _compile(factory)).snapshot
    delta = diff(before, after)
    assert [r.name for r in delta.upsert_routers] == ["r2"]
    assert list(delta.remove_routers) == ["r"]
    assert [(al.name, al.target) for al in delta.upsert_aliases] == [("al", "r2")]
    assert list(delta.upsert_models) == []

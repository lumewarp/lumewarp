import uuid
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
    member_scope_id,
)
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, add_key, add_route, seed_scope

ALL, INHERIT, LIST = ModelAccess.ALL, ModelAccess.INHERIT, ModelAccess.LIST


def lvl(mode: ModelAccess, *models: str) -> dict:
    return {"model_access": mode, "allowed_models": list(models)}


async def compile_(factory, **kw):
    async with factory() as s:
        kw.setdefault("require_local_membership", True)
        return await compile_scope(s, SCOPE, now=kw.pop("now", OCT), **kw)


def key_of(compiled, key) -> pb.ApiKey:
    return next(k for k in compiled.snapshot.api_keys if k.key_id == str(key.id))


# --------------------------------------------------------------------------- allowlists

# Each case: (team policy, owner policy, allow_all_models, allowed_models).


@pytest.mark.parametrize("owner_kind", ["member", "service_account"])
@pytest.mark.parametrize(
    ("team_policy", "owner_policy", "allow_all", "models"),
    [
        # Defaults: team ALL, owner INHERIT -> universal.
        (lvl(ALL), {}, True, []),
        (lvl(ALL), lvl(INHERIT), True, []),
        (lvl(ALL), lvl(ALL), True, []),
        (lvl(LIST, "a", "b"), {}, False, ["a", "b"]),
        (lvl(LIST, "a", "b"), lvl(LIST, "b", "c"), False, ["b"]),
        # ALL at the owner does not widen the team's list.
        (lvl(LIST, "a"), lvl(ALL), False, ["a"]),
        # The owner narrows a universal team.
        (lvl(ALL), lvl(LIST, "x", "y"), False, ["x", "y"]),
        # Disjoint lists intersect to deny-all, never to "all".
        (lvl(LIST, "a"), lvl(LIST, "b"), False, []),
        # Explicit empty list is deny-all at either level.
        (lvl(ALL), lvl(LIST), False, []),
        (lvl(LIST), lvl(ALL), False, []),
        # Empty names are dropped; order and duplicates normalized.
        (lvl(LIST, "z", "a", "", "a"), {}, False, ["a", "z"]),
    ],
)
async def test_two_level_allowlist_narrowing(
    factory, owner_kind, team_policy, owner_policy, allow_all, models
) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s, **team_policy)
        if owner_kind == "member":
            user = await factories.make_user(s)
            await factories.make_member(s, team, user, **owner_policy)
            owner = user
        else:
            owner = await factories.make_service_account(s, team, **owner_policy)
        key = await add_key(s, team, owner)
    k = key_of(await compile_(factory), key)
    assert (k.policy.allow_all_models, list(k.policy.allowed_models)) == (allow_all, models)


def test_narrow_unit() -> None:
    assert narrow(None, INHERIT, []) is None
    assert narrow(frozenset({"a"}), ALL, []) == {"a"}
    assert narrow(None, LIST, []) == frozenset()


# --------------------------------------------------------------------------- limits


async def test_rate_limit_scopes_for_user_and_service_account(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s, rpm_limit=600, tpm_limit=10_000, max_parallel=4)
        alice = await factories.make_user(s)
        await factories.make_member(s, team, alice, rpm_limit=60)
        bob = await factories.make_user(s)
        await factories.make_member(s, team, bob)
        sa = await factories.make_service_account(s, team, max_parallel=2)
        sa_plain = await factories.make_service_account(s, team)
        k_alice = await add_key(s, team, alice, name="alice")
        k_alice2 = await add_key(s, team, alice, name="alice2")
        k_bob = await add_key(s, team, bob, name="bob")
        k_sa = await add_key(s, team, sa, name="sa")
        k_sa_plain = await add_key(s, team, sa_plain, name="sa-plain")
    c = await compile_(factory)

    def scopes(key):
        return [(r.id, r.limit) for r in key_of(c, key).policy.rate_limit_scopes]

    team_scope = (
        f"team:{team.id}",
        pb.RateLimit(requests_per_minute=600, tokens_per_minute=10_000, max_parallel_requests=4),
    )
    alice_scope = (
        f"member:{member_scope_id(team.id, alice.id)}",
        pb.RateLimit(requests_per_minute=60),
    )
    assert scopes(k_alice) == [team_scope, alice_scope]
    # Keys of one owner share the owner's limiter.
    assert scopes(k_alice2) == scopes(k_alice)
    # No owner limit -> only the team scope; the scope id is never keyed by key.
    assert scopes(k_bob) == [team_scope]
    assert scopes(k_sa) == [team_scope, (f"sa:{sa.id}", pb.RateLimit(max_parallel_requests=2))]
    assert scopes(k_sa_plain) == [team_scope]
    assert "key:" not in str([scopes(k) for k in (k_alice, k_bob, k_sa)])


async def test_member_scope_id_is_stable_uuid5(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user, rpm_limit=5)
        key = await add_key(s, team, user)
    scope_id = next(r.id for r in key_of(await compile_(factory), key).policy.rate_limit_scopes)
    assert scope_id == f"member:{member_scope_id(team.id, user.id)}"
    assert member_scope_id(team.id, user.id) == uuid.uuid5(team.id, str(user.id))
    assert member_scope_id(team.id, user.id).version == 5
    assert member_scope_id(team.id, user.id) != user.id


# --------------------------------------------------------------------------- status


async def _spend(factory, level: ScopeType, entity_id, cost: int, window: datetime) -> None:
    async with factory() as s, s.begin():
        s.add(UsageTotal(scope_type=level, scope_id=entity_id, window_start=window,
                         cost_nano_usd=cost))  # fmt: skip


async def test_budget_status_and_rollover(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s, name="a", budget_nano_usd=1000)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        key = await add_key(s, team, user)
        team2 = await factories.make_team(s, name="b", budget_nano_usd=1000)
        user2 = await factories.make_user(s)
        await factories.make_member(s, team2, user2)
        other = await add_key(s, team2, user2)
    oct1, nov1 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    await _spend(factory, ScopeType.TEAM, team.id, 1000, oct1)  # exactly at budget
    await _spend(factory, ScopeType.TEAM, team2.id, 999, oct1)

    c = await compile_(factory, now=nov1 - timedelta(milliseconds=1))
    k = key_of(c, key)
    assert k.status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert k.policy.budget_window_end_unix_ms == int(nov1.timestamp() * 1000)
    assert key_of(c, other).status == pb.KEY_STATUS_ACTIVE

    # New month: last month's spend no longer blocks.
    c = await compile_(factory, now=nov1)
    assert key_of(c, key).status == pb.KEY_STATUS_ACTIVE
    assert key_of(c, key).policy.budget_window_end_unix_ms == 0


async def test_team_budget_blocks_every_key_of_the_team(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s, budget_nano_usd=5)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        sa = await factories.make_service_account(s, team)
        k_user = await add_key(s, team, user, name="u")
        k_sa = await add_key(s, team, sa, name="sa")
    await _spend(factory, ScopeType.TEAM, team.id, 6, datetime(2026, 10, 1, tzinfo=UTC))
    c = await compile_(factory)
    assert key_of(c, k_user).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, k_sa).status == pb.KEY_STATUS_BUDGET_EXCEEDED


async def test_owner_budget_blocks_only_that_owner(factory) -> None:
    await seed_scope(factory)
    oct1 = datetime(2026, 10, 1, tzinfo=UTC)
    async with factory() as s, s.begin():
        team = await factories.make_team(s)
        alice = await factories.make_user(s)
        await factories.make_member(s, team, alice, budget_nano_usd=5)
        bob = await factories.make_user(s)
        await factories.make_member(s, team, bob, budget_nano_usd=5)
        sa = await factories.make_service_account(s, team, budget_nano_usd=5)
        sa_other = await factories.make_service_account(s, team, budget_nano_usd=5)
        k_alice = await add_key(s, team, alice, name="alice")
        k_alice2 = await add_key(s, team, alice, name="alice2")
        k_bob = await add_key(s, team, bob, name="bob")
        k_sa = await add_key(s, team, sa, name="sa")
        k_sa_other = await add_key(s, team, sa_other, name="sa-other")
    await _spend(factory, ScopeType.MEMBER, member_scope_id(team.id, alice.id), 6, oct1)
    await _spend(factory, ScopeType.SERVICE_ACCOUNT, sa.id, 6, oct1)
    # Same ids at the wrong level must not count.
    await _spend(factory, ScopeType.SERVICE_ACCOUNT, bob.id, 99, oct1)
    c = await compile_(factory)
    blocked = pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, k_alice).status == blocked
    assert key_of(c, k_alice2).status == blocked
    assert key_of(c, k_sa).status == blocked
    assert key_of(c, k_bob).status == pb.KEY_STATUS_ACTIVE
    assert key_of(c, k_sa_other).status == pb.KEY_STATUS_ACTIVE


async def test_member_spend_in_another_team_does_not_block(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t1 = await factories.make_team(s, name="t1")
        t2 = await factories.make_team(s, name="t2")
        user = await factories.make_user(s)
        await factories.make_member(s, t1, user, budget_nano_usd=5)
        await factories.make_member(s, t2, user, budget_nano_usd=5)
        k1 = await add_key(s, t1, user, name="k1")
        k2 = await add_key(s, t2, user, name="k2")
    await _spend(
        factory,
        ScopeType.MEMBER,
        member_scope_id(t1.id, user.id),
        6,
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    c = await compile_(factory)
    assert key_of(c, k1).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, k2).status == pb.KEY_STATUS_ACTIVE


async def test_lifecycle_beats_budget(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team, user = await _team_with_member(s, team={"budget_nano_usd": 0})
        suspended = await add_key(s, team, user, name="s", lifecycle=KeyLifecycle.SUSPENDED)
        revoked = await add_key(
            s, team, user, name="r", lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC)
        )
        active = await add_key(s, team, user, name="a")
    c = await compile_(factory)
    assert key_of(c, suspended).status == pb.KEY_STATUS_SUSPENDED
    assert key_of(c, active).status == pb.KEY_STATUS_BUDGET_EXCEEDED  # budget 0 = no spend
    assert str(revoked.id) not in {k.key_id for k in c.snapshot.api_keys}


async def _team_with_member(s, *, team: dict | None = None, member: dict | None = None):
    t = await factories.make_team(s, **(team or {}))
    user = await factories.make_user(s)
    await factories.make_member(s, t, user, **(member or {}))
    return t, user


@pytest.mark.parametrize("reason", ["not_a_member", "inactive"])
async def test_user_key_suspended_when_owner_not_usable(factory, reason) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s)
        member = await factories.make_user(s)
        await factories.make_member(s, team, member)
        owner = await factories.make_user(s, is_active=reason != "inactive")
        if reason == "inactive":
            await factories.make_member(s, team, owner)
        # sa keys are unaffected by user state.
        sa = await factories.make_service_account(s, team)
        gone = await add_key(s, team, owner, name="gone")
        fine = await add_key(s, team, member, name="fine")
        sa_key = await add_key(s, team, sa, name="sa")
    c = await compile_(factory)
    assert key_of(c, gone).status == pb.KEY_STATUS_SUSPENDED
    assert key_of(c, fine).status == pb.KEY_STATUS_ACTIVE
    assert key_of(c, sa_key).status == pb.KEY_STATUS_ACTIVE
    # The suspended key is still published (with the team-only policy), not dropped.
    assert str(gone.id) in {k.key_id for k in c.snapshot.api_keys}


async def test_suspended_key_follows_membership_changes(factory) -> None:
    from sqlalchemy import delete

    from lumewarp_control_plane.models import TeamMember

    await seed_scope(factory)
    async with factory() as s, s.begin():
        team, user = await _team_with_member(s)
        key = await add_key(s, team, user)
    assert key_of(await compile_(factory), key).status == pb.KEY_STATUS_ACTIVE
    async with factory() as s, s.begin():
        await s.execute(delete(TeamMember).where(TeamMember.user_id == user.id))
    assert key_of(await compile_(factory), key).status == pb.KEY_STATUS_SUSPENDED


@pytest.mark.parametrize("require_local_membership", [True, False])
@pytest.mark.parametrize("has_local_policy", [True, False])
@pytest.mark.parametrize("active", [True, False])
async def test_owner_eligibility_and_policy_follow_backend(
    factory, require_local_membership, has_local_policy, active
) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(
            s, model_access=LIST, allowed_models=["a", "b"], rpm_limit=60
        )
        user = await factories.make_user(s, is_active=active)
        if has_local_policy:
            await factories.make_member(
                s, team, user, model_access=LIST, allowed_models=["a"], rpm_limit=10
            )
        key = await add_key(s, team, user)
    wire = key_of(await compile_(factory, require_local_membership=require_local_membership), key)
    eligible = active and (has_local_policy or not require_local_membership)
    assert wire.status == (pb.KEY_STATUS_ACTIVE if eligible else pb.KEY_STATUS_SUSPENDED)
    assert list(wire.policy.allowed_models) == (["a"] if has_local_policy else ["a", "b"])
    assert wire.policy.rate_limit_scopes[0].id == f"team:{team.id}"
    assert len(wire.policy.rate_limit_scopes) == (2 if has_local_policy else 1)


async def test_key_fields(factory) -> None:
    await seed_scope(factory)
    expires = datetime(2027, 1, 1, tzinfo=UTC)
    async with factory() as s, s.begin():
        t, u = await _team_with_member(s)
        key = await add_key(s, t, u, expires_at=expires)
    c = await compile_(factory)
    k = key_of(c, key)
    assert k.key_hash == key.key_hash and len(k.key_hash) == 32
    assert k.team_id == str(t.id)
    assert k.expires_at_unix_ms == int(expires.timestamp() * 1000)
    assert list(c.snapshot.teams) == [pb.Team(id=str(t.id), name=t.name)]
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
    assert [r.name for r in snap.routes] == ["fast", "llama"]
    assert snap.routes[0].strategy == pb.ROUTING_STRATEGY_LOWEST_LATENCY
    assert list(snap.routes[1].deployments) == ["llama-b", "llama-a"]  # position order kept
    assert snap.routes[1].strategy == pb.ROUTING_STRATEGY_WEIGHTED
    assert len(c.warnings) == 2
    deps = {d.name: d for d in snap.deployments}
    assert not deps["llama-a"].HasField("price")  # unknown, not free
    assert deps["llama-b"].price == pb.Price(input_nano_usd_per_mtok=5, output_nano_usd_per_mtok=5)
    assert [(u.name, u.kind) for u in snap.upstreams] == [("vllm", pb.UPSTREAM_KIND_VLLM)]
    assert snap.upstreams[0].stream_usage_mode == pb.STREAM_USAGE_MODE_INCLUDE_USAGE
    assert list(snap.required_capabilities) == ["lowest_latency"]


async def test_deterministic(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await _team_with_member(s, team=lvl(LIST, "b", "a"))
        for i in range(5):
            await add_key(s, t, u, name=f"k{i}")
        await add_route(s, "m", [("d2", 1), ("d1", 2)])
    a = (await compile_(factory)).snapshot.SerializeToString(deterministic=True)
    b = (await compile_(factory)).snapshot.SerializeToString(deterministic=True)
    assert a == b


async def test_limits(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await _team_with_member(s)
        await add_key(s, t, u, name="k1")
        await add_key(s, t, u, name="k2")
    with pytest.raises(CompileError, match="keys exceed"):
        await compile_(factory, max_snapshot_keys=1)
    with pytest.raises(CompileError, match="bytes"):
        await compile_(factory, max_snapshot_bytes=100)
    assert (await compile_(factory, max_snapshot_keys=2)).key_count == 2


async def test_unknown_scope(factory) -> None:
    with pytest.raises(CompileError, match="unknown config scope"):
        await compile_(factory)

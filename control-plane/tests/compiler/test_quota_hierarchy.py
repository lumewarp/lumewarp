"""The four scope levels, the derived org budget and the per-key scope cap."""

import uuid
from datetime import UTC, datetime

import pytest

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.compiler.validate import (
    MAX_RATE_LIMIT_SCOPES,
    SnapshotInvalid,
    validate_snapshot,
)
from lumewarp_control_plane.models import (
    MemberPolicy,
    ModelAccess,
    ScopeType,
    UsageTotal,
    member_scope_id,
)
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, add_key, seed_scope

ALL, INHERIT, LIST = ModelAccess.ALL, ModelAccess.INHERIT, ModelAccess.LIST


async def compile_(factory, **kw):
    async with factory() as s:
        kw.setdefault("require_local_membership", True)
        return await compile_scope(s, SCOPE, now=kw.pop("now", OCT), **kw)


def key_of(compiled, key) -> pb.ApiKey:
    return next(k for k in compiled.snapshot.api_keys if k.key_id == str(key.id))


def scopes(policy: pb.EffectivePolicy) -> list[tuple[str, pb.RateLimit]]:
    return [(scope.id, scope.limit) for scope in policy.rate_limit_scopes]


# --------------------------------------------------------------------------- levels


async def test_four_levels_org_team_owner_key(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        for name in ("a", "b", "c", "d"):
            await factories.catalog_name(s, name)
        org = await factories.make_org(
            s,
            model_access=LIST,
            rpm_limit=1000,
            tpm_limit=1_000_000,
            max_parallel=100,
        )
        await factories.grant(s, org, ["a", "b", "c"])
        team = await factories.make_team(
            s,
            org=org,
            model_access=LIST,
            allowed_models=["b", "c"],
            rpm_limit=100,
            tpm_limit=50_000,
            max_parallel=20,
        )
        user = await factories.make_user(s)
        await factories.make_member(
            s,
            team,
            user,
            model_access=LIST,
            allowed_models=["c", "d"],
            rpm_limit=50,
        )
        key = await add_key(s, team, user, rpm_limit=10, tpm_limit=1_000, max_parallel=2)

    wire = key_of(await compile_(factory), key)
    assert scopes(wire.policy) == [
        (
            f"org:{org.id}",
            pb.RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=1_000_000,
                max_parallel_requests=100,
            ),
        ),
        (
            f"team:{team.id}",
            pb.RateLimit(
                requests_per_minute=100,
                tokens_per_minute=50_000,
                max_parallel_requests=20,
            ),
        ),
        (
            f"member:{member_scope_id(team.id, user.id)}",
            pb.RateLimit(requests_per_minute=50),
        ),
        (
            f"key:{key.id}",
            pb.RateLimit(
                requests_per_minute=10,
                tokens_per_minute=1_000,
                max_parallel_requests=2,
            ),
        ),
    ]
    # Models are the intersection of all four levels.
    assert not wire.policy.allow_all_models
    assert list(wire.policy.allowed_models) == ["c"]


async def test_member_defaults_apply_without_a_membership_row(factory) -> None:
    """External authz: there is no `team_members` row, but quotas still apply."""
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s)
        team = await factories.make_team(
            s, org=org, rpm_limit=100, member_rpm_default=7, member_max_parallel_default=3
        )
        user = await factories.make_user(s)
        # No make_member and no make_member policy: only team defaults exist.
        key = await add_key(s, team, user)
    wire = key_of(await compile_(factory, require_local_membership=False), key)
    assert wire.status == pb.KEY_STATUS_ACTIVE
    assert scopes(wire.policy) == [
        (f"team:{team.id}", pb.RateLimit(requests_per_minute=100)),
        (
            f"member:{member_scope_id(team.id, user.id)}",
            pb.RateLimit(requests_per_minute=7, max_parallel_requests=3),
        ),
    ]


async def test_member_policy_applies_without_a_membership_row(factory) -> None:
    """The member exception is decoupled from `team_members` (external authz).

    The compiler used to skip the owner level when there was no membership row; the
    model list and limits must still apply.
    """
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.catalog_name(s, "a")
        await factories.catalog_name(s, "b")
        team = await factories.make_team(
            s, model_access=LIST, allowed_models=["a", "b"], rpm_limit=100
        )
        user = await factories.make_user(s)
        s.add(
            MemberPolicy(
                team_id=team.id,
                user_id=user.id,
                model_access=LIST,
                rpm_limit=5,
            )
        )
        await s.flush()
        await factories.grant(s, await s.get(MemberPolicy, (team.id, user.id)), ["a"])
        key = await add_key(s, team, user)
    wire = key_of(await compile_(factory, require_local_membership=False), key)
    assert (wire.status, list(wire.policy.allowed_models)) == (pb.KEY_STATUS_ACTIVE, ["a"])
    assert scopes(wire.policy) == [
        (f"team:{team.id}", pb.RateLimit(requests_per_minute=100)),
        (f"member:{member_scope_id(team.id, user.id)}", pb.RateLimit(requests_per_minute=5)),
    ]


async def test_member_defaults_are_clamped_by_the_team(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(
            s, rpm_limit=30, member_rpm_default=100, member_tpm_default=500
        )
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        key = await add_key(s, team, user)
    wire = key_of(await compile_(factory), key)
    member = next(r for r in wire.policy.rate_limit_scopes if r.id.startswith("member:"))
    assert member.limit.requests_per_minute == 30
    assert member.limit.tokens_per_minute == 500


async def test_service_account_takes_no_member_default(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s, rpm_limit=100, member_rpm_default=7)
        account = await factories.make_service_account(s, team)
        key = await add_key(s, team, account)
    wire = key_of(await compile_(factory), key)
    assert [scope.id for scope in wire.policy.rate_limit_scopes] == [f"team:{team.id}"]


async def test_key_without_limits_adds_no_scope(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        team = await factories.make_team(s)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        key = await add_key(s, team, user)
    assert scopes(key_of(await compile_(factory), key).policy) == []


# --------------------------------------------------------------------------- budgets


async def _spend(factory, level, entity_id, cost: int, window: datetime = OCT) -> None:
    async with factory() as s, s.begin():
        s.add(
            UsageTotal(
                scope_type=level,
                scope_id=entity_id,
                window_start=datetime(window.year, window.month, 1, tzinfo=UTC),
                cost_nano_usd=cost,
            )
        )


async def test_org_budget_sums_its_teams_and_blocks_every_key(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s, budget_nano_usd=100)
        other_org = await factories.make_org(
            s, f"other-{uuid.uuid4().hex[:8]}", budget_nano_usd=100
        )
        t1 = await factories.make_team(s, org=org)
        t2 = await factories.make_team(s, org=org)
        foreign = await factories.make_team(s, org=other_org)
        user = await factories.make_user(s)
        for team in (t1, t2, foreign):
            await factories.make_member(s, team, user)
        k1 = await add_key(s, t1, user, name="k1")
        k2 = await add_key(s, t2, user, name="k2")
        k_foreign = await add_key(s, foreign, user, name="foreign")
    await _spend(factory, ScopeType.TEAM, t1.id, 60)
    await _spend(factory, ScopeType.TEAM, t2.id, 50)
    assert key_of(await compile_(factory), k1).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(await compile_(factory), k2).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(await compile_(factory), k1).policy.budget_window_end_unix_ms > 0
    assert key_of(await compile_(factory), k_foreign).status == pb.KEY_STATUS_ACTIVE


async def test_archived_team_spend_counts_for_the_org_and_its_keys_are_left_out(
    factory,
) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s, budget_nano_usd=100)
        live = await factories.make_team(s, org=org)
        archived = await factories.make_team(s, org=org)
        archived.deleted_at = datetime(OCT.year, OCT.month, 1, tzinfo=UTC)
        user = await factories.make_user(s)
        await factories.make_member(s, live, user)
        await factories.make_member(s, archived, user)
        live_key = await add_key(s, live, user, name="live")
        archived_key = await add_key(s, archived, user, name="archived")
        archived_team_id = archived.id
    # Archived teams still contribute to the org's derived monthly spend, so the live
    # team's key is blocked by the org budget alone.
    await _spend(factory, ScopeType.TEAM, archived_team_id, 150)
    compiled = await compile_(factory)
    assert key_of(compiled, live_key).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    # The archived team and its keys are not in the snapshot.
    assert str(archived_team_id) not in {team.id for team in compiled.snapshot.teams}
    assert str(archived_key.id) not in {k.key_id for k in compiled.snapshot.api_keys}


async def test_org_budget_under_its_teams_does_not_block(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s, budget_nano_usd=100)
        team = await factories.make_team(s, org=org)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        key = await add_key(s, team, user)
    await _spend(factory, ScopeType.TEAM, team.id, 99)
    assert key_of(await compile_(factory), key).status == pb.KEY_STATUS_ACTIVE


async def test_each_level_can_block_independently(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s, budget_nano_usd=10_000)
        team = await factories.make_team(s, org=org, budget_nano_usd=1_000)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user, budget_nano_usd=10)
        account = await factories.make_service_account(s, team, budget_nano_usd=10)
        user_key = await add_key(s, team, user, name="user")
        sa_key = await add_key(s, team, account, name="sa")
        other_user = await factories.make_user(s)
        await factories.make_member(s, team, other_user)
        other_key = await add_key(s, team, other_user, name="other")
    window = datetime(OCT.year, OCT.month, 1, tzinfo=UTC)
    await _spend(factory, ScopeType.MEMBER, member_scope_id(team.id, user.id), 10)
    await _spend(factory, ScopeType.SERVICE_ACCOUNT, account.id, 10)
    c = await compile_(factory)
    assert key_of(c, user_key).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, sa_key).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert key_of(c, other_key).status == pb.KEY_STATUS_ACTIVE
    # The team level blocks everyone once it is reached.
    await _spend(factory, ScopeType.TEAM, team.id, 1_000)
    c = await compile_(factory)
    assert key_of(c, other_key).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    assert window == datetime(OCT.year, OCT.month, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- scope cap


async def test_compiled_key_has_at_most_four_scopes(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        org = await factories.make_org(s, rpm_limit=1000)
        team = await factories.make_team(s, org=org, rpm_limit=100)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user, rpm_limit=50)
        key = await add_key(s, team, user, rpm_limit=10)
    compiled = await compile_(factory)
    assert len(key_of(compiled, key).policy.rate_limit_scopes) == MAX_RATE_LIMIT_SCOPES == 4
    # A fifth scope (which the source cannot currently produce) is rejected.
    snap = pb.Snapshot()
    snap.CopyFrom(compiled.snapshot)
    snap.api_keys[0].policy.rate_limit_scopes.append(
        pb.RateLimitScope(id="member:extra", limit=pb.RateLimit(requests_per_minute=1))
    )
    with pytest.raises(SnapshotInvalid, match="over the limit of 4"):
        validate_snapshot(snap, max_bytes=64 * 1024 * 1024, max_keys=100)

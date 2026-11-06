"""The one resolver for the org -> team -> owner -> key quota hierarchy."""

import uuid

from lumewarp_control_plane.models import (
    ApiKey,
    KeyOwnerKind,
    MemberPolicy,
    ModelAccess,
    Org,
    ScopeType,
    Team,
    member_scope_id,
)
from lumewarp_control_plane.quota import (
    BudgetLevel,
    Policy,
    effective,
    key_effective,
    org_effective,
    owner_effective,
    remaining_nano_usd,
    resolve_effective,
    team_effective,
)
from tests import factories

ALL, INHERIT, LIST = ModelAccess.ALL, ModelAccess.INHERIT, ModelAccess.LIST


def _org(**kw) -> Org:
    return Org(name="o", **kw)


def _team(**kw) -> Team:
    return Team(name="t", org_id=uuid.uuid4(), **kw)


def test_org_and_team_limits_are_the_minimum_down_the_chain() -> None:
    org = _org(rpm_limit=1000, tpm_limit=50_000, max_parallel=100, budget_nano_usd=900)
    team = _team(rpm_limit=500, tpm_limit=80_000, max_parallel=20, budget_nano_usd=500)
    org_eff = org_effective(org)
    team_eff = team_effective(org_eff, team)
    assert (org_eff.rpm_limit, org_eff.tpm_limit, org_eff.max_parallel) == (1000, 50_000, 100)
    assert (team_eff.rpm_limit, team_eff.tpm_limit, team_eff.max_parallel) == (
        500,
        50_000,
        20,
    )
    assert team_eff.budget_nano_usd == 500


def test_member_exception_defaults_and_service_accounts() -> None:
    org = _org(rpm_limit=1000)
    team = _team(
        rpm_limit=100,
        tpm_limit=10_000,
        member_rpm_default=40,
        member_tpm_default=5_000,
        member_max_parallel_default=8,
    )
    team_eff = team_effective(org_effective(org), team)
    # No exception row: the team's member defaults apply (external-backend case too).
    assert owner_effective(team_eff, team, None, service_account=False).rpm_limit == 40
    assert owner_effective(team_eff, team, None, service_account=False).tpm_limit == 5_000
    assert owner_effective(team_eff, team, None, service_account=False).max_parallel == 8
    # An exception wins over the default and is clamped by the team's own limit.
    exception = Policy(model_access=INHERIT, allowed_models=(), rpm_limit=60, tpm_limit=99_999)
    member = owner_effective(team_eff, team, exception, service_account=False)
    assert (member.rpm_limit, member.tpm_limit, member.max_parallel) == (60, 10_000, 8)
    # A service account never takes the member defaults; only its own policy applies.
    account = owner_effective(team_eff, team, None, service_account=True)
    assert (account.rpm_limit, account.tpm_limit, account.max_parallel) == (100, 10_000, None)


def test_models_intersect_down_the_chain() -> None:
    org = _org(model_access=LIST, allowed_models=("a", "b", "c", "d"))
    team = _team(model_access=LIST, allowed_models=("b", "c", "d"))
    team_eff = team_effective(org_effective(org), team)
    assert team_eff.models == {"b", "c", "d"}
    member = owner_effective(
        team_eff,
        team,
        Policy(model_access=LIST, allowed_models=("c", "d", "e")),
        service_account=False,
    )
    assert member.models == {"c", "d"}
    # INHERIT and ALL never widen the parent.
    assert owner_effective(team_eff, team, Policy(ALL, ()), service_account=False).models == {
        "b",
        "c",
        "d",
    }
    assert owner_effective(team_eff, team, None, service_account=False).models == {
        "b",
        "c",
        "d",
    }


def test_owner_scope_ids_and_budget_levels() -> None:
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    assert member_scope_id(team_id, user_id) == uuid.uuid5(team_id, str(user_id))
    assert ScopeType.ORG.value == "org"
    levels = [
        BudgetLevel(ScopeType.ORG, uuid.uuid4(), 1000, 900),
        BudgetLevel(ScopeType.TEAM, team_id, 500, 100),
    ]
    # Each level's own remaining is used, then the minimum is returned.
    assert remaining_nano_usd(levels) == 100
    assert remaining_nano_usd([]) is None


def test_key_level_clamps_by_owner() -> None:
    org = _org(rpm_limit=1000)
    team = _team(rpm_limit=100)
    team_eff = team_effective(org_effective(org), team)
    owner = owner_effective(
        team_eff,
        team,
        Policy(INHERIT, (), rpm_limit=50, max_parallel=4),
        service_account=False,
    )
    key = ApiKey(
        team_id=team.id,
        owner_kind=KeyOwnerKind.USER,
        user_id=uuid.uuid4(),
        name="k",
        key_hash=b"\0" * 32,
        display_prefix="lw",
        rpm_limit=70,
        max_parallel=2,
    )
    resolved = key_effective(owner, key)
    assert (resolved.rpm_limit, resolved.max_parallel) == (50, 2)
    assert resolved.budget_nano_usd is None


def test_resolve_effective_is_the_single_entry_point() -> None:
    org = Org(name="o", rpm_limit=1000)
    team = Team(name="t", org_id=org.id, rpm_limit=100)
    resolved = resolve_effective(org, team)
    assert resolved.rpm_limit == 100
    assert resolve_effective(org, team).models is None


def test_effective_semantics_stay_byte_compatible() -> None:
    parent = effective(None, Policy(LIST, ("a",)))
    assert effective(parent, Policy(INHERIT, ())).models == {"a"}
    assert effective(parent, Policy(LIST, ())).models == frozenset()


async def test_member_policy_is_independent_of_membership(session) -> None:
    """A policy row needs no `team_members` row (external authz backend)."""
    org = await factories.make_org(session)
    team = await factories.make_team(session, org=org)
    user = await factories.make_user(session)
    even_without_membership = MemberPolicy(team_id=team.id, user_id=user.id, rpm_limit=7)
    session.add(even_without_membership)
    await session.flush()
    assert (
        "model_access" not in factories.new_key(team, await factories.make_user(session)).__dict__
    )
    row = await session.get(MemberPolicy, (team.id, user.id))
    assert row is not None and row.rpm_limit == 7

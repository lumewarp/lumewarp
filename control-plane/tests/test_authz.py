"""Action permissions, org inheritance and the shared backend contract."""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.authz import (
    MATRIX,
    Action,
    Authorizer,
    AuthzUnavailable,
    Decision,
    Level,
    Relation,
    RequestAuthz,
    org_resource,
    team_resource,
)
from lumewarp_control_plane.authz.builtin import BuiltinBackend
from lumewarp_control_plane.authz.contract_tests import AuthzContract, AuthzSeed
from lumewarp_control_plane.db import create_session_factory
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import InstallationAdmin, InstallationRole, Org, Team, TeamRole
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests import factories

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
TEAM, OTHER = uuid.uuid4(), uuid.uuid4()
ROLES = ("installation_admin", "org_admin", "admin", "member", "outsider")
# team.create / team.delete stay installation-level in this change.
EXPECTED = {
    Action.TEAM_CREATE: "Y----",
    Action.TEAM_DELETE: "Y----",
    # Org level: the org admin may do this for a team of its own org, a team admin never.
    Action.TEAM_QUOTA_SET: "YY---",
    Action.TEAM_ADMIN_ASSIGN: "YY---",
    Action.INSTALLATION_ADMIN_MANAGE: "Y----",
    Action.USER_MANAGE: "Y----",
    Action.ROUTING_MANAGE: "Y----",
    Action.ROUTING_READ: "Y----",
    Action.GATEWAY_TOKEN_MANAGE: "Y----",
    Action.GATEWAY_READ: "Y----",
    Action.CONFIG_READ: "Y----",
    Action.CONFIG_ROLLBACK: "Y----",
    Action.CATALOG_READ: "YYYYY",
    Action.OWN_TOKENS: "YYYYY",
    # Team admin capability: inherited by an org admin for its own org's teams.
    Action.TEAM_SETTINGS_UPDATE: "YYY--",
    Action.TEAM_MEMBERS_MANAGE: "YYY--",
    Action.TEAM_ADMIN_READ: "YYY--",
    Action.SERVICE_ACCOUNT_MANAGE: "YYY--",
    Action.KEY_MANAGE_ANY: "YYY--",
    # Team read capability: every direct member, team admins and org admins.
    Action.TEAM_READ: "YYYY-",
    Action.KEY_CREATE_OWN: "YYYY-",
    Action.KEY_MANAGE_OWN: "YYYY-",
}


@pytest.fixture(scope="module")
async def hierarchy(engine):
    """Two orgs with one team each, committed, for team -> org resolution."""
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        session.add_all(
            [
                Org(id=ORG_A, name=f"org-a-{uuid7().hex[-8:]}"),
                Org(id=ORG_B, name=f"org-b-{uuid7().hex[-8:]}"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Team(id=TEAM, org_id=ORG_A, name=f"team-a-{uuid7().hex[-8:]}"),
                Team(id=OTHER, org_id=ORG_B, name=f"team-b-{uuid7().hex[-8:]}"),
            ]
        )
    return create_session_factory(engine)


def request_for(role, session_factory=None, calls=None):
    async def check(user_id, relation, resource):
        if calls is not None:
            calls.append((user_id, relation, resource))
        if role == "org_admin":
            allowed = relation is Relation.ADMIN and resource == org_resource(ORG_A)
        elif role == "admin":
            allowed = resource == team_resource(TEAM) and relation in (
                Relation.ADMIN,
                Relation.MEMBER,
            )
        elif role == "member":
            allowed = resource == team_resource(TEAM) and relation is Relation.MEMBER
        else:
            allowed = False
        return Decision.ALLOW if allowed else Decision.DENY

    return RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(check),
        session_factory,
        installation_role=(InstallationRole.ADMIN if role == "installation_admin" else None),
    )


def test_matrix_complete() -> None:
    assert set(EXPECTED) == set(Action) == set(MATRIX)
    installation = {
        Action.TEAM_CREATE,
        Action.TEAM_DELETE,
        Action.INSTALLATION_ADMIN_MANAGE,
        Action.USER_MANAGE,
        Action.ROUTING_MANAGE,
        Action.ROUTING_READ,
        Action.GATEWAY_TOKEN_MANAGE,
        Action.GATEWAY_READ,
        Action.CONFIG_READ,
        Action.CONFIG_ROLLBACK,
    }
    assert {a for a, rule in MATRIX.items() if rule.level is Level.INSTALLATION} == installation
    assert {a for a, rule in MATRIX.items() if rule.level is Level.ORG} == {
        Action.TEAM_QUOTA_SET,
        Action.TEAM_ADMIN_ASSIGN,
    }


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("team", [TEAM, OTHER])
async def test_action_matrix(action, role, team, hierarchy) -> None:
    allowed = EXPECTED[action][ROLES.index(role)] == "Y"
    if MATRIX[action].level in (Level.ORG, Level.TEAM) and team == OTHER:
        # A team of another org: no org inheritance, and only the installation admin.
        allowed = allowed and role == "installation_admin"
    authz = request_for(role, hierarchy)
    assert await authz.allowed(action, team) is allowed
    if allowed:
        await authz.require(action, team)
    else:
        with pytest.raises(ApiError) as exc:
            await authz.require(action, team)
        assert exc.value.status == 403


async def test_bootstrap_is_admin() -> None:
    async def unavailable(*args):
        raise AssertionError("bootstrap must bypass backend")

    authz = RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), Authorizer(unavailable))
    assert await authz.is_admin()
    for action in Action:
        assert await authz.allowed(action, TEAM)
    assert await authz.has(Relation.MEMBER, TEAM)
    assert await authz.readable_teams([TEAM, OTHER]) is None
    assert await authz.admin_teams([TEAM, OTHER]) is None
    assert await authz.admin_orgs([ORG_A, ORG_B]) is None


@pytest.mark.parametrize(
    "action", [a for a in Action if MATRIX[a].level in (Level.ORG, Level.TEAM)]
)
async def test_team_actions_need_team(action) -> None:
    with pytest.raises(ValueError, match="needs a team"):
        await request_for("member").allowed(action)


async def test_request_cache_and_isolation(hierarchy) -> None:
    calls = []
    authz = request_for("admin", hierarchy, calls)
    for _ in range(2):
        assert await authz.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM)
        assert await authz.allowed(Action.SERVICE_ACCOUNT_MANAGE, TEAM)
    # ADMIN on org A (denied) and ADMIN on TEAM (allowed); repeated questions are cached.
    assert [call[2] for call in calls] == [org_resource(ORG_A), team_resource(TEAM)]
    await authz.has(Relation.ADMIN, OTHER)
    assert [call[2] for call in calls] == [
        org_resource(ORG_A),
        team_resource(TEAM),
        org_resource(ORG_B),
        team_resource(OTHER),
    ]
    # MEMBER on TEAM is satisfied by the cached team admin capability, not a new relation.
    assert await authz.has(Relation.MEMBER, TEAM)
    assert len(calls) == 4
    await request_for("admin", hierarchy, calls).allowed(Action.TEAM_SETTINGS_UPDATE, TEAM)
    assert len(calls) == 6


async def test_error_raises_503_and_is_not_cached_but_a_deny_is(hierarchy) -> None:
    errors = iter([Decision.ERROR, Decision.ALLOW])

    async def flaky(*args):
        return next(errors)

    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    authz = RequestAuthz(principal, Authorizer(flaky), hierarchy, installation_role=None)
    with pytest.raises(AuthzUnavailable) as exc:
        await authz.require(Action.TEAM_READ, TEAM)
    assert (exc.value.status, exc.value.code) == (503, "authz_unavailable")
    # ERROR is not cached: the org lookup is asked again and this time allows.
    assert await authz.allowed(Action.TEAM_READ, TEAM)

    # A deny is cached for this request, so it does not consume another decision: the
    # iterator would be exhausted (ERROR/503), not `False`.
    denials = iter([Decision.DENY, Decision.DENY])

    async def deny(*args):
        return next(denials)

    denying = RequestAuthz(principal, Authorizer(deny), hierarchy, installation_role=None)
    assert await denying.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM) is False
    assert await denying.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM) is False


@pytest.mark.parametrize("role", ROLES)
async def test_readable_teams(role, hierarchy) -> None:
    assert await request_for(role, hierarchy).readable_teams([TEAM, OTHER]) == (
        None
        if role == "installation_admin"
        else {TEAM}
        if role in ("org_admin", "admin", "member")
        else set()
    )


@pytest.mark.parametrize("role", ROLES)
async def test_admin_teams(role, hierarchy) -> None:
    assert await request_for(role, hierarchy).admin_teams([TEAM, OTHER]) == (
        None
        if role == "installation_admin"
        else {TEAM}
        if role in ("org_admin", "admin")
        else set()
    )


async def test_admin_orgs(hierarchy) -> None:
    assert await request_for("org_admin", hierarchy).admin_orgs([ORG_A, ORG_B]) == {ORG_A}
    assert await request_for("admin", hierarchy).admin_orgs([ORG_A, ORG_B]) == set()


async def test_org_admin_inherits_admin_and_read(hierarchy) -> None:
    """An org admin administers its own teams but never another org's."""
    authz = request_for("org_admin", hierarchy)
    assert await authz.is_org_admin(ORG_A)
    assert not await authz.is_org_admin(ORG_B)
    for relation in (Relation.ADMIN, Relation.MEMBER):
        assert await authz.has(relation, TEAM)
        assert not await authz.has(relation, OTHER)
    for action in (Action.TEAM_QUOTA_SET, Action.TEAM_ADMIN_ASSIGN, Action.TEAM_READ):
        assert await authz.allowed(action, TEAM)
        assert not await authz.allowed(action, OTHER)


async def test_team_admin_does_not_get_org_actions(hierarchy) -> None:
    authz = request_for("admin", hierarchy)
    assert await authz.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM)
    assert not await authz.allowed(Action.TEAM_QUOTA_SET, TEAM)
    assert not await authz.allowed(Action.TEAM_ADMIN_ASSIGN, TEAM)
    assert not await authz.is_org_admin(ORG_A)


async def test_team_org_is_read_from_postgres_not_from_input(hierarchy) -> None:
    """The org-level check asks about the team's actual org (OTHER -> ORG_B)."""
    calls = []
    authz = request_for("org_admin", hierarchy, calls)
    assert not await authz.allowed(Action.TEAM_QUOTA_SET, OTHER)
    assert [call[2] for call in calls] == [org_resource(ORG_B)]
    assert await authz.allowed(Action.TEAM_QUOTA_SET, TEAM)
    assert [call[2] for call in calls] == [org_resource(ORG_B), org_resource(ORG_A)]


async def test_org_admin_is_not_a_team_member(hierarchy) -> None:
    """The direct relation stays false: inheritance is a `RequestAuthz` capability only."""
    authz = request_for("org_admin", hierarchy)
    assert await authz.has(Relation.ADMIN, TEAM)
    direct = await authz.authorizer.check(str(authz.user_id), Relation.MEMBER, team_resource(TEAM))
    assert direct is Decision.DENY


async def test_inheritance_errors_are_503_never_deny_or_empty(hierarchy) -> None:
    async def unavailable(*args):
        return Decision.ERROR

    authz = RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(unavailable),
        hierarchy,
        installation_role=None,
    )
    with pytest.raises(AuthzUnavailable):
        await authz.is_org_admin(ORG_A)
    with pytest.raises(AuthzUnavailable):
        await authz.has(Relation.ADMIN, TEAM)
    with pytest.raises(AuthzUnavailable):
        await authz.admin_teams([TEAM])
    with pytest.raises(AuthzUnavailable):
        await authz.readable_teams([TEAM])
    with pytest.raises(AuthzUnavailable):
        await authz.admin_orgs([ORG_A])


async def test_installation_role_is_local_not_a_backend_relation(engine) -> None:
    """The backend may be down; the installation role comes from Postgres."""
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = await factories.make_user(session)
        session.add(InstallationAdmin(user_id=user.id, role=InstallationRole.ADMIN))
    calls = []

    async def check(user_id, relation, resource):
        calls.append((user_id, relation, resource))
        return Decision.ERROR

    authz = RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=user.id),
        Authorizer(check),
        create_session_factory(engine),
    )
    assert await authz.installation_role() is InstallationRole.ADMIN
    assert await authz.is_admin()
    assert await authz.allowed(Action.USER_MANAGE)
    # Not even the org/team relation is asked for an installation admin.
    assert await authz.allowed(Action.TEAM_QUOTA_SET, TEAM)
    assert calls == []


@pytest.fixture
async def authz_seed(engine):
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        org = await factories.make_org(session, f"contract-{uuid7().hex[-8:]}")
        teams = (
            await factories.make_team(session, org=org),
            await factories.make_team(session, org=org),
        )
        admin = await factories.make_user(session)
        member = await factories.make_user(session)
        inactive = await factories.make_user(session, is_active=False)
        org_admin = (await factories.make_org_admin(session, org)).user_id
        await factories.make_member(session, teams[0], admin, role=TeamRole.ADMIN)
        await factories.make_member(session, teams[0], member)
        await factories.make_member(session, teams[0], inactive, role=TeamRole.ADMIN)
    return AuthzSeed(
        outsider=str(uuid.uuid4()),
        admin=str(admin.id),
        member=str(member.id),
        inactive=str(inactive.id),
        unknown=str(uuid.uuid4()),
        teams=(teams[0].id, teams[1].id),
        org=org.id,
        org_admin=str(org_admin),
    )


class TestBuiltinContract(AuthzContract):
    @pytest.fixture
    def authz_backend(self, engine):
        return BuiltinBackend(create_session_factory(engine))


class TestFunctionContract(AuthzContract):
    @pytest.fixture
    def authz_seed(self):
        return AuthzSeed(
            outsider=str(uuid.uuid4()),
            admin=str(uuid.uuid4()),
            member=str(uuid.uuid4()),
            inactive=str(uuid.uuid4()),
            unknown=str(uuid.uuid4()),
            teams=(uuid.uuid4(), uuid.uuid4()),
            org=uuid.uuid4(),
            org_admin=str(uuid.uuid4()),
        )

    @pytest.fixture
    def authz_backend(self, authz_seed):
        seed = authz_seed

        async def check(user, relation, resource):
            allowed = (
                resource == team_resource(seed.teams[0])
                and relation in (Relation.ADMIN, Relation.MEMBER)
                and (user == seed.admin or (user == seed.member and relation is Relation.MEMBER))
            ) or (
                user == seed.org_admin
                and relation is Relation.ADMIN
                and resource == org_resource(seed.org)
            )
            return Decision.ALLOW if allowed else Decision.DENY

        return check


async def test_function_batch_preserves_order_when_completed_in_reverse() -> None:
    first_started, second_finished = asyncio.Event(), asyncio.Event()

    async def check(user, relation, resource):
        if user == "first":
            first_started.set()
            await second_finished.wait()
            return Decision.ALLOW
        await first_started.wait()
        second_finished.set()
        return Decision.DENY

    async with asyncio.timeout(2):
        assert await Authorizer(check).batch_check(
            [
                ("first", Relation.MEMBER, team_resource("00000000-0000-0000-0000-000000000001")),
                ("second", Relation.MEMBER, team_resource("00000000-0000-0000-0000-000000000001")),
            ]
        ) == [Decision.ALLOW, Decision.DENY]

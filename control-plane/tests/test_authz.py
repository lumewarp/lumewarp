"""Action permissions and the shared backend contract."""

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
    team_resource,
)
from lumewarp_control_plane.authz.builtin import BuiltinBackend
from lumewarp_control_plane.authz.contract_tests import AuthzContract, AuthzSeed
from lumewarp_control_plane.db import create_session_factory
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import InstallationAdmin, InstallationRole, TeamRole
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests import factories

TEAM, OTHER = uuid.uuid4(), uuid.uuid4()
ROLES = ("installation_admin", "admin", "member", "outsider")
EXPECTED = {
    Action.TEAM_CREATE: "Y---",
    Action.TEAM_DELETE: "Y---",
    Action.TEAM_QUOTA_SET: "Y---",
    Action.TEAM_ADMIN_ASSIGN: "Y---",
    Action.INSTALLATION_ADMIN_MANAGE: "Y---",
    Action.USER_MANAGE: "Y---",
    Action.ROUTING_MANAGE: "Y---",
    Action.ROUTING_READ: "Y---",
    Action.GATEWAY_TOKEN_MANAGE: "Y---",
    Action.GATEWAY_READ: "Y---",
    Action.CONFIG_READ: "Y---",
    Action.CONFIG_ROLLBACK: "Y---",
    Action.CATALOG_READ: "YYYY",
    Action.OWN_TOKENS: "YYYY",
    Action.TEAM_SETTINGS_UPDATE: "YY--",
    Action.TEAM_MEMBERS_MANAGE: "YY--",
    Action.TEAM_ADMIN_READ: "YY--",
    Action.SERVICE_ACCOUNT_MANAGE: "YY--",
    Action.KEY_MANAGE_ANY: "YY--",
    Action.TEAM_READ: "YYY-",
    Action.KEY_CREATE_OWN: "YYY-",
    Action.KEY_MANAGE_OWN: "YYY-",
}


def request_for(role, calls=None):
    async def check(user_id, relation, resource):
        if calls is not None:
            calls.append((user_id, relation, resource))
        allowed = resource == team_resource(TEAM) and (
            role == "admin" or (role == "member" and relation is Relation.MEMBER)
        )
        return Decision.ALLOW if allowed else Decision.DENY

    return RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(check),
        installation_role=(InstallationRole.ADMIN if role == "installation_admin" else None),
    )


def test_matrix_complete() -> None:
    assert set(EXPECTED) == set(Action) == set(MATRIX)


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("team", [TEAM, OTHER])
async def test_action_matrix(action, role, team) -> None:
    allowed = EXPECTED[action][ROLES.index(role)] == "Y"
    if MATRIX[action].level is Level.TEAM and team == OTHER and role != "installation_admin":
        allowed = False
    authz = request_for(role)
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


@pytest.mark.parametrize("action", [a for a in Action if MATRIX[a].level is Level.TEAM])
async def test_team_actions_need_team(action) -> None:
    with pytest.raises(ValueError, match="needs a team"):
        await request_for("member").allowed(action)


@pytest.mark.parametrize("role", ["admin", "outsider"])
async def test_request_cache_and_isolation(role) -> None:
    calls = []
    authz = request_for(role, calls)
    for _ in range(2):
        await authz.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM)
        await authz.allowed(Action.SERVICE_ACCOUNT_MANAGE, TEAM)
    assert len(calls) == 1  # one team relation; the second question is cached
    await authz.has(Relation.ADMIN, OTHER)
    await authz.has(Relation.MEMBER, TEAM)
    assert len(calls) == 3
    await request_for(role, calls).allowed(Action.TEAM_SETTINGS_UPDATE, TEAM)
    assert len(calls) == 4


async def test_error_raises_503_and_is_not_cached_but_a_deny_is() -> None:
    decisions = iter([Decision.ERROR, Decision.ALLOW, Decision.DENY])

    async def check(*args):
        return next(decisions)

    authz = RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(check),
        installation_role=None,
    )
    with pytest.raises(AuthzUnavailable) as exc:
        await authz.require(Action.TEAM_READ, TEAM)
    assert (exc.value.status, exc.value.code) == (503, "authz_unavailable")
    # ERROR is not cached: the next question asks again and allows.
    assert await authz.allowed(Action.TEAM_READ, TEAM)
    # A deny is cached for this request, so it does not consume another decision.
    assert await authz.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM) is False
    assert await authz.allowed(Action.TEAM_SETTINGS_UPDATE, TEAM) is False


@pytest.mark.parametrize("role", ROLES)
async def test_readable_teams(role) -> None:
    assert await request_for(role).readable_teams([TEAM, OTHER]) == (
        None if role == "installation_admin" else {TEAM} if role in ("admin", "member") else set()
    )


@pytest.mark.parametrize("role", ROLES)
async def test_admin_teams(role) -> None:
    assert await request_for(role).admin_teams([TEAM, OTHER]) == (
        None if role == "installation_admin" else {TEAM} if role == "admin" else set()
    )


async def test_admin_teams_error() -> None:
    async def check(user, relation, resource):
        return Decision.ERROR

    authz = RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(check),
        installation_role=None,
    )
    with pytest.raises(AuthzUnavailable):
        await authz.admin_teams([TEAM])


async def test_readable_teams_error() -> None:
    async def check(user, relation, resource):
        return Decision.ERROR

    authz = RequestAuthz(
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        Authorizer(check),
        installation_role=None,
    )
    with pytest.raises(AuthzUnavailable):
        await authz.readable_teams([TEAM])


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
    assert calls == []


@pytest.fixture
async def authz_seed(engine):
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        teams = (await factories.make_team(session), await factories.make_team(session))
        installation_admin = await factories.make_user(session)
        admin = await factories.make_user(session)
        member = await factories.make_user(session)
        inactive = await factories.make_user(session, is_active=False)
        await factories.make_member(session, teams[0], admin, role=TeamRole.ADMIN)
        await factories.make_member(session, teams[0], member)
        await factories.make_member(session, teams[0], inactive, role=TeamRole.ADMIN)
    return AuthzSeed(
        str(installation_admin.id),
        str(admin.id),
        str(member.id),
        str(inactive.id),
        str(uuid.uuid4()),
        (teams[0].id, teams[1].id),
    )


class TestBuiltinContract(AuthzContract):
    @pytest.fixture
    def authz_backend(self, engine):
        return BuiltinBackend(create_session_factory(engine))


class TestFunctionContract(AuthzContract):
    @pytest.fixture
    def authz_seed(self):
        return AuthzSeed(
            *(str(uuid.uuid4()) for _ in range(5)),
            teams=(uuid.uuid4(), uuid.uuid4()),
        )

    @pytest.fixture
    def authz_backend(self, authz_seed):
        seed = authz_seed

        async def check(user, relation, resource):
            allowed = (
                resource == team_resource(seed.teams[0])
                and relation in (Relation.ADMIN, Relation.MEMBER)
                and (user == seed.admin or (user == seed.member and relation is Relation.MEMBER))
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

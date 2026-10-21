"""The P1 RBAC matrix, restated independently of rbac.MATRIX and checked exhaustively."""

import uuid

import pytest

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.rbac import (
    MATRIX,
    Action,
    Level,
    ResourceScope,
    authorize,
    readable_org_ids,
    readable_team_ids,
    require,
)
from lumewarp_control_plane.security import Grant, Principal, PrincipalKind

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
TEAM_A1, TEAM_A2, TEAM_B1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

ROLES = ("superuser", "org_owner", "org_admin", "team_admin", "member", "viewer")

# One column per role in ROLES, evaluated on the principal's own org/team.
# Y = allowed, - = denied.
EXPECTED: dict[Action, str] = {
    Action.ORG_CREATE: "Y-----",
    Action.ORG_DELETE: "Y-----",
    Action.USER_MANAGE: "Y-----",
    Action.ROUTING_MANAGE: "Y-----",
    Action.GATEWAY_TOKEN_MANAGE: "Y-----",
    Action.GATEWAY_READ: "Y-----",
    Action.CONFIG_READ: "Y-----",
    Action.CONFIG_ROLLBACK: "Y-----",
    Action.NOTIFIER_MANAGE: "Y-----",
    Action.ROUTING_READ: "YYYYYY",
    Action.OWN_TOKENS: "YYYYYY",
    Action.ORG_READ: "YYYYYY",
    Action.ORG_UPDATE: "YYY---",
    Action.ORG_MEMBERSHIP_MANAGE: "YYY---",
    Action.ORG_GRANT_OWNER: "YY----",
    Action.TEAM_CREATE: "YYY---",
    Action.TEAM_UPDATE: "YYYY--",
    Action.TEAM_DELETE: "YYY---",
    Action.TEAM_MEMBERSHIP_MANAGE: "YYY---",
    Action.PROJECT_CREATE: "YYYY--",
    Action.PROJECT_UPDATE: "YYYY--",
    Action.PROJECT_DELETE: "YYYY--",
    Action.KEY_CREATE: "YYYYY-",
    Action.KEY_UPDATE: "YYYYY-",
    Action.KEY_REVOKE: "YYYYY-",
    Action.READ: "YYYYYY",
    Action.AUDIT_READ: "YYYY--",
}


def principal(role: str) -> Principal:
    if role == "superuser":
        return Principal(kind=PrincipalKind.SUPERUSER, user_id=uuid.uuid4())
    if role in ("org_owner", "org_admin"):
        grant = Grant(ORG_A, None, role.removeprefix("org_"))
    else:
        grant = Grant(ORG_A, TEAM_A1, role.removeprefix("team_"))
    return Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), grants=(grant,))


def own_scope(action: Action) -> ResourceScope:
    match MATRIX[action].level:
        case Level.ORG:
            return ResourceScope(ORG_A)
        case Level.TEAM | Level.ORG_OR_TEAM:
            return ResourceScope(ORG_A, TEAM_A1)
        case _:
            return ResourceScope()


CASES = [(a, i, r) for a in Action for i, r in enumerate(ROLES)]


def test_expected_covers_every_action() -> None:
    assert set(EXPECTED) == set(Action) == set(MATRIX)


@pytest.mark.parametrize(("action", "col", "role"), CASES)
def test_own_scope(action: Action, col: int, role: str) -> None:
    assert authorize(principal(role), action, own_scope(action)) is (EXPECTED[action][col] == "Y")


@pytest.mark.parametrize(("action", "col", "role"), CASES)
def test_other_team_same_org(action: Action, col: int, role: str) -> None:
    """Org roles reach every team of their org; team roles only their own team."""
    level = MATRIX[action].level
    team_level = level in (Level.TEAM, Level.ORG_OR_TEAM)
    scope = ResourceScope(ORG_A, TEAM_A2) if team_level else own_scope(action)
    allowed = EXPECTED[action][col] == "Y"
    if team_level and role in ("team_admin", "member", "viewer"):
        allowed = False
    assert authorize(principal(role), action, scope) is allowed


@pytest.mark.parametrize(("action", "col", "role"), CASES)
def test_other_org(action: Action, col: int, role: str) -> None:
    level = MATRIX[action].level
    scope = {
        Level.ORG: ResourceScope(ORG_B),
        Level.TEAM: ResourceScope(ORG_B, TEAM_B1),
        Level.ORG_OR_TEAM: ResourceScope(ORG_B, TEAM_B1),
    }.get(level, ResourceScope())
    allowed = EXPECTED[action][col] == "Y"
    if level in (Level.ORG, Level.TEAM, Level.ORG_OR_TEAM) and role != "superuser":
        allowed = False
    assert authorize(principal(role), action, scope) is allowed


@pytest.mark.parametrize("action", [a for a in Action if MATRIX[a].level is Level.TEAM])
def test_team_actions_need_team_scope(action: Action) -> None:
    assert not authorize(principal("org_owner"), action, ResourceScope(ORG_A))


# Org-wide reads (team_id None): usage and audit for the whole org.
ORG_WIDE = {Action.READ: "YYY---", Action.AUDIT_READ: "YYY---"}


def test_org_wide_actions_are_the_org_or_team_ones() -> None:
    assert {a for a in Action if MATRIX[a].level is Level.ORG_OR_TEAM} == set(ORG_WIDE)


@pytest.mark.parametrize(
    ("action", "col", "role"), [(a, i, r) for a in ORG_WIDE for i, r in enumerate(ROLES)]
)
def test_org_wide_reads(action: Action, col: int, role: str) -> None:
    p = principal(role)
    assert authorize(p, action, ResourceScope(ORG_A)) is (ORG_WIDE[action][col] == "Y")
    # Never across orgs, and never without an org.
    assert authorize(p, action, ResourceScope(ORG_B)) is (role == "superuser")
    assert authorize(p, action, ResourceScope()) is (role == "superuser")


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_member_and_viewer_never_read_audit(role: str) -> None:
    p = principal(role)
    for scope in (
        ResourceScope(ORG_A),
        ResourceScope(ORG_A, TEAM_A1),
        ResourceScope(ORG_A, TEAM_A2),
    ):
        assert not authorize(p, Action.AUDIT_READ, scope)


def test_unknown_role_string_grants_nothing() -> None:
    p = Principal(kind=PrincipalKind.USER, grants=(Grant(ORG_A, None, "member"),))
    assert not authorize(p, Action.ORG_UPDATE, ResourceScope(ORG_A))
    assert not authorize(p, Action.READ, ResourceScope(ORG_A, TEAM_A1))


def test_bootstrap_is_superuser() -> None:
    p = Principal.bootstrap()
    assert all(authorize(p, a, ResourceScope()) for a in Action)


def test_require_raises_403() -> None:
    with pytest.raises(ApiError) as e:
        require(principal("viewer"), Action.KEY_CREATE, ResourceScope(ORG_A, TEAM_A1))
    assert e.value.status == 403


def test_team_scope_needs_org() -> None:
    with pytest.raises(ValueError):
        ResourceScope(None, TEAM_A1)


def test_readable_ids() -> None:
    assert readable_team_ids(principal("superuser"), ORG_A) is None
    assert readable_team_ids(principal("org_admin"), ORG_A) is None
    assert readable_team_ids(principal("org_admin"), ORG_B) == frozenset()
    assert readable_team_ids(principal("viewer"), ORG_A) == {TEAM_A1}
    assert readable_org_ids(principal("superuser")) is None
    assert readable_org_ids(principal("member")) == {ORG_A}

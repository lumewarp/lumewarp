"""P1 RBAC matrix (frozen in .omh/runs/p1/plan.md) as data, plus `authorize`.

Roles come from memberships: org-level Owner/Admin, team-level Admin/Member/Viewer. The
superuser (bootstrap token or `users.is_superuser`) may do everything. A team role only
applies to resources of that team ("own team"); org roles apply to the whole org.

Callers pass the hierarchy position of the resource being touched, after resolving it from
the database (never from client-supplied ids alone):
- installation-wide resources: `ResourceScope()`
- an org or something created directly under it (e.g. a new team): `ResourceScope(org_id)`
- a team, its projects/keys/usage/audit, or a project created in it:
  `ResourceScope(org_id, team_id)`
- org-wide usage or audit (READ / AUDIT_READ without a team): `ResourceScope(org_id)`;
  only org Owner/Admin (and superuser) pass, team roles need their own team_id
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from lumewarp_control_plane.errors import forbidden
from lumewarp_control_plane.models import OrgRole, TeamRole
from lumewarp_control_plane.security import Principal


class Role(StrEnum):
    SUPERUSER = "superuser"
    ORG_OWNER = "org_owner"
    ORG_ADMIN = "org_admin"
    TEAM_ADMIN = "team_admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Level(StrEnum):
    """Which part of ResourceScope an action needs."""

    ANY = "any"  # any authenticated principal, scope ignored
    INSTALLATION = "installation"  # superuser only, scope ignored
    ORG = "org"  # needs org_id
    TEAM = "team"  # needs org_id and team_id
    # Needs org_id; team_id optional. Org-wide (team_id None) only org roles count, since
    # team grants never match a missing team; with team_id it behaves like TEAM.
    ORG_OR_TEAM = "org_or_team"


class Action(StrEnum):
    # Installation-wide (superuser only).
    ORG_CREATE = "org.create"
    ORG_DELETE = "org.delete"
    USER_MANAGE = "user.manage"
    ROUTING_MANAGE = "routing.manage"  # upstreams, deployments, routes, prices
    GATEWAY_TOKEN_MANAGE = "gateway_token.manage"
    GATEWAY_READ = "gateway.read"
    CONFIG_READ = "config.read"  # config versions
    CONFIG_ROLLBACK = "config.rollback"
    NOTIFIER_MANAGE = "notifier.manage"
    # Any authenticated principal.
    ROUTING_READ = "routing.read"  # model aliases are needed to set allowlists
    OWN_TOKENS = "own_tokens.manage"  # own PATs, users/me
    # Org level.
    ORG_READ = "org.read"
    ORG_UPDATE = "org.update"  # settings, budget, limits, allowlist
    ORG_MEMBERSHIP_MANAGE = "org.membership.manage"
    ORG_GRANT_OWNER = "org.membership.grant_owner"
    TEAM_CREATE = "team.create"
    # Team level.
    TEAM_UPDATE = "team.update"  # incl. team budget/limits/allowlist
    TEAM_DELETE = "team.delete"
    TEAM_MEMBERSHIP_MANAGE = "team.membership.manage"
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    PROJECT_DELETE = "project.delete"
    KEY_CREATE = "key.create"
    KEY_UPDATE = "key.update"
    KEY_REVOKE = "key.revoke"
    # Team, projects, keys, usage. Org-wide (team_id None): org Owner/Admin only.
    READ = "read"
    # Audit rows. Org-wide (team_id None, e.g. org settings/memberships): org Owner/Admin.
    AUDIT_READ = "audit.read"


@dataclass(frozen=True)
class Rule:
    level: Level
    roles: frozenset[Role]


def _rule(level: Level, *roles: Role) -> Rule:
    return Rule(level, frozenset({Role.SUPERUSER, *roles}))


_ORG_MANAGERS = (Role.ORG_OWNER, Role.ORG_ADMIN)
_TEAM_ROLES = (Role.TEAM_ADMIN, Role.MEMBER, Role.VIEWER)

MATRIX: dict[Action, Rule] = {
    Action.ORG_CREATE: _rule(Level.INSTALLATION),
    Action.ORG_DELETE: _rule(Level.INSTALLATION),
    Action.USER_MANAGE: _rule(Level.INSTALLATION),
    Action.ROUTING_MANAGE: _rule(Level.INSTALLATION),
    Action.GATEWAY_TOKEN_MANAGE: _rule(Level.INSTALLATION),
    Action.GATEWAY_READ: _rule(Level.INSTALLATION),
    Action.CONFIG_READ: _rule(Level.INSTALLATION),
    Action.CONFIG_ROLLBACK: _rule(Level.INSTALLATION),
    Action.NOTIFIER_MANAGE: _rule(Level.INSTALLATION),
    Action.ROUTING_READ: _rule(Level.ANY, *_ORG_MANAGERS, *_TEAM_ROLES),
    Action.OWN_TOKENS: _rule(Level.ANY, *_ORG_MANAGERS, *_TEAM_ROLES),
    # At org level a team role counts if the principal has it on any team of the org.
    Action.ORG_READ: _rule(Level.ORG, *_ORG_MANAGERS, *_TEAM_ROLES),
    Action.ORG_UPDATE: _rule(Level.ORG, *_ORG_MANAGERS),
    Action.ORG_MEMBERSHIP_MANAGE: _rule(Level.ORG, *_ORG_MANAGERS),
    Action.ORG_GRANT_OWNER: _rule(Level.ORG, Role.ORG_OWNER),
    Action.TEAM_CREATE: _rule(Level.ORG, *_ORG_MANAGERS),
    Action.TEAM_UPDATE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN),
    Action.TEAM_DELETE: _rule(Level.TEAM, *_ORG_MANAGERS),
    Action.TEAM_MEMBERSHIP_MANAGE: _rule(Level.TEAM, *_ORG_MANAGERS),
    Action.PROJECT_CREATE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN),
    Action.PROJECT_UPDATE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN),
    Action.PROJECT_DELETE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN),
    Action.KEY_CREATE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN, Role.MEMBER),
    Action.KEY_UPDATE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN, Role.MEMBER),
    Action.KEY_REVOKE: _rule(Level.TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN, Role.MEMBER),
    Action.READ: _rule(Level.ORG_OR_TEAM, *_ORG_MANAGERS, *_TEAM_ROLES),
    Action.AUDIT_READ: _rule(Level.ORG_OR_TEAM, *_ORG_MANAGERS, Role.TEAM_ADMIN),
}

_ORG_ROLE = {OrgRole.OWNER.value: Role.ORG_OWNER, OrgRole.ADMIN.value: Role.ORG_ADMIN}
_TEAM_ROLE = {
    TeamRole.ADMIN.value: Role.TEAM_ADMIN,
    TeamRole.MEMBER.value: Role.MEMBER,
    TeamRole.VIEWER.value: Role.VIEWER,
}


@dataclass(frozen=True)
class ResourceScope:
    org_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.team_id is not None and self.org_id is None:
            raise ValueError("team scope needs its org_id")


def roles_in(principal: Principal, scope: ResourceScope, level: Level) -> frozenset[Role]:
    """Roles the principal holds that are relevant to `scope` at `level`."""
    if principal.is_superuser:
        return frozenset({Role.SUPERUSER})
    roles: set[Role] = set()
    for g in principal.grants:
        if g.org_id != scope.org_id:
            continue
        if g.team_id is None:
            if role := _ORG_ROLE.get(g.role):
                roles.add(role)
        elif level is Level.ORG or g.team_id == scope.team_id:
            if role := _TEAM_ROLE.get(g.role):
                roles.add(role)
    return frozenset(roles)


def authorize(principal: Principal, action: Action, scope: ResourceScope) -> bool:
    rule = MATRIX[action]
    if principal.is_superuser:
        return True
    match rule.level:
        case Level.ANY:
            return True
        case Level.INSTALLATION:
            return False
        case Level.ORG | Level.ORG_OR_TEAM:
            if scope.org_id is None:
                return False
        case Level.TEAM:
            if scope.org_id is None or scope.team_id is None:
                return False
    return bool(roles_in(principal, scope, rule.level) & rule.roles)


def require(principal: Principal, action: Action, scope: ResourceScope) -> None:
    """Raise 403 unless allowed. To avoid leaking existence, callers check READ (or
    ORG_READ) first and answer 404 when that fails.
    """
    if not authorize(principal, action, scope):
        raise forbidden(f"{action.value} not allowed")


def readable_team_ids(principal: Principal, org_id: uuid.UUID) -> frozenset[uuid.UUID] | None:
    """Teams of `org_id` whose data the principal may READ. None means every team (superuser
    or org Owner/Admin); an empty set means none.
    """
    if principal.is_superuser:
        return None
    if roles_in(principal, ResourceScope(org_id), Level.ORG) & set(_ORG_MANAGERS):
        return None
    return frozenset(
        g.team_id
        for g in principal.grants
        if g.org_id == org_id and g.team_id is not None and g.role in _TEAM_ROLE
    )


def readable_org_ids(principal: Principal) -> frozenset[uuid.UUID] | None:
    """Orgs the principal may ORG_READ. None means all (superuser)."""
    if principal.is_superuser:
        return None
    return frozenset(g.org_id for g in principal.grants)

"""Pluggable authorization.

Who may do what is decided in two layers:

- The action matrix (`MATRIX`) stays in Lumewarp: every admin API action needs either an
  installation role, any authenticated caller, or one relation on one org or team.
- A backend answers only *direct* relation questions: "is user U an `admin` of org O?",
  "is U an `admin` or `member` of team T?". That is a single async function,
  `check(user_id, relation, resource) -> Decision`, so OpenFGA, a home-grown AAA system or
  the built-in tables can be swapped without touching the API. Installation roles are never
  asked of a backend: they live in Postgres (`installation_admins`, see `installation.py`).

Inheritance lives in `RequestAuthz` only, and the backend never needs to know which team
belongs to which org:

    is_installation_admin        = a row in installation_admins, or break-glass
    is_org_admin(org)            = is_installation_admin or admin of org:<id>
    team_admin_capability(team)  = is_org_admin(team.org_id) or admin of team:<id>
    team_read_capability(team)   = team_admin_capability(team) or member of team:<id>

The team -> org mapping is always read from Postgres by `RequestAuthz`, never from a
request payload. A caller that needs a direct relation must ask for one explicitly and
must not use the inherited capabilities:

- `_is_member` (key creation) requires a direct membership, so an org admin who is not a
  member cannot own a key;
- the key reconciler and `Authorizer.list_objects(..., "team", ...)` see direct relations;
- `/users/me` lists direct memberships and the caller's `admin_orgs` separately;
- `is_admin()` is installation-level only (proxy_admin, admin, break-glass), never "any
  admin".

A backend guarantees that team `admin` implies `member`. The new contract is that
`member of team` is a **direct** membership only: a backend must not make an org admin
inherit it, because `_is_member`, `/users/me` and the reconciler would then be wrong.
The matrix separates reading a team's basic record (`TEAM_READ`, any member or above) from
reading what a team admin sees (`TEAM_ADMIN_READ`: members, service accounts, every key,
usage, audit). Team creation and archiving (`TEAM_CREATE`, `TEAM_DELETE`), team quota
(`TEAM_QUOTA_SET`) and admin assignment (`TEAM_ADMIN_ASSIGN`) are `ORG` level: an org admin
may do them for a team of its own org (or create one in it), a team admin for none.
Catalog names are readable by every caller (`CATALOG_READ`) and routing details need
`ROUTING_READ`, which is installation-level.
`Decision.ERROR` (backend down or timed out) is never treated as a deny:
the admin API answers 503 and background jobs skip
the round. The gateway never calls a backend (it only sees compiled snapshots).

Every backend, including the three built-ins, loads through one `lumewarp.authz` entry
point. `load_authorizer` passes an `AuthzContext` to a factory that can be called with one
argument; a plain async `check` function, an object with `check`, or a zero-argument
factory keeps working unchanged. The entry point may name any of those directly.
Everything besides `check` is optional and filled in by `Authorizer`:

- `batch_check(items)`: default runs `check` concurrently (bounded).
- `list_objects(user_id, relation, resource_type)`: the objects of one type where the user
  holds the direct relation. `list_teams(user_id, relation)` is the older name for the
  team case and is still accepted.
- `changes(cursor)`: a stream of users whose relations may have changed (`ChangeBatch`),
  used by the key reconciler to react within seconds. Without it the reconciler only
  rescans periodically.
- `manages_grants`: default False, i.e. memberships live in the external system and the
  admin API's member endpoints are read-only.

Every backend call is bounded by `LUMEWARP_AUTHZ_TIMEOUT_SECONDS`; a timeout or exception is
`Decision.ERROR`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import Depends, Request
from sqlalchemy import select

from lumewarp_control_plane.errors import ApiError, forbidden
from lumewarp_control_plane.metrics import DISCARD, Metrics
from lumewarp_control_plane.models import InstallationRole, Team
from lumewarp_control_plane.security import CurrentPrincipal, Principal

if TYPE_CHECKING:
    from lumewarp_control_plane.db import SessionFactory
    from lumewarp_control_plane.settings import Settings

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "lumewarp.authz"
BUILTIN = "builtin"
OPENFGA = "openfga"
HTTP = "http"
# Concurrent `check` calls per default `batch_check`.
BATCH_CONCURRENCY = 16


@dataclass(frozen=True)
class AuthzContext:
    """What an authorization backend factory receives.

    A factory registered in `lumewarp.authz` may take one argument (this context) or none;
    the context carries what the built-ins need without making every plugin depend on the
    settings class. A backend named by `LUMEWARP_AUTHZ_BACKEND` that is a plain `check`
    function or an object with `check` is used as is.
    """

    settings: Settings | None = None
    session_factory: SessionFactory | None = None
    metrics: Metrics = DISCARD


class Relation(StrEnum):
    ADMIN = "admin"  # on an org or a team
    MEMBER = "member"  # on a team (direct membership only)


@dataclass(frozen=True)
class Resource:
    type: Literal["org", "team"]
    id: str


def team_resource(team_id: uuid.UUID | str) -> Resource:
    return Resource("team", str(team_id))


def org_resource(org_id: uuid.UUID | str) -> Resource:
    return Resource("org", str(org_id))


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    # Backend unavailable or timed out. Never a deny: callers fail closed with 503.
    ERROR = "error"


CheckFn = Callable[[str, Relation, Resource], Awaitable[Decision]]
CheckItem = tuple[str, Relation, Resource]


@dataclass(frozen=True)
class ChangeBatch:
    """Result of `changes(cursor)`."""

    # Lumewarp user ids whose relations may have changed since `cursor`.
    user_ids: frozenset[str]
    # Opaque position to pass to the next `changes` call.
    cursor: str
    # The cursor was None, unknown or expired: changes may have been missed, so the
    # caller must rescan everything. `user_ids` may then be empty.
    reset: bool = False


class Backend(Protocol):
    """The minimum a backend implements. See the module docstring for optional methods."""

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision: ...


class AuthzUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(503, "authz_unavailable", "authorization backend unavailable")


class _FunctionBackend:
    def __init__(self, fn: CheckFn) -> None:
        self._fn = fn

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        return await self._fn(user_id, relation, resource)


class Authorizer:
    """A backend with the optional capabilities filled in from `check`.

    Every decision of `check` and every item of `batch_check` is counted in
    `lumewarp_cp_authz_checks_total{backend,decision}`.
    """

    def __init__(
        self,
        backend: Any,
        name: str = BUILTIN,
        timeout: float = 2.0,
        metrics: Metrics = DISCARD,
    ) -> None:
        if inspect.iscoroutinefunction(backend):
            backend = _FunctionBackend(backend)
        if not callable(getattr(backend, "check", None)):
            raise TypeError(f"authz backend {name!r} has no check()")
        self.backend = backend
        self.name = name
        self.timeout = timeout
        self.manages_grants: bool = bool(getattr(backend, "manages_grants", False))
        self.metrics = metrics
        metrics.init_authz_backend(name)

    @property
    def has_changes(self) -> bool:
        return getattr(self.backend, "changes", None) is not None

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        try:
            async with asyncio.timeout(self.timeout):
                decision = Decision(await self.backend.check(user_id, relation, resource))
        except Exception:
            log.exception("authz backend %s: check failed", self.name)
            decision = Decision.ERROR
        self.metrics.authz_checks.labels(self.name, decision.value).inc()
        return decision

    async def batch_check(self, items: Sequence[CheckItem]) -> list[Decision]:
        native = getattr(self.backend, "batch_check", None)
        if native is not None:
            try:
                async with asyncio.timeout(self.timeout * (math.ceil(len(items) / 50) + 1)):
                    raw = await native(list(items))
                if len(raw) != len(items):
                    raise ValueError("batch_check returned a different number of decisions")
                decisions = [Decision(d) for d in raw]
            except Exception:
                log.exception("authz backend %s: batch_check failed", self.name)
                decisions = [Decision.ERROR] * len(items)
            self.metrics.record_authz(self.name, Counter(d.value for d in decisions))
            return decisions
        # Each item goes through `check`, which counts it.
        limit = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(item: CheckItem) -> Decision:
            async with limit:
                return await self.check(*item)

        return list(await asyncio.gather(*(one(item) for item in items)))

    async def list_objects(
        self,
        user_id: str,
        relation: Relation,
        resource_type: Literal["org", "team"],
        candidates: Iterable[uuid.UUID],
    ) -> set[uuid.UUID] | None:
        """Objects among `candidates` where `user_id` holds the direct relation.

        None on ERROR. Never inherited: an org admin is not returned for a team. A
        backend may implement `list_objects(user_id, relation, resource_type)`; the older
        team-only `list_teams(user_id, relation)` still works.
        """
        candidates = list(candidates)
        if not candidates:
            return set()
        native = getattr(self.backend, "list_objects", None)
        args: tuple[Any, ...] = (user_id, relation, resource_type)
        if native is None and resource_type == "team":
            native = getattr(self.backend, "list_teams", None)
            args = (user_id, relation)
        if native is not None:
            try:
                async with asyncio.timeout(self.timeout):
                    found = await native(*args)
                return {uuid.UUID(str(t)) for t in found} & set(candidates)
            except Exception:
                log.exception("authz backend %s: list_objects failed", self.name)
                return None
        decisions = await self.batch_check(
            [(user_id, relation, Resource(resource_type, str(t))) for t in candidates]
        )
        if Decision.ERROR in decisions:
            return None
        return {t for t, d in zip(candidates, decisions, strict=True) if d is Decision.ALLOW}

    async def list_teams(
        self, user_id: str, relation: Relation, candidates: Iterable[uuid.UUID]
    ) -> set[uuid.UUID] | None:
        """Teams among `candidates` where `user_id` holds the direct relation; None on ERROR."""
        return await self.list_objects(user_id, relation, "team", candidates)

    async def changes(self, cursor: str | None) -> ChangeBatch | None:
        """The backend's change stream since `cursor`; None on error or when the backend
        has no `changes` (check `has_changes`).
        """
        native = getattr(self.backend, "changes", None)
        if native is None:
            return None
        try:
            async with asyncio.timeout(self.timeout):
                batch = await native(cursor)
            if not isinstance(batch, ChangeBatch):
                raise TypeError(f"changes() returned {type(batch).__name__}")
            return batch
        except Exception:
            log.exception("authz backend %s: changes failed", self.name)
            return None


def _instantiate(factory: Any, context: AuthzContext) -> Any:
    """Call a factory, passing the context only when it takes an argument.

    Compatibility: a zero-argument factory (or class) keeps working as before. A factory
    with a required (or named `context`) parameter receives `AuthzContext`.
    """
    if not inspect.isclass(factory) and (
        inspect.iscoroutinefunction(factory) or hasattr(factory, "check")
    ):
        return factory
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory()
    try:
        signature.bind()
    except TypeError:
        return factory(context)
    for name in signature.parameters:
        if name in ("context", "ctx"):
            return factory(context)
    return factory()


def load_authorizer(
    settings: Settings, session_factory: SessionFactory | None, metrics: Metrics = DISCARD
) -> Authorizer:
    """Load `LUMEWARP_AUTHZ_BACKEND` from the `lumewarp.authz` entry points.

    The built-ins register themselves in `pyproject.toml`; there is no special case here.
    """
    name = settings.authz_backend
    timeout = settings.authz_timeout_seconds
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise RuntimeError(f"unknown authz backend {name!r} (no {ENTRY_POINT_GROUP} entry point)")
    context = AuthzContext(settings=settings, session_factory=session_factory, metrics=metrics)
    return Authorizer(_instantiate(matches[0].load(), context), name, timeout, metrics)


# --------------------------------------------------------------------------- actions


class Level(StrEnum):
    ANY = "any"  # any authenticated caller
    INSTALLATION = "installation"  # a proxy_admin or admin row in installation_admins
    ORG = "org"  # `relation` on the org the target team belongs to (or `org_id`)
    TEAM = "team"  # an (inherited) capability on the team itself


class Action(StrEnum):
    # Installation-wide (proxy_admin, admin, or break-glass).
    ORG_MANAGE = "org.manage"  # create, rename, set policy, archive, assign org admins
    INSTALLATION_ADMIN_MANAGE = "installation_admin.manage"  # assign/remove/transfer roles
    USER_MANAGE = "user.manage"
    ROUTING_MANAGE = "routing.manage"  # upstreams, models, routers, prices
    # Upstreams, models, aliases, routers, strategies: prices and routing details.
    ROUTING_READ = "routing.read"
    GATEWAY_TOKEN_MANAGE = "gateway_token.manage"
    GATEWAY_READ = "gateway.read"
    CONFIG_READ = "config.read"  # config versions
    CONFIG_ROLLBACK = "config.rollback"
    # Org admin (and installation admin): a team of the caller's own org only. TEAM_CREATE
    # takes the org directly (no team exists yet); TEAM_DELETE archives.
    TEAM_CREATE = "team.create"
    TEAM_DELETE = "team.delete"  # DELETE /teams/{id} archives the team
    TEAM_QUOTA_SET = "team.quota.set"  # models, limits, budget
    TEAM_ADMIN_ASSIGN = "team.admin.assign"  # add/promote/demote/remove an admin
    # Any authenticated caller.
    CATALOG_READ = "catalog.read"  # names and kinds, for allowlist pickers
    OWN_TOKENS = "own_tokens.manage"  # own PATs, users/me
    # Team admin (org admins inherit this for their org's teams).
    TEAM_SETTINGS_UPDATE = "team.settings.update"  # name, member key limits
    TEAM_MEMBERS_MANAGE = "team.members.manage"  # incl. members' own limits
    TEAM_ADMIN_READ = "team.admin.read"  # members, service accounts, keys, usage, audit
    SERVICE_ACCOUNT_MANAGE = "service_account.manage"  # incl. their keys
    KEY_MANAGE_ANY = "key.manage_any"  # suspend/revoke any key of the team
    # Team member (admin included; org admins inherit this too).
    TEAM_READ = "team.read"  # the team's basic record (id, name, timestamps, key limits)
    KEY_CREATE_OWN = "key.create_own"  # a direct membership is checked by the caller
    KEY_MANAGE_OWN = "key.manage_own"


@dataclass(frozen=True)
class Rule:
    level: Level
    relation: Relation | None = None


MATRIX: dict[Action, Rule] = {
    **{
        a: Rule(Level.INSTALLATION)
        for a in (
            Action.ORG_MANAGE,
            Action.INSTALLATION_ADMIN_MANAGE,
            Action.USER_MANAGE,
            Action.ROUTING_MANAGE,
            Action.ROUTING_READ,
            Action.GATEWAY_TOKEN_MANAGE,
            Action.GATEWAY_READ,
            Action.CONFIG_READ,
            Action.CONFIG_ROLLBACK,
        )
    },
    # Org level: the relation is checked on the target team's own org, resolved from
    # Postgres, or on the org id given for TEAM_CREATE. A team admin never gets these,
    # and an org admin never another org's team.
    Action.TEAM_CREATE: Rule(Level.ORG, Relation.ADMIN),
    Action.TEAM_DELETE: Rule(Level.ORG, Relation.ADMIN),
    Action.TEAM_QUOTA_SET: Rule(Level.ORG, Relation.ADMIN),
    Action.TEAM_ADMIN_ASSIGN: Rule(Level.ORG, Relation.ADMIN),
    Action.CATALOG_READ: Rule(Level.ANY),
    Action.OWN_TOKENS: Rule(Level.ANY),
    **{
        a: Rule(Level.TEAM, Relation.ADMIN)
        for a in (
            Action.TEAM_SETTINGS_UPDATE,
            Action.TEAM_MEMBERS_MANAGE,
            Action.TEAM_ADMIN_READ,
            Action.SERVICE_ACCOUNT_MANAGE,
            Action.KEY_MANAGE_ANY,
        )
    },
    **{
        a: Rule(Level.TEAM, Relation.MEMBER)
        for a in (Action.TEAM_READ, Action.KEY_CREATE_OWN, Action.KEY_MANAGE_OWN)
    },
}


_UNSET = object()


class RequestAuthz:
    """Authorization for one admin API request.

    This is the only place where org -> team inheritance is applied; the backend answers
    direct relations only. Backend decisions are cached for the request, so a handler may
    ask the same question repeatedly without another round trip. The installation role and
    each team's org are read from Postgres once per request and cached too: a revoked role
    applies to the next request, and no caller can supply an org from a payload.
    """

    def __init__(
        self,
        principal: Principal,
        authorizer: Authorizer,
        session_factory: SessionFactory | None = None,
        *,
        installation_role: InstallationRole | None | object = _UNSET,
    ) -> None:
        self.principal = principal
        self.authorizer = authorizer
        self._session_factory = session_factory
        self._installation_role = installation_role
        self._cache: dict[tuple[Relation, Resource], Decision] = {}
        self._team_orgs: dict[uuid.UUID, uuid.UUID | None] = {}

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.principal.user_id

    async def _relation(self, relation: Relation, resource: Resource) -> bool:
        assert self.principal.user_id is not None
        key = (relation, resource)
        decision = self._cache.get(key)
        if decision is None:
            decision = await self.authorizer.check(str(self.principal.user_id), relation, resource)
            if decision is not Decision.ERROR:
                self._cache[key] = decision
        if decision is Decision.ERROR:
            raise AuthzUnavailable()
        return decision is Decision.ALLOW

    async def installation_role(self) -> InstallationRole | None:
        """The caller's installation role, read once from Postgres per request.

        Break-glass has no row (it is not a user). The authorization backend is never
        consulted for this.
        """
        if self._installation_role is not _UNSET:
            return self._installation_role  # type: ignore[return-value]
        if self.principal.user_id is None or self._session_factory is None:
            self._installation_role = None
            return None
        from lumewarp_control_plane.installation import role_of

        async with self._session_factory() as session:
            self._installation_role = await role_of(session, self.principal.user_id)
        return self._installation_role

    def set_installation_role(self, role: InstallationRole | None) -> None:
        """Replace the cached role after an in-lock re-read (see `installation.recheck_role`)."""
        self._installation_role = role

    async def is_admin(self) -> bool:
        """Installation level only: proxy_admin, admin or break-glass. Never "any admin"."""
        if self.principal.is_break_glass:
            return True
        return await self.installation_role() is not None

    async def _resolve_orgs(self, team_ids: Iterable[uuid.UUID]) -> None:
        """Read team -> org from Postgres for the ids not cached yet.

        Never from a payload: callers only pass a team id. A team that does not exist maps
        to None (the API checks existence first and answers 404).
        """
        missing = [t for t in dict.fromkeys(team_ids) if t not in self._team_orgs]
        if not missing:
            return
        if self._session_factory is None:
            raise RuntimeError("RequestAuthz needs a session factory to resolve team -> org")
        async with self._session_factory() as session:
            rows = await session.execute(select(Team.id, Team.org_id).where(Team.id.in_(missing)))
            found = {team_id: org_id for team_id, org_id in rows}
        for team_id in missing:
            self._team_orgs[team_id] = found.get(team_id)

    async def team_org(self, team_id: uuid.UUID) -> uuid.UUID | None:
        """The team's org, from Postgres (cached per request); None if it does not exist."""
        await self._resolve_orgs([team_id])
        return self._team_orgs[team_id]

    async def is_org_admin(self, org_id: uuid.UUID) -> bool:
        """Installation admin, or a direct `admin` relation on this org."""
        if await self.is_admin():
            return True
        return await self._relation(Relation.ADMIN, org_resource(org_id))

    async def _team_admin_capability(self, team_id: uuid.UUID) -> bool:
        """Org admin of the team's org, or a direct team `admin`."""
        org_id = await self.team_org(team_id)
        if org_id is not None and await self.is_org_admin(org_id):
            return True
        return await self._relation(Relation.ADMIN, team_resource(team_id))

    async def _team_read_capability(self, team_id: uuid.UUID) -> bool:
        """Team admin capability, or a direct `member` relation on the team."""
        if await self._team_admin_capability(team_id):
            return True
        return await self._relation(Relation.MEMBER, team_resource(team_id))

    async def has(self, relation: Relation, team_id: uuid.UUID) -> bool:
        """The caller's inherited capability for `relation` on the team.

        Installation admins always have it; org admins have both relations on their org's
        teams by inheritance. This is a capability, not a direct relation: never use it to
        decide membership (keys, the reconciler, `/users/me`).
        """
        if await self.is_admin():
            return True
        if relation is Relation.ADMIN:
            return await self._team_admin_capability(team_id)
        return await self._team_read_capability(team_id)

    async def allowed(
        self,
        action: Action,
        team_id: uuid.UUID | None = None,
        *,
        org_id: uuid.UUID | None = None,
    ) -> bool:
        rule = MATRIX[action]
        if rule.level is Level.ANY:
            return True
        if rule.level is Level.INSTALLATION:
            return await self.is_admin()
        if await self.is_admin():
            return True
        if rule.level is Level.ORG:
            target = org_id
            if target is None:
                if team_id is None:
                    raise ValueError(f"{action} needs a team or an org")
                target = await self.team_org(team_id)
            return target is not None and await self.is_org_admin(target)
        if team_id is None:
            raise ValueError(f"{action} needs a team")
        assert rule.relation is not None
        return await self.has(rule.relation, team_id)

    async def require(
        self,
        action: Action,
        team_id: uuid.UUID | None = None,
        *,
        org_id: uuid.UUID | None = None,
    ) -> None:
        """403 unless allowed. To avoid leaking existence, callers check TEAM_READ first
        (`ensure_team_read`) and answer 404 when that fails.
        """
        if not await self.allowed(action, team_id, org_id=org_id):
            raise forbidden(f"{action.value} not allowed")

    async def _direct_objects(
        self,
        relation: Relation,
        resource_type: Literal["org", "team"],
        candidates: Iterable[uuid.UUID],
    ) -> set[uuid.UUID]:
        assert self.principal.user_id is not None
        found = await self.authorizer.list_objects(
            str(self.principal.user_id), relation, resource_type, candidates
        )
        if found is None:
            raise AuthzUnavailable()
        return found

    async def admin_orgs(self, candidates: Iterable[uuid.UUID]) -> set[uuid.UUID] | None:
        """Orgs among `candidates` the caller administers (direct relation); None = all."""
        if await self.is_admin():
            return None
        return await self._direct_objects(Relation.ADMIN, "org", candidates)

    async def admin_teams(self, candidates: Iterable[uuid.UUID]) -> set[uuid.UUID] | None:
        """Teams among `candidates` the caller may administer; None means every team.

        A direct team admin, plus every team of an org the caller administers. Handlers use
        it to decide whether a caller sees the whole-team view (team and org admins) or
        only their own rows, and never to trust a filter they were given.
        """
        if await self.is_admin():
            return None
        candidates = list(dict.fromkeys(candidates))
        direct = await self._direct_objects(Relation.ADMIN, "team", candidates)
        await self._resolve_orgs(candidates)
        orgs = {t: o for t in candidates if (o := self._team_orgs.get(t)) is not None}
        if not orgs:
            return direct
        admin_orgs = await self._direct_objects(Relation.ADMIN, "org", set(orgs.values()))
        return direct | {team for team, org in orgs.items() if org in admin_orgs}

    async def readable_teams(self, candidates: Iterable[uuid.UUID]) -> set[uuid.UUID] | None:
        """Teams among `candidates` the caller may read; None means every team.

        A direct member (team admins imply member by contract) or anything the caller
        administers, inherited org included.
        """
        if await self.is_admin():
            return None
        candidates = list(dict.fromkeys(candidates))
        members = await self._direct_objects(Relation.MEMBER, "team", candidates)
        admins = await self.admin_teams(candidates)
        return members | (admins or set())


def get_authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


async def request_authz(request: Request, principal: CurrentPrincipal) -> RequestAuthz:
    return RequestAuthz(principal, get_authorizer(request), request.app.state.session_factory)


Authz = Annotated[RequestAuthz, Depends(request_authz)]

"""Pluggable authorization.

Who may do what is decided in two layers:

- The action matrix (`MATRIX`) stays in Lumewarp: every admin API action needs either an
  installation role, any authenticated caller, or one relation on one team.
- A backend answers only relation questions: "is user U an `admin` or `member` of team T?".
  That is a single async function, `check(user_id, relation, resource) -> Decision`, so
  OpenFGA, a home-grown AAA system or the built-in membership table can be swapped without
  touching the API. Installation roles are never asked of a backend: they live in Postgres
  (`installation_admins`, see `installation.py`).

A backend guarantees that team `admin` implies `member`. `RequestAuthz` applies the
installation roles to every team action, independently of the backend. The matrix
separates reading a team's basic record (`TEAM_READ`, any member) from reading what a team
admin sees (`TEAM_ADMIN_READ`: members, service accounts, every key, usage, audit), and it
keeps team quota (`TEAM_QUOTA_SET`) and admin assignment (`TEAM_ADMIN_ASSIGN`) at the
installation level, so a team admin cannot set either. Catalog names are readable by every caller
(`CATALOG_READ`) because allowlists are picked by name; routing details need
`ROUTING_READ`, which is installation-level.
`Decision.ERROR` (backend down or timed out) is never treated as a deny:
the admin API answers 503 and background jobs skip
the round. The gateway never calls a backend (it only sees compiled snapshots).

Backends are selected by `LUMEWARP_AUTHZ_BACKEND`: "builtin" (default, `builtin.py`),
"openfga" (`openfga.py`), "http" (`http.py`) or the name of a `lumewarp.authz` entry point.
The entry point may be the async `check` function itself, an object with a `check` method,
or a class / zero-argument factory returning one.
Everything besides `check` is optional and filled in by `Authorizer`:

- `batch_check(items)`: default runs `check` concurrently (bounded).
- `list_teams(user_id, relation, candidates)`: default filters `candidates` with
  `batch_check`.
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

from lumewarp_control_plane.errors import ApiError, forbidden
from lumewarp_control_plane.metrics import DISCARD, Metrics
from lumewarp_control_plane.models import InstallationRole
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


class Relation(StrEnum):
    ADMIN = "admin"  # on a team
    MEMBER = "member"  # on a team


@dataclass(frozen=True)
class Resource:
    type: Literal["team"]
    id: str


def team_resource(team_id: uuid.UUID | str) -> Resource:
    return Resource("team", str(team_id))


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

    async def list_teams(
        self, user_id: str, relation: Relation, candidates: Iterable[uuid.UUID]
    ) -> set[uuid.UUID] | None:
        """Teams among `candidates` where `user_id` holds `relation`; None on ERROR."""
        native = getattr(self.backend, "list_teams", None)
        candidates = list(candidates)
        if native is not None:
            try:
                async with asyncio.timeout(self.timeout):
                    teams = await native(user_id, relation)
                return {uuid.UUID(str(t)) for t in teams} & set(candidates)
            except Exception:
                log.exception("authz backend %s: list_teams failed", self.name)
                return None
        decisions = await self.batch_check(
            [(user_id, relation, team_resource(t)) for t in candidates]
        )
        if Decision.ERROR in decisions:
            return None
        return {t for t, d in zip(candidates, decisions, strict=True) if d is Decision.ALLOW}

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


def load_authorizer(
    settings: Settings, session_factory: SessionFactory, metrics: Metrics = DISCARD
) -> Authorizer:
    name = settings.authz_backend
    timeout = settings.authz_timeout_seconds
    if name == BUILTIN:
        from lumewarp_control_plane.authz.builtin import BuiltinBackend

        return Authorizer(BuiltinBackend(session_factory), BUILTIN, timeout, metrics)
    if name == OPENFGA:
        from lumewarp_control_plane.authz.openfga import OpenFGABackend

        return Authorizer(OpenFGABackend(settings), OPENFGA, timeout, metrics)
    if name == HTTP:
        from lumewarp_control_plane.authz.http import HttpBackend

        return Authorizer(HttpBackend(settings), HTTP, timeout, metrics)
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise RuntimeError(f"unknown authz backend {name!r} (no {ENTRY_POINT_GROUP} entry point)")
    obj = matches[0].load()
    if inspect.isclass(obj) or (
        callable(obj) and not inspect.iscoroutinefunction(obj) and not hasattr(obj, "check")
    ):
        obj = obj()
    return Authorizer(obj, name, timeout, metrics)


# --------------------------------------------------------------------------- actions


class Level(StrEnum):
    ANY = "any"  # any authenticated caller
    INSTALLATION = "installation"  # a proxy_admin or admin row in installation_admins
    TEAM = "team"  # a relation on the team


class Action(StrEnum):
    # Installation-wide (proxy_admin, admin, or break-glass).
    TEAM_CREATE = "team.create"
    TEAM_DELETE = "team.delete"
    TEAM_QUOTA_SET = "team.quota.set"  # models, limits, budget; installation-level
    TEAM_ADMIN_ASSIGN = (
        "team.admin.assign"  # add/promote/demote/remove an admin; installation-level
    )
    INSTALLATION_ADMIN_MANAGE = "installation_admin.manage"  # assign/remove/transfer roles
    USER_MANAGE = "user.manage"
    ROUTING_MANAGE = "routing.manage"  # upstreams, models, routers, prices
    # Upstreams, models, aliases, routers, strategies: prices and routing details.
    ROUTING_READ = "routing.read"
    GATEWAY_TOKEN_MANAGE = "gateway_token.manage"
    GATEWAY_READ = "gateway.read"
    CONFIG_READ = "config.read"  # config versions
    CONFIG_ROLLBACK = "config.rollback"
    # Any authenticated caller.
    CATALOG_READ = "catalog.read"  # names and kinds, for allowlist pickers
    OWN_TOKENS = "own_tokens.manage"  # own PATs, users/me
    # Team admin.
    TEAM_SETTINGS_UPDATE = "team.settings.update"  # name, member key limits
    TEAM_MEMBERS_MANAGE = "team.members.manage"  # incl. members' own limits
    TEAM_ADMIN_READ = "team.admin.read"  # members, service accounts, keys, usage, audit
    SERVICE_ACCOUNT_MANAGE = "service_account.manage"  # incl. their keys
    KEY_MANAGE_ANY = "key.manage_any"  # suspend/revoke any key of the team
    # Team member (admin included).
    TEAM_READ = "team.read"  # the team's basic record (id, name, timestamps, key limits)
    KEY_CREATE_OWN = "key.create_own"  # owner check is the caller's job
    KEY_MANAGE_OWN = "key.manage_own"


@dataclass(frozen=True)
class Rule:
    level: Level
    relation: Relation | None = None


MATRIX: dict[Action, Rule] = {
    **{
        a: Rule(Level.INSTALLATION)
        for a in (
            Action.TEAM_CREATE,
            Action.TEAM_DELETE,
            # Installation-level: a team admin cannot set quota or assign admins.
            Action.TEAM_QUOTA_SET,
            Action.TEAM_ADMIN_ASSIGN,
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

    Team decisions are cached for the request, so a handler may ask the same question
    repeatedly without another backend round trip. The installation role is read from
    Postgres once per request and cached too: a revoked role applies to the next request.
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
        if self.principal.is_break_glass:
            return True
        return await self.installation_role() is not None

    async def has(self, relation: Relation, team_id: uuid.UUID) -> bool:
        """Whether the caller holds `relation` on the team (installation admins always do)."""
        if await self.is_admin():
            return True
        return await self._relation(relation, team_resource(team_id))

    async def allowed(self, action: Action, team_id: uuid.UUID | None = None) -> bool:
        rule = MATRIX[action]
        if rule.level is Level.ANY:
            return True
        if rule.level is Level.INSTALLATION:
            return await self.is_admin()
        if team_id is None:
            raise ValueError(f"{action} needs a team")
        assert rule.relation is not None
        return await self.has(rule.relation, team_id)

    async def require(self, action: Action, team_id: uuid.UUID | None = None) -> None:
        """403 unless allowed. To avoid leaking existence, callers check TEAM_READ first
        (`ensure_team_read`) and answer 404 when that fails.
        """
        if not await self.allowed(action, team_id):
            raise forbidden(f"{action.value} not allowed")

    async def readable_teams(self, candidates: Iterable[uuid.UUID]) -> set[uuid.UUID] | None:
        """Teams among `candidates` the caller may read; None means every team."""
        if await self.is_admin():
            return None
        assert self.principal.user_id is not None
        teams = await self.authorizer.list_teams(
            str(self.principal.user_id), Relation.MEMBER, candidates
        )
        if teams is None:
            raise AuthzUnavailable()
        return teams

    async def admin_teams(self, candidates: Iterable[uuid.UUID]) -> set[uuid.UUID] | None:
        """Teams among `candidates` the caller may administer; None means every team.

        Handlers use it to decide whether a caller sees the whole-team view (team
        admins) or only their own rows, and never to trust a filter they were given.
        """
        if await self.is_admin():
            return None
        assert self.principal.user_id is not None
        teams = await self.authorizer.list_teams(
            str(self.principal.user_id), Relation.ADMIN, candidates
        )
        if teams is None:
            raise AuthzUnavailable()
        return teams


def get_authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


async def request_authz(request: Request, principal: CurrentPrincipal) -> RequestAuthz:
    return RequestAuthz(principal, get_authorizer(request), request.app.state.session_factory)


Authz = Annotated[RequestAuthz, Depends(request_authz)]

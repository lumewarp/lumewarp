"""Pluggable authorization.

Who may do what is decided in two layers:

- The action matrix (`MATRIX`) stays in Lumewarp: every admin API action needs either the
  installation superuser, any authenticated caller, or one relation on one team.
- A backend answers only relation questions: "is user U the `superuser` of the
  installation / an `admin` or `member` of team T?". That is a single async function,
  `check(user_id, relation, resource) -> Decision`, so OpenFGA, a home-grown AAA system or
  the built-in membership table can be swapped without touching the API.

A backend guarantees the implied relations itself: `admin` implies `member`, and the
installation `superuser` implies `admin` of every team. `Decision.ERROR` (backend down or
timed out) is never treated as a deny: the admin API answers 503 and background jobs skip
the round. The gateway never calls a backend (it only sees compiled snapshots).

Backends are selected by `LUMEWARP_AUTHZ_BACKEND`: "builtin" (default, `builtin.py`) or the
name of a `lumewarp.authz` entry point. The entry point may be the async `check` function
itself, an object with a `check` method, or a class / zero-argument factory returning one.
Everything besides `check` is optional and filled in by `Authorizer`:

- `batch_check(items)`: default runs `check` concurrently (bounded).
- `list_teams(user_id, relation, candidates)`: default filters `candidates` with
  `batch_check`.
- `manages_grants`: default False, i.e. memberships live in the external system and the
  admin API's member endpoints are read-only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import Depends, Request

from lumewarp_control_plane.errors import ApiError, forbidden
from lumewarp_control_plane.security import CurrentPrincipal, Principal

if TYPE_CHECKING:
    from lumewarp_control_plane.db import SessionFactory
    from lumewarp_control_plane.settings import Settings

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "lumewarp.authz"
BUILTIN = "builtin"
# Concurrent `check` calls per default `batch_check`.
BATCH_CONCURRENCY = 16


class Relation(StrEnum):
    SUPERUSER = "superuser"  # on the installation only
    ADMIN = "admin"  # on a team
    MEMBER = "member"  # on a team


@dataclass(frozen=True)
class Resource:
    type: Literal["installation", "team"]
    id: str


INSTALLATION = Resource("installation", "default")


def team_resource(team_id: uuid.UUID | str) -> Resource:
    return Resource("team", str(team_id))


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    # Backend unavailable or timed out. Never a deny: callers fail closed with 503.
    ERROR = "error"


CheckFn = Callable[[str, Relation, Resource], Awaitable[Decision]]
CheckItem = tuple[str, Relation, Resource]


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
    """A backend with the optional capabilities filled in from `check`."""

    def __init__(self, backend: Any, name: str = BUILTIN) -> None:
        if inspect.iscoroutinefunction(backend):
            backend = _FunctionBackend(backend)
        if not callable(getattr(backend, "check", None)):
            raise TypeError(f"authz backend {name!r} has no check()")
        self.backend = backend
        self.name = name
        self.manages_grants: bool = bool(getattr(backend, "manages_grants", False))

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        try:
            decision = await self.backend.check(user_id, relation, resource)
        except Exception:
            log.exception("authz backend %s: check failed", self.name)
            return Decision.ERROR
        return Decision(decision)

    async def batch_check(self, items: Sequence[CheckItem]) -> list[Decision]:
        native = getattr(self.backend, "batch_check", None)
        if native is not None:
            try:
                decisions = await native(list(items))
            except Exception:
                log.exception("authz backend %s: batch_check failed", self.name)
                return [Decision.ERROR] * len(items)
            return [Decision(d) for d in decisions]
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
                return {uuid.UUID(str(t)) for t in await native(user_id, relation)} & set(
                    candidates
                )
            except Exception:
                log.exception("authz backend %s: list_teams failed", self.name)
                return None
        decisions = await self.batch_check(
            [(user_id, relation, team_resource(t)) for t in candidates]
        )
        if Decision.ERROR in decisions:
            return None
        return {t for t, d in zip(candidates, decisions, strict=True) if d is Decision.ALLOW}


def load_authorizer(settings: Settings, session_factory: SessionFactory) -> Authorizer:
    name = settings.authz_backend
    if name == BUILTIN:
        from lumewarp_control_plane.authz.builtin import BuiltinBackend

        return Authorizer(BuiltinBackend(session_factory), BUILTIN)
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise RuntimeError(f"unknown authz backend {name!r} (no {ENTRY_POINT_GROUP} entry point)")
    obj = matches[0].load()
    if inspect.isclass(obj) or (
        callable(obj) and not inspect.iscoroutinefunction(obj) and not hasattr(obj, "check")
    ):
        obj = obj()
    return Authorizer(obj, name)


# --------------------------------------------------------------------------- actions


class Level(StrEnum):
    ANY = "any"  # any authenticated caller
    INSTALLATION = "installation"  # superuser only
    TEAM = "team"  # a relation on the team


class Action(StrEnum):
    # Installation-wide (superuser only).
    TEAM_CREATE = "team.create"
    TEAM_DELETE = "team.delete"
    USER_MANAGE = "user.manage"
    ROUTING_MANAGE = "routing.manage"  # upstreams, deployments, routes, prices
    GATEWAY_TOKEN_MANAGE = "gateway_token.manage"
    GATEWAY_READ = "gateway.read"
    CONFIG_READ = "config.read"  # config versions
    CONFIG_ROLLBACK = "config.rollback"
    NOTIFIER_MANAGE = "notifier.manage"
    # Any authenticated caller.
    ROUTING_READ = "routing.read"  # model aliases are needed to set allowlists
    OWN_TOKENS = "own_tokens.manage"  # own PATs, users/me
    # Team admin.
    TEAM_UPDATE = "team.update"  # name, budget, limits, allowlist
    TEAM_MEMBERS_MANAGE = "team.members.manage"  # incl. members' own limits
    SERVICE_ACCOUNT_MANAGE = "service_account.manage"  # incl. their keys
    KEY_MANAGE_ANY = "key.manage_any"  # suspend/revoke any key of the team
    # Team member (admin included).
    TEAM_READ = "team.read"  # team, members, service accounts, key metadata, usage, audit
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
            Action.USER_MANAGE,
            Action.ROUTING_MANAGE,
            Action.GATEWAY_TOKEN_MANAGE,
            Action.GATEWAY_READ,
            Action.CONFIG_READ,
            Action.CONFIG_ROLLBACK,
            Action.NOTIFIER_MANAGE,
        )
    },
    Action.ROUTING_READ: Rule(Level.ANY),
    Action.OWN_TOKENS: Rule(Level.ANY),
    **{
        a: Rule(Level.TEAM, Relation.ADMIN)
        for a in (
            Action.TEAM_UPDATE,
            Action.TEAM_MEMBERS_MANAGE,
            Action.SERVICE_ACCOUNT_MANAGE,
            Action.KEY_MANAGE_ANY,
        )
    },
    **{
        a: Rule(Level.TEAM, Relation.MEMBER)
        for a in (Action.TEAM_READ, Action.KEY_CREATE_OWN, Action.KEY_MANAGE_OWN)
    },
}


class RequestAuthz:
    """Authorization for one admin API request. Results are cached for the request, so a
    handler may ask the same question repeatedly without another backend round trip.
    """

    def __init__(self, principal: Principal, authorizer: Authorizer) -> None:
        self.principal = principal
        self.authorizer = authorizer
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

    async def is_superuser(self) -> bool:
        if self.principal.is_bootstrap:
            return True
        return await self._relation(Relation.SUPERUSER, INSTALLATION)

    async def has(self, relation: Relation, team_id: uuid.UUID) -> bool:
        """Whether the caller holds `relation` on the team (superusers always do)."""
        if await self.is_superuser():
            return True
        return await self._relation(relation, team_resource(team_id))

    async def allowed(self, action: Action, team_id: uuid.UUID | None = None) -> bool:
        rule = MATRIX[action]
        if rule.level is Level.ANY:
            return True
        if rule.level is Level.INSTALLATION:
            return await self.is_superuser()
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
        if await self.is_superuser():
            return None
        assert self.principal.user_id is not None
        teams = await self.authorizer.list_teams(
            str(self.principal.user_id), Relation.MEMBER, candidates
        )
        if teams is None:
            raise AuthzUnavailable()
        return teams


def get_authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


async def request_authz(request: Request, principal: CurrentPrincipal) -> RequestAuthz:
    return RequestAuthz(principal, get_authorizer(request))


Authz = Annotated[RequestAuthz, Depends(request_authz)]

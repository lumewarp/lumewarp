"""Virtual API keys: create, inspect, update, revoke and rotate.

The raw `lw-...` secret is returned exactly once from `createKey` / `rotateKey`; only its
sha256 and a short display prefix are stored. A key is owned by a team member or by a
service account. Its own policy is the last level of the hierarchy: optional limits, and a
model list that inherits the owner's set or narrows it (`model_access` `inherit`/`list`).
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from pydantic import Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_key_read,
    ensure_not_null,
    get_team_or_404,
    lock_hierarchy,
    mutation,
    parse_uuid_cursor,
    validation_error,
)
from lumewarp_control_plane.api.key_refs import key_refs
from lumewarp_control_plane.api.openapi import RATE_LIMITED
from lumewarp_control_plane.api.policy import (
    Policy,
    member_defaults_of,
    owner_effective_of,
    policy_of,
    save_model_grants,
    validate_key_limits,
    validate_key_models,
    violation,
)
from lumewarp_control_plane.api.schemas import (
    KeyCreate,
    KeyRotate,
    KeyUpdate,
    Page,
)
from lumewarp_control_plane.api.schemas import KeyCreated as KeyCreatedBase
from lumewarp_control_plane.api.schemas import KeyOut as KeyOutBase
from lumewarp_control_plane.api.zone_limits import limits_of, zone_or_404
from lumewarp_control_plane.authz import (
    Action,
    Authz,
    AuthzUnavailable,
    Decision,
    RequestAuthz,
)
from lumewarp_control_plane.errors import ApiError, forbidden, not_found
from lumewarp_control_plane.keys.owners import key_owner_decisions
from lumewarp_control_plane.metrics import note_suspended
from lumewarp_control_plane.models import (
    ApiKey,
    KeyLifecycle,
    KeyMinCreatorRole,
    KeyOwnerKind,
    MemberPolicy,
    ModelAccess,
    Org,
    ServiceAccount,
    Team,
    TeamMember,
    User,
    Zone,
    ZoneLimit,
)
from lumewarp_control_plane.quota import org_effective, owner_effective, team_effective
from lumewarp_control_plane.security import SecretKind, generate_secret

router = APIRouter(tags=["keys"])

# Self-service writes are rate limited per principal and operation, in this process. The
# target (team or key) is deliberately not part of the scope: otherwise a caller with many
# keys or teams could multiply the quota, and unknown UUIDs would allocate entries. The
# control plane runs one process (README); a fixed window is enough to stop a runaway
# client without a database write per attempt.
KEY_WRITE_WINDOW_SECONDS = 60
KEY_CREATE_LIMIT = 30
KEY_ROTATE_LIMIT = 30
KEY_UPDATE_LIMIT = 30
MAX_LIMITER_ENTRIES = 10_000


@dataclass
class KeyWriteLimiter:
    """Per-scope fixed-window counters with amortized, incremental expiry.

    `entries` is the live table and `expiries` is ordered by expiry (every counter keeps its
    original deadline), so a call only pops the expired front instead of rebuilding the
    table. At most three entries exist per principal (create, rotate and update), so the
    capacity bounds principals. Normal hits and refusals are O(1). A full table refuses a new scope
    and never evicts a live counter: no one can free another principal's quota by creating
    targets.
    """

    capacity: int = MAX_LIMITER_ENTRIES
    window_seconds: float = KEY_WRITE_WINDOW_SECONDS
    entries: dict[str, tuple[int, float]] = field(default_factory=dict)
    expiries: deque[tuple[str, float]] = field(default_factory=deque)

    def _purge(self, now: float) -> None:
        while self.expiries and self.expiries[0][1] <= now:
            scope, until = self.expiries.popleft()
            current = self.entries.get(scope)
            if current is not None and current[1] == until:
                del self.entries[scope]

    def _retry_after(self, now: float) -> int:
        if not self.expiries:
            return max(1, ceil(self.window_seconds))
        return max(1, ceil(self.expiries[0][1] - now))

    def reserve(self, scope: str, limit: int) -> int:
        """Count one attempt; return 0 when allowed, else seconds until the window ends."""
        now = time.monotonic()
        self._purge(now)
        row = self.entries.get(scope)
        if row is None:
            if len(self.entries) >= self.capacity:
                return self._retry_after(now)
            until = now + self.window_seconds
            self.entries[scope] = (1, until)
            self.expiries.append((scope, until))
            return 0
        count, until = row
        if count >= limit:
            return max(1, ceil(until - now))
        self.entries[scope] = (count + 1, until)
        return 0


def _limiter(request: Request) -> KeyWriteLimiter:
    limiter = getattr(request.app.state, "key_write_limiter", None)
    if limiter is None:
        limiter = KeyWriteLimiter()
        request.app.state.key_write_limiter = limiter
    return limiter


def _principal_scope(authz: Authz) -> str:
    return str(authz.user_id) if authz.user_id is not None else "break_glass"


async def _rate_limit(
    request: Request, authz: Authz, operation: str, limit: int, metrics: MetricsDep
) -> None:
    """Rate limit after authentication, before target lookup and authorization.

    Installation admins (or a break-glass session) provision in
    bulk and are exempt; `RequestAuthz.is_admin()` is the same check the rest of the API
    uses. A team admin is only an admin of that team, so this returns False and they stay
    limited. The scope contains no target, so a missing or foreign UUID cannot buy quota or
    allocate an entry; running before the target lookup keeps refusals cheap.
    """
    if await authz.is_admin():
        return
    retry_after = _limiter(request).reserve(f"{operation}:{_principal_scope(authz)}", limit)
    if retry_after:
        # Deliberately no audit row: 429s are cheap to trigger and would let a client flood
        # the audit table. The metric still records the refusal.
        metrics.key_writes.labels(operation, "rate_limited").inc()
        raise ApiError(
            429,
            "rate_limited",
            f"too many key {operation} attempts; retry later",
            {"Retry-After": str(retry_after)},
        )


async def _owner_effective(session: AsyncSession, team: Team, key: ApiKey):
    """The effective policy of a key's owner in the key's zone."""
    if key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT:
        account = await session.get(ServiceAccount, key.service_account_id)
        if account is None:
            raise not_found("service account not found")
        limits = await limits_of(
            session, key.zone, {"team_id": team.id, "service_account_id": account.id}
        )
        return await owner_effective_of(
            session,
            team,
            policy_of(account, limits),
            service_account=True,
            zone=key.zone,
        )
    policy_row = await session.get(MemberPolicy, (key.team_id, key.user_id))
    limits = await limits_of(session, key.zone, {"team_id": team.id, "user_id": key.user_id})
    return await owner_effective_of(
        session,
        team,
        policy_of(policy_row, limits) if policy_row is not None else None,
        zone=key.zone,
    )


async def _check_key_limits(
    session: AsyncSession,
    team: Team,
    key: ApiKey,
    body: KeyCreate | KeyUpdate,
    *,
    create: bool = False,
) -> None:
    """A key's limits may not exceed its owner's effective values (422).

    Only *changed* values are checked. On create every supplied limit is new; on update a
    supplied value equal to the stored one is not rechecked, so after the owner's limit
    shrank a PATCH of another limit (or of an unchanged one) still succeeds. The other
    fields are passed as None and therefore not validated.
    """
    supplied = body.model_fields_set & {"rpm_limit", "tpm_limit", "max_parallel"}
    changed = {field for field in supplied if create or getattr(body, field) != getattr(key, field)}
    if not changed:
        return
    effective = await _owner_effective(session, team, key)
    await validate_key_limits(
        effective,
        rpm_limit=body.rpm_limit if "rpm_limit" in changed else None,
        tpm_limit=body.tpm_limit if "tpm_limit" in changed else None,
        max_parallel=body.max_parallel if "max_parallel" in changed else None,
    )


_KEY_MODEL_FIELDS = {"model_access", "allowed_models", "allowed_groups"}


def _key_policy(key: ApiKey) -> Policy:
    """The key's stored model half as a policy (limits are checked separately)."""
    return Policy(
        model_access=key.model_access,
        allowed_models=tuple(key.allowed_models or ()),
        allowed_groups=tuple(key.allowed_groups or ()),
        group_models=tuple(key.granted_models or ()),
    )


def _proposed_key_policy(body: KeyCreate | KeyUpdate, base: Policy | None) -> Policy:
    """The key's model list after this write, following `policy_from_fields`'s rules:
    a list is only accepted under `list`, and leaving `list` clears both lists.
    """
    supplied = body.model_fields_set
    if "model_access" in supplied and body.model_access is not None:
        mode = ModelAccess(body.model_access)
    else:
        mode = base.model_access if base is not None else ModelAccess.INHERIT
    lists: dict[str, tuple[str, ...]] = {}
    for name in ("allowed_models", "allowed_groups"):
        value = getattr(body, name)
        if name in supplied and value is not None:
            if value and mode is not ModelAccess.LIST:
                raise violation(name, "only allowed when model_access is 'list'")
            lists[name] = tuple(sorted(value))
        elif base is not None and base.model_access is ModelAccess.LIST:
            lists[name] = getattr(base, name)
        else:
            lists[name] = ()
        if mode is not ModelAccess.LIST:
            lists[name] = ()
    return Policy(
        model_access=mode,
        allowed_models=lists["allowed_models"],
        allowed_groups=lists["allowed_groups"],
    )


async def _owner_models(
    session: AsyncSession, keys: list[ApiKey]
) -> dict[uuid.UUID, list[str] | None]:
    """Each key's owner's effective model set (None = every name), batched per page.

    The key model picker is scoped by it. Teams, orgs and owner rows are read once for the
    whole page, never once per key.
    """
    if not keys:
        return {}
    # Per distinct team and org, through the identity map: a team page already holds its
    # team, and a page never needs more than one read per team.
    teams: dict[uuid.UUID, Team] = {}
    for team_id in sorted({key.team_id for key in keys}):
        team = await session.get(Team, team_id)
        if team is not None:
            teams[team_id] = team
    orgs: dict[uuid.UUID, Org] = {}
    for org_id in sorted({team.org_id for team in teams.values()}):
        org = await session.get(Org, org_id)
        if org is not None:
            orgs[org_id] = org
    user_pairs = {(key.team_id, key.user_id) for key in keys if key.owner_kind is KeyOwnerKind.USER}
    member_rows = {}
    if user_pairs:
        member_rows = {
            (row.team_id, row.user_id): row
            for row in await session.scalars(
                select(MemberPolicy).where(
                    MemberPolicy.user_id.in_({user_id for _, user_id in user_pairs}),
                    MemberPolicy.team_id.in_({team_id for team_id, _ in user_pairs}),
                )
            )
        }
    account_ids = {
        key.service_account_id for key in keys if key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT
    }
    accounts = {}
    if account_ids:
        accounts = {
            row.id: row
            for row in await session.scalars(
                select(ServiceAccount).where(ServiceAccount.id.in_(account_ids))
            )
        }
    # Zone limits are read per distinct key zone, in one query each.
    limits_by_key_zone: dict[str, dict] = {}
    for zone in {key.zone for key in keys}:
        limits_by_key_zone[zone] = (
            {
                ("org", org_id): row
                for org_id, row in (
                    (row.org_id, row)
                    for row in await session.scalars(
                        select(ZoneLimit).where(
                            ZoneLimit.zone == zone, ZoneLimit.org_id.in_(list(orgs))
                        )
                    )
                )
            }
            | {
                ("team", row.team_id): row
                for row in await session.scalars(
                    select(ZoneLimit).where(
                        ZoneLimit.zone == zone, ZoneLimit.team_id.in_(list(teams))
                    )
                )
                if row.user_id is None and row.service_account_id is None and row.org_id is None
            }
            | {
                ("member", row.team_id, row.user_id): row
                for row in await session.scalars(
                    select(ZoneLimit).where(
                        ZoneLimit.zone == zone,
                        ZoneLimit.team_id.in_(list(teams)),
                        ZoneLimit.user_id.is_not(None),
                    )
                )
            }
            | {
                ("service_account", row.service_account_id): row
                for row in await session.scalars(
                    select(ZoneLimit).where(
                        ZoneLimit.zone == zone, ZoneLimit.service_account_id.is_not(None)
                    )
                )
            }
        )
    out: dict[uuid.UUID, list[str] | None] = {}
    for key in keys:
        team = teams.get(key.team_id)
        if team is None or team.org_id not in orgs:
            out[key.id] = []
            continue
        zone_limits = limits_by_key_zone.get(key.zone, {})
        team_eff = team_effective(
            org_effective(orgs[team.org_id], zone_limits.get(("org", team.org_id))),
            team,
            zone_limits.get(("team", team.id)),
        )
        defaults = await member_defaults_of(session, key.zone, team.id)
        if key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT:
            account = accounts.get(key.service_account_id)
            owner = owner_effective(
                team_eff,
                defaults,
                policy_of(account, zone_limits.get(("service_account", key.service_account_id)))
                if account is not None
                else None,
                service_account=True,
            )
        else:
            row = member_rows.get((key.team_id, key.user_id))
            owner = owner_effective(
                team_eff,
                defaults,
                policy_of(row, zone_limits.get(("member", key.team_id, key.user_id)))
                if row is not None
                else None,
                service_account=False,
            )
        out[key.id] = None if owner.models is None else sorted(owner.models)
    return out


# The key rules a zone applies, in rank order: installation and org admins outrank a team
# admin, which outranks a member.
_ROLE_RANK = {
    KeyMinCreatorRole.MEMBER: 0,
    KeyMinCreatorRole.TEAM_ADMIN: 1,
    KeyMinCreatorRole.ORG_ADMIN: 2,
}


async def _caller_rank(authz: RequestAuthz, team: Team) -> int:
    """The caller's effective team role: org/installation admin > team admin > member."""
    if await authz.is_admin() or await authz.is_org_admin(team.org_id):
        return 2
    if await authz.allowed(Action.TEAM_MEMBERS_MANAGE, team.id):
        return 1
    return 0


def _lifetime_cap(zone: Zone, team: Team, *, user_key: bool) -> int | None:
    """The effective lifetime cap: min(zone, team) for user keys, zone for SA keys."""
    if user_key:
        return _min_cap(zone.key_max_lifetime_seconds, team.max_member_key_lifetime_seconds)
    return zone.key_max_lifetime_seconds


def _member_cap(zone: Zone, team: Team) -> int | None:
    return _min_cap(zone.key_max_per_member, team.max_keys_per_member)


def _min_cap(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


async def _member_key_count(
    session: AsyncSession, zone: str, team_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.team_id == team_id,
                ApiKey.zone == zone,
                ApiKey.owner_kind == KeyOwnerKind.USER,
                ApiKey.user_id == user_id,
                ApiKey.lifecycle != KeyLifecycle.REVOKED,
            )
        )
        or 0
    )


def _expiry_for_create(
    zone: Zone, team: Team, *, user_key: bool, expires_at: datetime | None
) -> datetime | None:
    """Apply key_expiry_required and the lifetime cap to a new key."""
    cap = _lifetime_cap(zone, team, user_key=user_key)
    if expires_at is None:
        if cap is not None:
            return datetime.now(UTC) + timedelta(seconds=cap)
        if zone.key_expiry_required:
            raise validation_error("expires_at", "keys in this zone must have an expiry")
        return None
    if cap is not None:
        deadline = datetime.now(UTC) + timedelta(seconds=cap)
        if expires_at > deadline:
            raise validation_error("expires_at", f"keys in this zone may not outlive {cap} seconds")
    return expires_at


def _expiry_for_update(
    zone: Zone, team: Team, *, user_key: bool, expires_at: datetime | None
) -> None:
    """An expiry change may not clear or exceed the zone's current rule.

    Existing keys that already violate a tightened rule keep working until they rotate or
    expire; only a write that changes the expiry is judged here.
    """
    cap = _lifetime_cap(zone, team, user_key=user_key)
    if expires_at is None:
        if zone.key_expiry_required:
            raise validation_error("expires_at", "keys in this zone must have an expiry")
        if cap is not None:
            raise validation_error("expires_at", "keys in this zone must keep an expiry")
        return
    if cap is not None and expires_at > datetime.now(UTC) + timedelta(seconds=cap):
        raise validation_error("expires_at", f"keys in this zone may not outlive {cap} seconds")


def _violations(
    key: ApiKey,
    zone: Zone,
    team: Team,
    *,
    role_rank: int | None,
    key_count: int | None,
) -> list[str]:
    """The zone/team rules this existing key no longer satisfies."""
    out: list[str] = []
    user_key = key.owner_kind is KeyOwnerKind.USER
    if key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT and not zone.service_account_keys_allowed:
        out.append("service_account_keys_allowed")
    if zone.key_expiry_required and key.expires_at is None:
        out.append("key_expiry_required")
    cap = _lifetime_cap(zone, team, user_key=user_key)
    if cap is not None and key.expires_at is not None:
        if key.expires_at > datetime.now(UTC) + timedelta(seconds=cap):
            out.append("key_max_lifetime_seconds")
    member_cap = _member_cap(zone, team)
    if user_key and member_cap is not None and key_count is not None and key_count > member_cap:
        out.append("key_max_per_member")
    if role_rank is not None and role_rank < _ROLE_RANK[zone.key_min_creator_role]:
        out.append("key_min_creator_role")
    return out


async def _violations_for(
    session: AsyncSession, keys: list[ApiKey], teams: dict[uuid.UUID, Team]
) -> dict[uuid.UUID, list[str]]:
    """Rule violations of a page of keys, batched by zone and (team, owner)."""
    if not keys:
        return {}
    zones = {
        row.id: row
        for row in await session.scalars(select(Zone).where(Zone.id.in_({k.zone for k in keys})))
    }
    counts: dict[tuple[str, uuid.UUID, uuid.UUID], int] = {}
    # The count only matters when some zone or team caps member keys.
    capped = any(row.key_max_per_member is not None for row in zones.values()) or any(
        team.max_keys_per_member is not None for team in teams.values()
    )
    if capped:
        for zone, team_id, user_id, count in await session.execute(
            select(ApiKey.zone, ApiKey.team_id, ApiKey.user_id, func.count())
            .where(
                ApiKey.owner_kind == KeyOwnerKind.USER,
                ApiKey.lifecycle != KeyLifecycle.REVOKED,
                ApiKey.team_id.in_({k.team_id for k in keys}),
            )
            .group_by(ApiKey.zone, ApiKey.team_id, ApiKey.user_id)
        ):
            counts[(zone, team_id, user_id)] = count
    roles: dict[tuple[uuid.UUID, uuid.UUID], str] = {
        (row.team_id, row.user_id): row.role.value
        for row in await session.scalars(
            select(TeamMember).where(TeamMember.team_id.in_({k.team_id for k in keys}))
        )
    }
    out: dict[uuid.UUID, list[str]] = {}
    for key in keys:
        zone = zones.get(key.zone)
        team = teams.get(key.team_id)
        if zone is None or team is None:
            out[key.id] = []
            continue
        role_rank = None
        if key.owner_kind is KeyOwnerKind.USER:
            role_rank = 1 if roles.get((key.team_id, key.user_id)) == "admin" else 0
        elif key.created_by_user_id is not None:
            role_rank = 1 if roles.get((key.team_id, key.created_by_user_id)) == "admin" else 0
        out[key.id] = _violations(
            key,
            zone,
            team,
            role_rank=role_rank,
            key_count=(
                counts.get((key.zone, key.team_id, key.user_id))
                if key.owner_kind is KeyOwnerKind.USER
                else None
            ),
        )
    return out


async def _apply_key_models(
    session: AsyncSession,
    team: Team,
    key: ApiKey,
    body: KeyCreate | KeyUpdate,
    *,
    create: bool = False,
) -> Policy | None:
    """Validate the key's model list against its owner's effective set (422) and set
    `model_access`. Returns the policy whose grants the caller saves once the key has an
    id, or None when the body does not touch the model list.
    """
    if not body.model_fields_set & _KEY_MODEL_FIELDS:
        return None
    base = None if create else _key_policy(key)
    proposed = _proposed_key_policy(body, base)
    owner = await _owner_effective(session, team, key)
    await validate_key_models(session, owner, proposed, base)
    key.model_access = proposed.model_access
    return proposed


async def _member_key_expiry(
    session: AsyncSession, team: Team, user_id: uuid.UUID, expires_at: datetime | None
) -> datetime | None:
    """Apply the team's per-member key limits to a create. Both are source-only limits."""
    if team.max_keys_per_member is not None:
        owned = await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.team_id == team.id,
                ApiKey.owner_kind == KeyOwnerKind.USER,
                ApiKey.user_id == user_id,
                ApiKey.lifecycle != KeyLifecycle.REVOKED,
            )
        )
        if (owned or 0) >= team.max_keys_per_member:
            raise conflict(
                f"member key limit reached ({team.max_keys_per_member} per member in this team)"
            )
    lifetime = team.max_member_key_lifetime_seconds
    if lifetime is None:
        return expires_at
    deadline = datetime.now(UTC) + timedelta(seconds=lifetime)
    if expires_at is None:
        return deadline
    if expires_at > deadline:
        raise validation_error("expires_at", f"member keys may not outlive {lifetime} seconds")
    return expires_at


def _check_member_expiry(team: Team, expires_at: datetime | None) -> None:
    """A member key cannot be extended past the team's current lifetime cap.

    Create and rotate enforce it too; update is included because an owner could otherwise
    extend or clear the expiry of a key created under a stricter team policy.
    """
    lifetime = team.max_member_key_lifetime_seconds
    if lifetime is None:
        return
    if expires_at is None:
        raise validation_error(
            "expires_at", "member keys must keep an expiry while the team sets a maximum lifetime"
        )
    if expires_at > datetime.now(UTC) + timedelta(seconds=lifetime):
        raise validation_error("expires_at", f"member keys may not outlive {lifetime} seconds")


def _enforce_rotation_lifetime(team: Team, row: ApiKey) -> None:
    """A rotation may not leave a member key valid past the team's current lifetime cap.

    Existing keys created before the cap are handled without a silent policy change: a
    never-expiring key gets an expiry at rotation, and one whose expiry is already too far
    out must be shortened first (422). Team admins are subject to the same rule.
    """
    lifetime = team.max_member_key_lifetime_seconds
    if lifetime is None:
        return
    deadline = datetime.now(UTC) + timedelta(seconds=lifetime)
    if row.expires_at is None:
        row.expires_at = deadline
    elif row.expires_at > deadline:
        raise validation_error(
            "expires_at",
            f"rotation would leave the key valid past the {lifetime}-second member lifetime;"
            " shorten expires_at first",
        )


class KeyOut(KeyOutBase):
    owner_active: bool | None
    # The service account's name for an SA-owned key; None for a user's key.
    service_account_name: str | None
    team_name: str = Field(description="The name of the team the key belongs to.")
    org_id: uuid.UUID | None = Field(description="The id of the team's org.")
    org_name: str | None = Field(description="The name of the team's org.")
    owner_models: list[str] | None = Field(
        default=None,
        description="The owner's effective models, the most the key can be narrowed to;"
        " null = every name in the catalog.",
    )


class KeyCreated(KeyCreatedBase):
    key: KeyOut


def _key_out(
    key: ApiKey,
    active: bool | None,
    account_name: str | None,
    team_name: str,
    org_id: uuid.UUID | None,
    org_name: str | None,
    owner_models: list[str] | None,
    violations: list[str] | None = None,
) -> KeyOut:
    out = KeyOut(
        **KeyOutBase.model_validate(key).model_dump(),
        owner_active=active,
        service_account_name=account_name,
        team_name=team_name,
        org_id=org_id,
        org_name=org_name,
        owner_models=owner_models,
    )
    out.rule_violations = violations or []
    return out


async def _out(session: AsyncSession, key: ApiKey) -> KeyOut:
    active = None
    if key.owner_kind is KeyOwnerKind.USER:
        active = await session.scalar(select(User.is_active).where(User.id == key.user_id))
    ref = (await key_refs(session, [key.id]))[key.id]
    team = await session.get(Team, key.team_id)
    violations = (
        await _violations_for(session, [key], {team.id: team}) if team is not None else {}
    ).get(key.id)
    return _key_out(
        key,
        active,
        ref.service_account_name,
        ref.team_name,
        ref.org_id,
        ref.org_name,
        (await _owner_models(session, [key]))[key.id],
        violations,
    )


async def _page(
    session: AsyncSession, stmt: Select[tuple[ApiKey]], limit: int, cursor: str | None
) -> Page[KeyOut]:
    # The team, org and service-account names are joined into the page query itself, and
    # the team entity comes from it too: the key list stays one data query at every page
    # size, never one query per row or per team.
    stmt = (
        stmt.add_columns(
            User.is_active,
            Team,
            Org.name,
            ServiceAccount.name,
        )
        .outerjoin(User, User.id == ApiKey.user_id)
        .join(Team, Team.id == ApiKey.team_id)
        .outerjoin(Org, Org.id == Team.org_id)
        .outerjoin(ServiceAccount, ServiceAccount.id == ApiKey.service_account_id)
    )
    if cursor is not None:
        stmt = stmt.where(ApiKey.id > parse_uuid_cursor(cursor))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0].id) if len(rows) > limit else None
    page_rows = rows[:limit]
    page_keys = [row[0] for row in page_rows]
    teams = {row[2].id: row[2] for row in page_rows}
    owner_models = await _owner_models(session, page_keys)
    violations = await _violations_for(session, page_keys, teams)
    return Page[KeyOut](
        items=[
            _key_out(
                key,
                active,
                account_name,
                team.name,
                team.org_id,
                org_name,
                owner_models[key.id],
                violations.get(key.id),
            )
            for key, active, team, org_name, account_name in page_rows
        ],
        next_cursor=next_cursor,
    )


async def _may_own_keys(
    session: AsyncSession, authz: RequestAuthz, team_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Whether the user may own keys in the team: a direct member, an installation admin or
    an admin of the team's org (`keys.owners`). Break-glass has no user and owns none.
    """
    (decision,) = await key_owner_decisions(session, authz.authorizer, [(user_id, team_id)])
    if decision is Decision.ERROR:
        raise AuthzUnavailable()
    return decision is Decision.ALLOW


async def _live_team_or_404(session: AsyncSession, team_id: uuid.UUID) -> Team:
    """The key's team, 404 when the team was archived (writes go through here too)."""
    team = await session.get(Team, team_id)
    if team is None or team.deleted_at is not None:
        raise not_found("key not found")
    return team


async def _get_key(session: AsyncSession, authz: RequestAuthz, key_id: uuid.UUID) -> ApiKey:
    row = await session.get(ApiKey, key_id, with_for_update=True)
    if row is None:
        raise not_found("key not found")
    await ensure_key_read(authz, row)
    # Every write to a key of an archived team is refused, even though its row is kept for
    # usage history. All archive paths revoke their keys in the same transaction, but the
    # guard must not depend on that ordering.
    await _live_team_or_404(session, row.team_id)
    return row


async def _require_manage(authz: RequestAuthz, key: ApiKey) -> None:
    """403 unless the caller may change this key (the caller can already read it)."""
    if await authz.allowed(Action.KEY_MANAGE_ANY, key.team_id):
        return
    if key.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT:
        await authz.require(Action.SERVICE_ACCOUNT_MANAGE, key.team_id)
        return
    if key.user_id is not None and key.user_id == authz.user_id:
        await authz.require(Action.KEY_MANAGE_OWN, key.team_id)
        return
    raise forbidden(f"{Action.KEY_MANAGE_ANY.value} not allowed")


@router.get(
    "/teams/{team_id}/keys",
    response_model=Page[KeyOut],
    operation_id="listTeamKeys",
)
async def list_team_keys(
    team_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    owner_kind: KeyOwnerKind | None = None,
    user_id: uuid.UUID | None = None,
    service_account_id: uuid.UUID | None = None,
    lifecycle: KeyLifecycle | None = None,
    zone: Annotated[
        str | None, Query(description="Only keys of this zone; omitted lists every zone.")
    ] = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[KeyOut]:
    """A team admin sees every key of the team and may filter by owner. Any other member sees only
    their own keys, and a filter naming someone else returns nothing.
    """
    team = await get_team_or_404(session, authz, team_id)
    stmt = select(ApiKey).where(ApiKey.team_id == team.id).order_by(ApiKey.id)
    if not await authz.allowed(Action.TEAM_ADMIN_READ, team.id):
        # A plain member sees only their own keys. The owner predicate is ANDed with the
        # caller's filters, so asking for someone else (user_id, service_account_id, or
        # owner_kind) returns nothing instead of widening what they may see.
        stmt = stmt.where(ApiKey.owner_kind == KeyOwnerKind.USER, ApiKey.user_id == authz.user_id)
    if owner_kind is not None:
        stmt = stmt.where(ApiKey.owner_kind == owner_kind)
    if user_id is not None:
        stmt = stmt.where(ApiKey.user_id == user_id)
    if service_account_id is not None:
        stmt = stmt.where(ApiKey.service_account_id == service_account_id)
    if lifecycle is not None:
        stmt = stmt.where(ApiKey.lifecycle == lifecycle)
    if zone is not None:
        stmt = stmt.where(ApiKey.zone == zone)
    return await _page(session, stmt, limit, cursor)


@router.post(
    "/teams/{team_id}/keys",
    response_model=KeyCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createKey",
    responses=RATE_LIMITED,
)
async def create_key(
    team_id: uuid.UUID,
    body: KeyCreate,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> KeyCreated:
    """Create a virtual API key (`lw-…`). The secret is in this response only. Without
    `service_account_id` the key belongs to the caller, who must belong to or administer the
    team, and the team's `max_keys_per_member` and `max_member_key_lifetime_seconds` apply. A
    key for a service account requires a team admin. The key's limits may not exceed its owner's
    effective limits, and with `model_access` `list` its models must be within the owner's.
    Limited to 30 attempts per 60 seconds per caller (429).
    """
    await _rate_limit(request, authz, "create", KEY_CREATE_LIMIT, metrics)
    async with mutation(session):
        await lock_hierarchy(session)
        team = await get_team_or_404(session, authz, team_id)
        zone = await zone_or_404(session, body.zone or settings.zone)
        caller_rank = await _caller_rank(authz, team)
        if caller_rank < _ROLE_RANK[zone.key_min_creator_role]:
            raise validation_error(
                "zone",
                f"creating a key in zone {zone.id!r} requires {zone.key_min_creator_role.value}",
            )
        if body.service_account_id is None:
            await authz.require(Action.KEY_CREATE_OWN, team.id)
            user_id = authz.user_id
            if user_id is None or not await _may_own_keys(session, authz, team.id, user_id):
                raise validation_error("team_id", "not a member or admin of this team")
            owner = {"owner_kind": KeyOwnerKind.USER, "user_id": user_id}
            cap = _member_cap(zone, team)
            if cap is not None:
                owned = await _member_key_count(session, zone.id, team.id, user_id)
                if owned >= cap:
                    raise conflict(
                        f"member key limit reached ({cap} per member in zone {zone.id!r})"
                    )
            expires_at = _expiry_for_create(zone, team, user_key=True, expires_at=body.expires_at)
        else:
            account = await session.get(ServiceAccount, body.service_account_id)
            if account is None or account.team_id != team.id:
                raise not_found("service account not found")
            if account.deactivated_at is not None:
                raise conflict("the service account is deactivated")
            if not zone.service_account_keys_allowed:
                raise validation_error(
                    "zone", f"zone {zone.id!r} does not allow service-account keys"
                )
            await authz.require(Action.SERVICE_ACCOUNT_MANAGE, team.id)
            owner = {
                "owner_kind": KeyOwnerKind.SERVICE_ACCOUNT,
                "service_account_id": account.id,
            }
            expires_at = _expiry_for_create(zone, team, user_key=False, expires_at=body.expires_at)
        new = generate_secret(SecretKind.API_KEY)
        row = ApiKey(
            team_id=team.id,
            zone=zone.id,
            name=body.name,
            key_hash=new.hash,
            display_prefix=new.display_prefix,
            lifecycle=KeyLifecycle.ACTIVE,
            expires_at=expires_at,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            max_parallel=body.max_parallel,
            created_by_user_id=authz.user_id,
            **owner,
        )
        await _check_key_limits(session, team, row, body, create=True)
        models = await _apply_key_models(session, team, row, body, create=True)
        session.add(row)
        await session.flush()
        if models is not None:
            await save_model_grants(session, row, models)
        await session.refresh(row)
        audit.record(
            session,
            action="key.create",
            resource_type="key",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={
                "name": row.name,
                "zone": row.zone,
                "owner_kind": row.owner_kind.value,
                "owner_id": row.owner_id,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "model_access": row.model_access.value,
                "allowed_models": row.allowed_models,
                "allowed_groups": row.allowed_groups,
            },
        )
        await enqueue_recompile(session, settings, "api.key.create", row.zone)
    metrics.key_writes.labels("create", "success").inc()
    return KeyCreated(key=await _out(session, row), secret=new.secret)


@router.get("/keys/{key_id}", response_model=KeyOut, operation_id="getKey")
async def get_key(key_id: uuid.UUID, authz: Authz, session: SessionDep) -> KeyOut:
    """Readable by a team admin and by the key's owner. Another member of the team gets 403, a
    caller outside the team 404. The secret is never returned.
    """
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise not_found("key not found")
    await ensure_key_read(authz, row)
    await _live_team_or_404(session, row.team_id)
    return await _out(session, row)


@router.patch(
    "/keys/{key_id}",
    response_model=KeyOut,
    operation_id="updateKey",
    responses=RATE_LIMITED,
)
async def update_key(
    key_id: uuid.UUID,
    body: KeyUpdate,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> KeyOut:
    """Rename, change limits or the model list, or suspend and resume (`lifecycle`). The owner
    manages their own key and a team admin any key of the team. A changed model list must stay
    within the owner's effective models. An owner may resume only their own `owner` and
    `owner_removed` suspensions; `admin` and `owner_deactivated` need a team admin. Revoked keys
    are immutable and revoking has its own operation. Limited to 30 attempts per 60 seconds per
    caller (429).
    """
    await _rate_limit(request, authz, "update", KEY_UPDATE_LIMIT, metrics)
    async with mutation(session):
        await lock_hierarchy(session)
        ensure_not_null(body, "name")
        row = await _get_key(session, authz, key_id)
        await _require_manage(authz, row)
        if row.lifecycle is KeyLifecycle.REVOKED:
            raise conflict("revoked keys are immutable")
        if "lifecycle" in body.model_fields_set:
            if body.lifecycle is None:
                raise validation_error("lifecycle", "cannot be null")
            if body.lifecycle is KeyLifecycle.REVOKED:
                raise validation_error("lifecycle", "use POST /keys/{id}/revoke")
        if body.name is not None:
            row.name = body.name
        zone_row = await session.get(Zone, row.zone)
        if "expires_at" in body.model_fields_set:
            team = await _live_team_or_404(session, row.team_id)
            if zone_row is None:
                raise conflict(f"zone {row.zone!r} no longer exists")
            _expiry_for_update(
                zone_row,
                team,
                user_key=row.owner_kind is KeyOwnerKind.USER,
                expires_at=body.expires_at,
            )
            row.expires_at = body.expires_at
        limit_fields = ("rpm_limit", "tpm_limit", "max_parallel")
        if body.model_fields_set & set(limit_fields):
            # The owner's effective limits cap the key. A limits-only PATCH never touches
            # `lifecycle` or `suspended_reason`; those stay with their own branches.
            team = await _live_team_or_404(session, row.team_id)
            await _check_key_limits(session, team, row, body)
            for field in limit_fields:
                if field in body.model_fields_set:
                    setattr(row, field, getattr(body, field))
        if body.model_fields_set & _KEY_MODEL_FIELDS:
            # The owner's effective models cap the key's list; an unchanged list is not
            # rechecked after the owner shrank (the compiler intersects it anyway).
            team = await _live_team_or_404(session, row.team_id)
            models = await _apply_key_models(session, team, row, body)
            if models is not None:
                await save_model_grants(session, row, models)
        admin_action = (
            Action.SERVICE_ACCOUNT_MANAGE
            if row.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT
            else Action.KEY_MANAGE_ANY
        )
        if body.lifecycle is KeyLifecycle.ACTIVE:
            if row.owner_kind is KeyOwnerKind.USER and row.user_id is not None:
                owner = await session.get(User, row.user_id)
                if owner is None or not owner.is_active:
                    raise conflict("the key's owner is not active")
                if not await _may_own_keys(session, authz, row.team_id, row.user_id):
                    raise conflict("the key's owner is no longer a member or admin of the team")
            if row.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT:
                account = await session.get(ServiceAccount, row.service_account_id)
                if account is None or account.deactivated_at is not None:
                    raise conflict("the service account is deactivated")
                if zone_row is not None and not zone_row.service_account_keys_allowed:
                    raise conflict(f"zone {row.zone!r} does not allow service-account keys")
            elif zone_row is not None:
                team = await _live_team_or_404(session, row.team_id)
                if zone_row.key_expiry_required and row.expires_at is None:
                    raise conflict(
                        f"zone {row.zone!r} requires an expiry; set expires_at before resuming"
                    )
            owner_resume = (
                row.suspended_reason == "owner_removed"
                and row.owner_kind is KeyOwnerKind.USER
                and row.user_id == authz.user_id
            )
            if row.suspended_reason not in (None, "owner") and not owner_resume:
                await authz.require(admin_action, row.team_id)
            row.lifecycle = KeyLifecycle.ACTIVE
            row.suspended_reason = None
        elif body.lifecycle is KeyLifecycle.SUSPENDED:
            if await authz.allowed(admin_action, row.team_id):
                row.suspended_reason = "admin"
            elif row.suspended_reason in (None, "owner"):
                row.suspended_reason = "owner"
            if row.lifecycle is KeyLifecycle.ACTIVE:
                note_suspended(session, row.suspended_reason or "owner")
            row.lifecycle = KeyLifecycle.SUSPENDED
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="key.update",
            resource_type="key",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={
                "name": row.name,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.key.update", row.zone)
    metrics.count_committed_suspensions(session)
    return await _out(session, row)


@router.post("/keys/{key_id}/revoke", response_model=KeyOut, operation_id="revokeKey")
async def revoke_key(
    key_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyOut:
    """Revoke a key permanently, including a previous secret still inside its rotation grace.
    Allowed for the owner and for a team admin. Revoking an already revoked key succeeds.
    """
    async with mutation(session):
        await lock_hierarchy(session)
        row = await _get_key(session, authz, key_id)
        await _require_manage(authz, row)
        if row.lifecycle is not KeyLifecycle.REVOKED:
            row.lifecycle = KeyLifecycle.REVOKED
            row.suspended_reason = None
            row.revoked_at = datetime.now(UTC)
            # Revoking kills both credentials; never leave a grace hash behind.
            row.previous_key_hash = None
            row.previous_key_expires_at = None
            await session.flush()
            await session.refresh(row)
            audit.record(
                session,
                action="key.revoke",
                resource_type="key",
                resource_id=row.id,
                principal=authz.principal,
                team_id=row.team_id,
                details={"name": row.name},
            )
            await enqueue_recompile(session, settings, "api.key.revoke", row.zone)
    return await _out(session, row)


@router.post(
    "/keys/{key_id}/rotate",
    response_model=KeyCreated,
    operation_id="rotateKey",
    responses=RATE_LIMITED,
)
async def rotate_key(
    key_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
    body: KeyRotate | None = None,
) -> KeyCreated:
    """Replace the secret in place: same key_id, so usage history continues.

    With `grace_seconds` > 0 the replaced hash stays valid until its own expiry. Only one
    previous hash is kept: rotating twice inside the grace drops the older one.
    """
    grace = (body or KeyRotate()).grace_seconds
    await _rate_limit(request, authz, "rotate", KEY_ROTATE_LIMIT, metrics)
    async with mutation(session):
        await lock_hierarchy(session)
        row = await _get_key(session, authz, key_id)
        await _require_manage(authz, row)
        if row.lifecycle is KeyLifecycle.REVOKED:
            raise conflict("revoked keys cannot be rotated")
        team = await _live_team_or_404(session, row.team_id)
        zone_row = await session.get(Zone, row.zone)
        if zone_row is None:
            raise conflict(f"zone {row.zone!r} no longer exists")
        if (
            row.owner_kind is KeyOwnerKind.SERVICE_ACCOUNT
            and not zone_row.service_account_keys_allowed
        ):
            raise conflict(f"zone {row.zone!r} does not allow service-account keys")
        row.expires_at = _expiry_for_create(
            zone_row,
            team,
            user_key=row.owner_kind is KeyOwnerKind.USER,
            expires_at=row.expires_at,
        )
        new = generate_secret(SecretKind.API_KEY)
        old_hash = row.key_hash
        row.key_hash = new.hash
        row.display_prefix = new.display_prefix
        if grace:
            previous_expires = datetime.now(UTC) + timedelta(seconds=grace)
            if row.expires_at is not None:
                previous_expires = min(previous_expires, row.expires_at)
            row.previous_key_hash = old_hash
            row.previous_key_expires_at = previous_expires
        else:
            row.previous_key_hash = None
            row.previous_key_expires_at = None
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="key.rotate",
            resource_type="key",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={
                "name": row.name,
                "zone": row.zone,
                "grace_seconds": grace,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "previous_key_expires_at": (
                    row.previous_key_expires_at.isoformat() if row.previous_key_expires_at else None
                ),
            },
        )
        await enqueue_recompile(session, settings, "api.key.rotate", row.zone)
    metrics.key_writes.labels("rotate", "success").inc()
    return KeyCreated(key=await _out(session, row), secret=new.secret)


@router.get("/users/me/keys", response_model=Page[KeyOut], operation_id="listOwnKeys")
async def list_own_keys(
    authz: Authz,
    session: SessionDep,
    include_service_accounts: Annotated[
        bool,
        Query(
            description=(
                "Also list the service-account keys of every live team the caller administers,"
                " including by org or installation inheritance."
            )
        ),
    ] = False,
    zone: Annotated[
        str | None, Query(description="Only keys of this zone; omitted lists every zone.")
    ] = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[KeyOut]:
    """The caller's own keys across every team. With `include_service_accounts` it also lists
    the service-account keys of the live teams the caller administers (a team admin, or an org
    or installation admin by inheritance); a plain member still sees only their own keys.
    Empty for a break-glass session, which is not a user.
    """
    if authz.user_id is None:
        return Page[KeyOut](items=[], next_cursor=None)
    own = and_(ApiKey.owner_kind == KeyOwnerKind.USER, ApiKey.user_id == authz.user_id)
    if not include_service_accounts:
        stmt = select(ApiKey).where(own)
    else:
        live_team_ids = list(
            await session.scalars(select(Team.id).where(Team.deleted_at.is_(None)))
        )
        # Same team set as `/users/me`: direct team admins plus org and installation
        # inheritance. Restricting to live teams also hides an archived team's SA keys.
        administered = await authz.admin_teams(live_team_ids)
        team_ids = live_team_ids if administered is None else list(administered)
        stmt = select(ApiKey).where(
            or_(
                own,
                and_(
                    ApiKey.owner_kind == KeyOwnerKind.SERVICE_ACCOUNT,
                    ApiKey.team_id.in_(team_ids),
                ),
            )
        )
    if zone is not None:
        stmt = stmt.where(ApiKey.zone == zone)
    return await _page(session, stmt.order_by(ApiKey.id), limit, cursor)

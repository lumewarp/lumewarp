"""Virtual API keys: create, inspect, update, revoke and rotate.

The raw `lw-...` secret is returned exactly once from `createKey` / `rotateKey`; only its
sha256 and a short display prefix are stored. A key is owned by a team member or by a
service account and carries no policy of its own (the owner's policy applies).
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil

from fastapi import APIRouter, Request, status
from sqlalchemy import Select, func, select
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
    lock_scope,
    mutation,
    parse_uuid_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    KeyCreate,
    KeyRotate,
    KeyUpdate,
    Page,
)
from lumewarp_control_plane.api.schemas import KeyCreated as KeyCreatedBase
from lumewarp_control_plane.api.schemas import KeyOut as KeyOutBase
from lumewarp_control_plane.authz import (
    Action,
    Authz,
    AuthzUnavailable,
    Decision,
    Relation,
    RequestAuthz,
    team_resource,
)
from lumewarp_control_plane.errors import ApiError, forbidden, not_found
from lumewarp_control_plane.metrics import note_suspended
from lumewarp_control_plane.models import (
    ApiKey,
    KeyLifecycle,
    KeyOwnerKind,
    ServiceAccount,
    Team,
    TeamMember,
    User,
)
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
MAX_LIMITER_ENTRIES = 10_000


@dataclass
class KeyWriteLimiter:
    """Per-scope fixed-window counters with amortized, incremental expiry.

    `entries` is the live table and `expiries` is ordered by expiry (every counter keeps its
    original deadline), so a call only pops the expired front instead of rebuilding the
    table. At most two entries exist per principal (create and rotate), so the capacity
    bounds principals. Normal hits and refusals are O(1). A full table refuses a new scope
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


class KeyCreated(KeyCreatedBase):
    key: KeyOut


async def _out(session: AsyncSession, key: ApiKey) -> KeyOut:
    active = (
        await session.scalar(select(User.is_active).where(User.id == key.user_id))
        if key.owner_kind is KeyOwnerKind.USER
        else None
    )
    return KeyOut(**KeyOutBase.model_validate(key).model_dump(), owner_active=active)


async def _page(
    session: AsyncSession, stmt: Select[tuple[ApiKey]], limit: int, cursor: str | None
) -> Page[KeyOut]:
    stmt = stmt.add_columns(User.is_active).outerjoin(User, User.id == ApiKey.user_id)
    if cursor is not None:
        stmt = stmt.where(ApiKey.id > parse_uuid_cursor(cursor))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0].id) if len(rows) > limit else None
    return Page[KeyOut](
        items=[
            KeyOut(**KeyOutBase.model_validate(key).model_dump(), owner_active=active)
            for key, active in rows[:limit]
        ],
        next_cursor=next_cursor,
    )


async def _is_member(
    session: AsyncSession, authz: RequestAuthz, team_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Whether the user holds a membership of the team itself. `authz.has` is not enough:
    it is true for every installation admin, who may not be a member and so cannot own keys.
    """
    if authz.authorizer.manages_grants:
        return await session.get(TeamMember, (team_id, user_id)) is not None
    decision = await authz.authorizer.check(str(user_id), Relation.MEMBER, team_resource(team_id))
    if decision is Decision.ERROR:
        raise AuthzUnavailable()
    return decision is Decision.ALLOW


async def _get_key(session: AsyncSession, authz: RequestAuthz, key_id: uuid.UUID) -> ApiKey:
    row = await session.get(ApiKey, key_id, with_for_update=True)
    if row is None:
        raise not_found("key not found")
    await ensure_key_read(authz, row)
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
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[KeyOut]:
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
    return await _page(session, stmt, limit, cursor)


@router.post(
    "/teams/{team_id}/keys",
    response_model=KeyCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createKey",
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
    await _rate_limit(request, authz, "create", KEY_CREATE_LIMIT, metrics)
    async with mutation(session):
        await lock_scope(session, settings)
        team = await get_team_or_404(session, authz, team_id)
        if body.service_account_id is None:
            await authz.require(Action.KEY_CREATE_OWN, team.id)
            user_id = authz.user_id
            if user_id is None or not await _is_member(session, authz, team.id, user_id):
                raise validation_error("team_id", "not a member of this team")
            owner = {"owner_kind": KeyOwnerKind.USER, "user_id": user_id}
            expires_at = await _member_key_expiry(session, team, user_id, body.expires_at)
        else:
            account = await session.get(ServiceAccount, body.service_account_id)
            if account is None or account.team_id != team.id:
                raise not_found("service account not found")
            await authz.require(Action.SERVICE_ACCOUNT_MANAGE, team.id)
            owner = {
                "owner_kind": KeyOwnerKind.SERVICE_ACCOUNT,
                "service_account_id": account.id,
            }
            # Member limits do not apply to service accounts (team-admin managed).
            expires_at = body.expires_at
        new = generate_secret(SecretKind.API_KEY)
        row = ApiKey(
            team_id=team.id,
            name=body.name,
            key_hash=new.hash,
            display_prefix=new.display_prefix,
            lifecycle=KeyLifecycle.ACTIVE,
            expires_at=expires_at,
            created_by_user_id=authz.user_id,
            **owner,
        )
        session.add(row)
        await session.flush()
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
                "owner_kind": row.owner_kind.value,
                "owner_id": row.owner_id,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            },
        )
        await enqueue_recompile(session, settings, "api.key.create")
    metrics.key_writes.labels("create", "success").inc()
    return KeyCreated(key=await _out(session, row), secret=new.secret)


@router.get("/keys/{key_id}", response_model=KeyOut, operation_id="getKey")
async def get_key(key_id: uuid.UUID, authz: Authz, session: SessionDep) -> KeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise not_found("key not found")
    await ensure_key_read(authz, row)
    return await _out(session, row)


@router.patch("/keys/{key_id}", response_model=KeyOut, operation_id="updateKey")
async def update_key(
    key_id: uuid.UUID,
    body: KeyUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> KeyOut:
    async with mutation(session):
        await lock_scope(session, settings)
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
        if "expires_at" in body.model_fields_set:
            if row.owner_kind is KeyOwnerKind.USER:
                team = await session.get(Team, row.team_id)
                if team is None:
                    raise not_found("team not found")
                _check_member_expiry(team, body.expires_at)
            row.expires_at = body.expires_at
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
                if not await _is_member(session, authz, row.team_id, row.user_id):
                    raise conflict("the key's owner is no longer a member of the team")
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
        await enqueue_recompile(session, settings, "api.key.update")
    metrics.count_committed_suspensions(session)
    return await _out(session, row)


@router.post("/keys/{key_id}/revoke", response_model=KeyOut, operation_id="revokeKey")
async def revoke_key(
    key_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyOut:
    async with mutation(session):
        await lock_scope(session, settings)
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
            await enqueue_recompile(session, settings, "api.key.revoke")
    return await _out(session, row)


@router.post("/keys/{key_id}/rotate", response_model=KeyCreated, operation_id="rotateKey")
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
        await lock_scope(session, settings)
        row = await _get_key(session, authz, key_id)
        await _require_manage(authz, row)
        if row.lifecycle is KeyLifecycle.REVOKED:
            raise conflict("revoked keys cannot be rotated")
        if row.owner_kind is KeyOwnerKind.USER:
            team = await session.get(Team, row.team_id)
            if team is None:
                raise not_found("team not found")
            _enforce_rotation_lifetime(team, row)
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
                "grace_seconds": grace,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "previous_key_expires_at": (
                    row.previous_key_expires_at.isoformat() if row.previous_key_expires_at else None
                ),
            },
        )
        await enqueue_recompile(session, settings, "api.key.rotate")
    metrics.key_writes.labels("rotate", "success").inc()
    return KeyCreated(key=await _out(session, row), secret=new.secret)


@router.get("/users/me/keys", response_model=Page[KeyOut], operation_id="listOwnKeys")
async def list_own_keys(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[KeyOut]:
    if authz.user_id is None:
        return Page[KeyOut](items=[], next_cursor=None)
    stmt = (
        select(ApiKey)
        .where(ApiKey.owner_kind == KeyOwnerKind.USER, ApiKey.user_id == authz.user_id)
        .order_by(ApiKey.id)
    )
    return await _page(session, stmt, limit, cursor)

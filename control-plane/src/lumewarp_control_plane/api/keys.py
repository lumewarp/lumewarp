"""Virtual API keys: create, inspect, update, revoke and rotate.

The raw `lw-...` secret is returned exactly once from `createKey` / `rotateKey`; only its
sha256 and a short display prefix are stored. A key is owned by a team member or by a
service account and carries no policy of its own (the owner's policy applies).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_key_read,
    ensure_not_null,
    get_team_or_404,
    lock_scope,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    KeyCreate,
    KeyCreated,
    KeyOut,
    KeyUpdate,
    Page,
)
from lumewarp_control_plane.authz import (
    Action,
    Authz,
    AuthzUnavailable,
    Decision,
    Relation,
    RequestAuthz,
    team_resource,
)
from lumewarp_control_plane.errors import forbidden, not_found
from lumewarp_control_plane.models import (
    ApiKey,
    KeyLifecycle,
    KeyOwnerKind,
    ServiceAccount,
    TeamMember,
    User,
)
from lumewarp_control_plane.security import SecretKind, generate_secret

router = APIRouter(tags=["keys"])


async def _is_member(
    session: AsyncSession, authz: RequestAuthz, team_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Whether the user holds a membership of the team itself. `authz.has` is not enough:
    it is true for every superuser, who may not be a member and so cannot own keys.
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
    if owner_kind is not None:
        stmt = stmt.where(ApiKey.owner_kind == owner_kind)
    if user_id is not None:
        stmt = stmt.where(ApiKey.user_id == user_id)
    if service_account_id is not None:
        stmt = stmt.where(ApiKey.service_account_id == service_account_id)
    if lifecycle is not None:
        stmt = stmt.where(ApiKey.lifecycle == lifecycle)
    rows, next_cursor = await uuid_page(session, stmt, ApiKey.id, limit, cursor)
    return Page[KeyOut](items=[KeyOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/teams/{team_id}/keys",
    response_model=KeyCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createKey",
)
async def create_key(
    team_id: uuid.UUID,
    body: KeyCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyCreated:
    async with mutation(session):
        await lock_scope(session, settings)
        team = await get_team_or_404(session, authz, team_id)
        if body.service_account_id is None:
            await authz.require(Action.KEY_CREATE_OWN, team.id)
            user_id = authz.user_id
            if user_id is None or not await _is_member(session, authz, team.id, user_id):
                raise validation_error("team_id", "not a member of this team")
            owner = {"owner_kind": KeyOwnerKind.USER, "user_id": user_id}
        else:
            account = await session.get(ServiceAccount, body.service_account_id)
            if account is None or account.team_id != team.id:
                raise not_found("service account not found")
            await authz.require(Action.SERVICE_ACCOUNT_MANAGE, team.id)
            owner = {
                "owner_kind": KeyOwnerKind.SERVICE_ACCOUNT,
                "service_account_id": account.id,
            }
        new = generate_secret(SecretKind.API_KEY)
        row = ApiKey(
            team_id=team.id,
            name=body.name,
            key_hash=new.hash,
            display_prefix=new.display_prefix,
            lifecycle=KeyLifecycle.ACTIVE,
            expires_at=body.expires_at,
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
            },
        )
        await enqueue_recompile(session, settings, "api.key.create")
    return KeyCreated(key=KeyOut.model_validate(row), secret=new.secret)


@router.get("/keys/{key_id}", response_model=KeyOut, operation_id="getKey")
async def get_key(key_id: uuid.UUID, authz: Authz, session: SessionDep) -> KeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise not_found("key not found")
    await ensure_key_read(authz, row)
    return KeyOut.model_validate(row)


@router.patch("/keys/{key_id}", response_model=KeyOut, operation_id="updateKey")
async def update_key(
    key_id: uuid.UUID,
    body: KeyUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
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
            if row.suspended_reason not in (None, "owner"):
                await authz.require(admin_action, row.team_id)
            row.lifecycle = KeyLifecycle.ACTIVE
            row.suspended_reason = None
        elif body.lifecycle is KeyLifecycle.SUSPENDED:
            if await authz.allowed(admin_action, row.team_id):
                row.suspended_reason = "admin"
            elif row.suspended_reason in (None, "owner"):
                row.suspended_reason = "owner"
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
    return KeyOut.model_validate(row)


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
    return KeyOut.model_validate(row)


@router.post("/keys/{key_id}/rotate", response_model=KeyCreated, operation_id="rotateKey")
async def rotate_key(
    key_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyCreated:
    """Replace the secret in place: same key_id, so usage history continues."""
    async with mutation(session):
        await lock_scope(session, settings)
        row = await _get_key(session, authz, key_id)
        await _require_manage(authz, row)
        if row.lifecycle is KeyLifecycle.REVOKED:
            raise conflict("revoked keys cannot be rotated")
        new = generate_secret(SecretKind.API_KEY)
        row.key_hash = new.hash
        row.display_prefix = new.display_prefix
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="key.rotate",
            resource_type="key",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.key.rotate")
    return KeyCreated(key=KeyOut.model_validate(row), secret=new.secret)


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
    rows, next_cursor = await uuid_page(session, stmt, ApiKey.id, limit, cursor)
    return Page[KeyOut](items=[KeyOut.model_validate(row) for row in rows], next_cursor=next_cursor)

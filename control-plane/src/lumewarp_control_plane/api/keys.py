"""Virtual API keys: create, inspect, update and revoke.

The raw `lw-...` secret is returned exactly once from `createKey`; only its sha256 and a
short display prefix are stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select

from lumewarp_control_plane import audit, rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_key_read,
    ensure_not_null,
    ensure_parent_matches,
    ensure_project_read,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    check_narrowing,
    policy_from_fields,
    policy_of,
    project_effective,
)
from lumewarp_control_plane.api.schemas import (
    KeyCreate,
    KeyCreated,
    KeyOut,
    KeyUpdate,
    Page,
)
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, ModelAccess, Project
from lumewarp_control_plane.security import CurrentPrincipal, SecretKind, generate_secret

router = APIRouter(tags=["keys"])


@router.get(
    "/projects/{project_id}/keys",
    response_model=Page[KeyOut],
    operation_id="listKeys",
)
async def list_keys(
    project_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[KeyOut]:
    project = await session.get(Project, project_id)
    if project is None:
        raise not_found("project not found")
    ensure_project_read(principal, project)
    stmt = select(ApiKey).where(ApiKey.project_id == project.id).order_by(ApiKey.id)
    rows, next_cursor = await uuid_page(session, stmt, ApiKey.id, limit, cursor)
    return Page[KeyOut](items=[KeyOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/projects/{project_id}/keys",
    response_model=KeyCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createKey",
)
async def create_key(
    project_id: uuid.UUID,
    body: KeyCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyCreated:
    async with mutation(session):
        project = await session.get(Project, project_id)
        if project is None:
            raise not_found("project not found")
        ensure_project_read(principal, project)
        rbac.require(
            principal,
            rbac.Action.KEY_CREATE,
            rbac.ResourceScope(project.org_id, project.team_id),
        )
        ensure_parent_matches(body.project_id, project.id, "project_id")
        ensure_parent_matches(body.team_id, project.team_id, "team_id")
        ensure_parent_matches(body.org_id, project.org_id, "org_id")
        policy = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        check_narrowing(await project_effective(session, project), policy)
        new = generate_secret(SecretKind.API_KEY)
        row = ApiKey(
            org_id=project.org_id,
            team_id=project.team_id,
            project_id=project.id,
            name=body.name,
            key_hash=new.hash,
            display_prefix=new.display_prefix,
            lifecycle=KeyLifecycle.ACTIVE,
            expires_at=body.expires_at,
            created_by_user_id=principal.user_id,
        )
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="key.create",
            resource_type="key",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"name": row.name, "project_id": str(row.project_id)},
        )
        await enqueue_recompile(session, settings, "api.key.create")
    return KeyCreated(key=KeyOut.model_validate(row), secret=new.secret)


@router.get("/keys/{key_id}", response_model=KeyOut, operation_id="getKey")
async def get_key(key_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep) -> KeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise not_found("key not found")
    ensure_key_read(principal, row)
    return KeyOut.model_validate(row)


@router.patch("/keys/{key_id}", response_model=KeyOut, operation_id="updateKey")
async def update_key(
    key_id: uuid.UUID,
    body: KeyUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyOut:
    async with mutation(session):
        ensure_not_null(body, "name")
        row = await session.get(ApiKey, key_id)
        if row is None:
            raise not_found("key not found")
        ensure_key_read(principal, row)
        rbac.require(principal, rbac.Action.KEY_UPDATE, rbac.ResourceScope(row.org_id, row.team_id))
        if row.lifecycle is KeyLifecycle.REVOKED:
            raise conflict("revoked keys are immutable")
        ensure_parent_matches(body.project_id, row.project_id, "project_id")
        ensure_parent_matches(body.team_id, row.team_id, "team_id")
        ensure_parent_matches(body.org_id, row.org_id, "org_id")
        if "lifecycle" in body.model_fields_set:
            if body.lifecycle is None:
                raise validation_error("lifecycle", "cannot be null")
            if body.lifecycle is KeyLifecycle.REVOKED:
                raise validation_error("lifecycle", "use POST /keys/{id}/revoke")
        project = await session.get(Project, row.project_id)
        if project is None:
            raise not_found("key not found")
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        check_narrowing(await project_effective(session, project), proposed)
        changed = proposed != base
        if changed:
            apply_policy(row, proposed)
        if body.name is not None:
            row.name = body.name
        if "expires_at" in body.model_fields_set:
            row.expires_at = body.expires_at
        if body.lifecycle is not None:
            row.lifecycle = body.lifecycle
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="key.update",
            resource_type="key",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.key.update")
    return KeyOut.model_validate(row)


@router.post("/keys/{key_id}/revoke", response_model=KeyOut, operation_id="revokeKey")
async def revoke_key(
    key_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> KeyOut:
    async with mutation(session):
        row = await session.get(ApiKey, key_id)
        if row is None:
            raise not_found("key not found")
        ensure_key_read(principal, row)
        rbac.require(principal, rbac.Action.KEY_REVOKE, rbac.ResourceScope(row.org_id, row.team_id))
        if row.lifecycle is not KeyLifecycle.REVOKED:
            row.lifecycle = KeyLifecycle.REVOKED
            row.revoked_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            audit.record(
                session,
                action="key.revoke",
                resource_type="key",
                resource_id=row.id,
                principal=principal,
                org_id=row.org_id,
                team_id=row.team_id,
                details={"name": row.name},
            )
            await enqueue_recompile(session, settings, "api.key.revoke")
    return KeyOut.model_validate(row)

"""Model groups: named sets of catalog names granted as one unit.

A group's members are models, aliases or routers (never another group); its name is not
a catalog name and is never callable. Any level (org, team, member, service account, key)
may grant a group in `allowed_groups`; the grant stands for the group's current members.

Changing the members does not re-validate the holders: the compiler intersects down the
chain, so a member a parent does not allow is dropped there, exactly as when a parent
narrows. Deleting a group removes its grants (audited one by one). Groups are not part of a
rollback's catalog state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import APIRouter, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    lock_scope,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    ModelGroupCreate,
    ModelGroupMemberOut,
    ModelGroupOut,
    ModelGroupUpdate,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.model_types import catalog_types
from lumewarp_control_plane.models import (
    CatalogName,
    ModelGroup,
    ModelGroupMember,
    PolicyGroupGrant,
)
from lumewarp_control_plane.security import Principal

router = APIRouter(tags=["model-groups"])


async def _member_ids(session: AsyncSession, names: list[str]) -> list[uuid.UUID]:
    """Catalog ids of the named members, in order; 422 listing the unknown names."""
    if not names:
        return []
    rows = await session.execute(
        select(CatalogName.name, CatalogName.id).where(CatalogName.name.in_(names))
    )
    found = {name: name_id for name, name_id in rows}
    missing = [name for name in names if name not in found]
    if missing:
        raise validation_error("members", f"unknown names: {', '.join(missing)}")
    return [found[name] for name in names]


async def _name_free(session: AsyncSession, name: str, current: uuid.UUID | None = None) -> None:
    existing = await session.scalar(select(ModelGroup.id).where(ModelGroup.name == name))
    if existing is not None and existing != current:
        raise conflict(f"a model group named {name!r} already exists")


async def _outs(session: AsyncSession, rows: list[ModelGroup]) -> list[ModelGroupOut]:
    """One query for every member and one for the grant counts, whatever the page size."""
    ids = [row.id for row in rows]
    members: dict[uuid.UUID, list[ModelGroupMemberOut]] = {group_id: [] for group_id in ids}
    grants: dict[uuid.UUID, int] = {}
    if ids:
        types = await catalog_types(session)
        for group_id, name_id, name, kind in await session.execute(
            select(ModelGroupMember.group_id, CatalogName.id, CatalogName.name, CatalogName.kind)
            .join(CatalogName, CatalogName.id == ModelGroupMember.name_id)
            .where(ModelGroupMember.group_id.in_(ids))
            .order_by(CatalogName.name)
        ):
            members[group_id].append(
                ModelGroupMemberOut(id=name_id, name=name, kind=kind, model_type=types.get(name_id))
            )
        grants = dict(
            (
                await session.execute(
                    select(PolicyGroupGrant.group_id, func.count())
                    .where(PolicyGroupGrant.group_id.in_(ids))
                    .group_by(PolicyGroupGrant.group_id)
                )
            ).all()
        )
    return [
        ModelGroupOut(
            id=row.id,
            name=row.name,
            description=row.description,
            members=members[row.id],
            grants=grants.get(row.id, 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def _out(session: AsyncSession, row: ModelGroup) -> ModelGroupOut:
    return (await _outs(session, [row]))[0]


async def _replace_members(
    session: AsyncSession, group_id: uuid.UUID, ids: list[uuid.UUID]
) -> None:
    await session.execute(delete(ModelGroupMember).where(ModelGroupMember.group_id == group_id))
    session.add_all(ModelGroupMember(group_id=group_id, name_id=name_id) for name_id in ids)
    await session.flush()


async def audit_group_grant_removal(
    session: AsyncSession,
    groups: Iterable[tuple[uuid.UUID, str]],
    *,
    principal: Principal,
) -> int:
    """Audit every grant of these (id, name) groups; the caller deletes the groups, which
    cascades to the grants. Mirrors `catalog.audit_grant_removal`.
    """
    count = 0
    for group_id, name in groups:
        rows = await session.execute(
            select(
                PolicyGroupGrant.id,
                PolicyGroupGrant.org_id,
                PolicyGroupGrant.team_id,
                PolicyGroupGrant.user_id,
                PolicyGroupGrant.service_account_id,
                PolicyGroupGrant.key_id,
            )
            .where(PolicyGroupGrant.group_id == group_id)
            .order_by(PolicyGroupGrant.id)
        )
        for grant in rows:
            count += 1
            audit.record(
                session,
                action="grant.delete",
                resource_type="grant",
                resource_id=grant.id,
                team_id=grant.team_id,
                principal=principal,
                details={
                    "group": name,
                    "group_id": group_id,
                    "org_id": grant.org_id,
                    "user_id": grant.user_id,
                    "service_account_id": grant.service_account_id,
                    "key_id": grant.key_id,
                },
            )
    return count


@router.get("/model-groups", response_model=Page[ModelGroupOut], operation_id="listModelGroups")
async def list_model_groups(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ModelGroupOut]:
    """Readable by every signed-in caller, like the catalog names, for the allowlist pickers."""
    await authz.require(Action.CATALOG_READ)
    stmt = select(ModelGroup).order_by(ModelGroup.id)
    rows, next_cursor = await uuid_page(session, stmt, ModelGroup.id, limit, cursor)
    return Page[ModelGroupOut](items=await _outs(session, rows), next_cursor=next_cursor)


@router.post(
    "/model-groups",
    response_model=ModelGroupOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createModelGroup",
)
async def create_model_group(
    body: ModelGroupCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelGroupOut:
    """Create a group of models, aliases and routers. Requires an installation admin. The name is
    unique among groups (409) and is not a callable name.
    """
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        await _name_free(session, body.name)
        ids = await _member_ids(session, body.members)
        row = ModelGroup(name=body.name, description=body.description)
        session.add(row)
        await session.flush()
        await _replace_members(session, row.id, ids)
        await session.refresh(row)
        audit.record(
            session,
            action="model_group.create",
            resource_type="model_group",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "members": body.members},
        )
        # Nothing holds a new group yet; recompiling keeps every write path uniform.
        await enqueue_recompile(session, settings, "api.model_group.create")
    return await _out(session, row)


@router.get("/model-groups/{group_id}", response_model=ModelGroupOut, operation_id="getModelGroup")
async def get_model_group(group_id: uuid.UUID, authz: Authz, session: SessionDep) -> ModelGroupOut:
    """Readable by every signed-in caller."""
    await authz.require(Action.CATALOG_READ)
    row = await session.get(ModelGroup, group_id)
    if row is None:
        raise not_found("model group not found")
    return await _out(session, row)


@router.patch(
    "/model-groups/{group_id}", response_model=ModelGroupOut, operation_id="updateModelGroup"
)
async def update_model_group(
    group_id: uuid.UUID,
    body: ModelGroupUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelGroupOut:
    """Rename, describe or replace the members. Requires an installation admin. Every holder of
    the group gets the new members at the next compile; the holders are not re-validated, and a
    member a holder's parent does not allow is dropped from that holder's effective set.
    """
    await authz.require(Action.ROUTING_MANAGE)
    for name in ("name", "description", "members"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(ModelGroup, group_id, with_for_update=True)
        if row is None:
            raise not_found("model group not found")
        if body.name is not None and body.name != row.name:
            await _name_free(session, body.name, row.id)
            row.name = body.name
        if body.description is not None:
            row.description = body.description
        if body.members is not None:
            await _replace_members(session, row.id, await _member_ids(session, body.members))
        row.updated_at = func.now()
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="model_group.update",
            resource_type="model_group",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": body.model_dump(exclude_unset=True, mode="json")},
        )
        await enqueue_recompile(session, settings, "api.model_group.update")
    return await _out(session, row)


@router.delete(
    "/model-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteModelGroup",
)
async def delete_model_group(
    group_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Requires an installation admin. Every grant of the group is removed with it (each one
    audited), so its holders lose its members at the next compile.
    """
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(ModelGroup, group_id, with_for_update=True)
        if row is None:
            raise not_found("model group not found")
        removed = await audit_group_grant_removal(
            session, [(row.id, row.name)], principal=authz.principal
        )
        audit.record(
            session,
            action="model_group.delete",
            resource_type="model_group",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "grants_removed": removed},
        )
        await session.delete(row)
        await session.flush()
        await enqueue_recompile(session, settings, "api.model_group.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Service accounts: non-human key owners that belong to a team."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_not_null,
    get_team_or_404,
    lock_scope,
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    policy_from_fields,
    policy_of,
    save_grants,
    validate_owner_policy,
)
from lumewarp_control_plane.api.schemas import (
    Page,
    ServiceAccountCreate,
    ServiceAccountOut,
    ServiceAccountUpdate,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ApiKey, ModelAccess, ServiceAccount, Team

router = APIRouter(tags=["service-accounts"])


async def _account_or_404(
    session: SessionDep, authz: Authz, account_id: uuid.UUID
) -> tuple[ServiceAccount, Team]:
    row = await session.get(ServiceAccount, account_id)
    if row is None:
        raise not_found("service account not found")
    if not await authz.allowed(Action.TEAM_READ, row.team_id):
        raise not_found("service account not found")
    await authz.require(Action.TEAM_ADMIN_READ, row.team_id)
    team = await session.get(Team, row.team_id)
    if team is None or team.deleted_at is not None:
        # An archived team is invisible: its service accounts are deactivated with it and
        # no write may target them any more.
        raise not_found("service account not found")
    return row, team


@router.get(
    "/teams/{team_id}/service-accounts",
    response_model=Page[ServiceAccountOut],
    operation_id="listServiceAccounts",
)
async def list_service_accounts(
    team_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ServiceAccountOut]:
    """Requires a team admin."""
    team = await get_team_or_404(session, authz, team_id)
    await authz.require(Action.TEAM_ADMIN_READ, team.id)
    stmt = (
        select(ServiceAccount).where(ServiceAccount.team_id == team.id).order_by(ServiceAccount.id)
    )
    rows, next_cursor = await uuid_page(session, stmt, ServiceAccount.id, limit, cursor)
    return Page[ServiceAccountOut](
        items=[ServiceAccountOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/teams/{team_id}/service-accounts",
    response_model=ServiceAccountOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createServiceAccount",
)
async def create_service_account(
    team_id: uuid.UUID,
    body: ServiceAccountCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ServiceAccountOut:
    """Create a service account, a non-human key owner of the team. Requires a team admin."""
    async with mutation(session):
        await lock_scope(session, settings)
        team = await get_team_or_404(session, authz, team_id)
        await authz.require(Action.SERVICE_ACCOUNT_MANAGE, team.id)
        policy = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        await validate_owner_policy(session, team, policy)
        row = ServiceAccount(
            team_id=team.id,
            name=body.name,
            description=body.description,
            created_by_user_id=authz.principal.user_id,
        )
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await save_grants(session, row, policy)
        await session.refresh(row)
        audit.record(
            session,
            action="service_account.create",
            resource_type="service_account",
            resource_id=row.id,
            principal=authz.principal,
            team_id=team.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.service_account.create")
    return ServiceAccountOut.model_validate(row)


@router.get(
    "/service-accounts/{account_id}",
    response_model=ServiceAccountOut,
    operation_id="getServiceAccount",
)
async def get_service_account(
    account_id: uuid.UUID, authz: Authz, session: SessionDep
) -> ServiceAccountOut:
    """Requires a team admin; a caller outside the team gets 404."""
    row, _ = await _account_or_404(session, authz, account_id)
    return ServiceAccountOut.model_validate(row)


@router.patch(
    "/service-accounts/{account_id}",
    response_model=ServiceAccountOut,
    operation_id="updateServiceAccount",
)
async def update_service_account(
    account_id: uuid.UUID,
    body: ServiceAccountUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ServiceAccountOut:
    """Rename, change the policy, or deactivate and reactivate. Requires a team admin. Keys of a
    deactivated service account stop authenticating.
    """
    async with mutation(session):
        await lock_scope(session, settings)
        ensure_not_null(body, "name", "description")
        row, team = await _account_or_404(session, authz, account_id)
        await authz.require(Action.SERVICE_ACCOUNT_MANAGE, row.team_id)
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        await validate_owner_policy(session, team, proposed, base)
        changed = proposed != base
        if changed:
            apply_policy(row, proposed)
            await save_grants(session, row, proposed)
        if body.name is not None:
            row.name = body.name
        if body.description is not None:
            row.description = body.description
        if body.is_active is not None:
            if body.is_active:
                row.deactivated_at = None
            elif row.deactivated_at is None:
                row.deactivated_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="service_account.update",
            resource_type="service_account",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.service_account.update")
    return ServiceAccountOut.model_validate(row)


@router.delete(
    "/service-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteServiceAccount",
)
async def delete_service_account(
    account_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Requires a team admin. Answers 409 while the account has keys, revoked ones included,
    because usage history refers to them; deactivate it instead.
    """
    async with mutation(session):
        await lock_scope(session, settings)
        row, _ = await _account_or_404(session, authz, account_id)
        await authz.require(Action.SERVICE_ACCOUNT_MANAGE, row.team_id)
        key = await session.scalar(
            select(ApiKey.id).where(ApiKey.service_account_id == row.id).limit(1)
        )
        if key is not None:
            raise conflict("service account has keys (kept for usage history)")
        await session.delete(row)
        audit.record(
            session,
            action="service_account.delete",
            resource_type="service_account",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.team_id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.service_account.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

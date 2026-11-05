"""Installation roles: the proxy_admin and admins, stored in Postgres.

Any installation admin (either role, or break-glass) can assign and remove `admin` rows.
The proxy_admin is only moved through `transfer`; assign/remove never demote it. Transfer is
for the current proxy_admin or break-glass, so a departed proxy_admin can be replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from lumewarp_control_plane import audit, installation
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    conflict,
    mutation,
    parse_uuid_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    InstallationAdminAssign,
    InstallationAdminOut,
    InstallationAdminTransfer,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import forbidden, not_found
from lumewarp_control_plane.models import (
    InstallationAdmin,
    InstallationRole,
    User,
    UserIdentity,
)

router = APIRouter(tags=["installation-admins"])


async def _require_signed_in(session: SessionDep, user_id: uuid.UUID) -> None:
    """An account must have linked an IdP identity before it receives an installation role.

    The role is protected from email linking (`resolve_user`), so assigning it to a user
    who has never signed in would make that account impossible to log into.
    """
    identity = await session.scalar(
        select(UserIdentity.id).where(UserIdentity.user_id == user_id).limit(1)
    )
    if identity is None:
        raise conflict("the user must sign in once before receiving an installation role")


def _out(row: InstallationAdmin, user: User) -> InstallationAdminOut:
    return InstallationAdminOut(
        user_id=row.user_id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        role=row.role,
        assigned_by=row.assigned_by,
        assigned_at=row.assigned_at,
    )


@router.get(
    "/installation-admins",
    response_model=Page[InstallationAdminOut],
    operation_id="listInstallationAdmins",
)
async def list_installation_admins(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[InstallationAdminOut]:
    await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
    stmt = (
        select(InstallationAdmin, User)
        .join(User, User.id == InstallationAdmin.user_id)
        .order_by(InstallationAdmin.user_id)
    )
    if cursor is not None:
        stmt = stmt.where(InstallationAdmin.user_id > parse_uuid_cursor(cursor))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0].user_id) if len(rows) > limit else None
    return Page[InstallationAdminOut](
        items=[_out(row, user) for row, user in rows[:limit]], next_cursor=next_cursor
    )


@router.put(
    "/installation-admins/{user_id}",
    response_model=InstallationAdminOut,
    operation_id="putInstallationAdmin",
)
async def put_installation_admin(
    user_id: uuid.UUID,
    body: InstallationAdminAssign,
    authz: Authz,
    session: SessionDep,
) -> InstallationAdminOut:
    """Give `user_id` the `admin` role. Idempotent; the proxy_admin cannot be demoted here."""
    await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
    if body.role is InstallationRole.PROXY_ADMIN:
        raise validation_error("role", "proxy_admin is only set by transfer")
    async with mutation(session):
        await installation.lock_installation(session)
        await installation.recheck_principal(session, authz.principal)
        await installation.recheck_role(session, authz)
        await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("user not found")
        row = await session.get(InstallationAdmin, user_id, with_for_update=True)
        if row is not None and row.role is InstallationRole.PROXY_ADMIN:
            raise forbidden("the proxy_admin cannot be demoted; transfer the role instead")
        if row is None:
            if not user.is_active:
                raise conflict("the user is not active")
            await _require_signed_in(session, user.id)
            row = InstallationAdmin(
                user_id=user.id,
                role=InstallationRole.ADMIN,
                assigned_by=authz.user_id,
            )
            session.add(row)
            await session.flush()
            audit.record(
                session,
                action="installation_admin.assign",
                resource_type="installation_admin",
                resource_id=user.id,
                principal=authz.principal,
                details={"user_id": user.id, "role": InstallationRole.ADMIN.value},
            )
            await session.refresh(row)
    return _out(row, user)


@router.delete(
    "/installation-admins/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteInstallationAdmin",
)
async def delete_installation_admin(
    user_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
) -> Response:
    """Remove an `admin`. The proxy_admin must be transferred, never removed."""
    await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
    async with mutation(session):
        await installation.lock_installation(session)
        await installation.recheck_principal(session, authz.principal)
        await installation.recheck_role(session, authz)
        await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
        row = await session.get(InstallationAdmin, user_id, with_for_update=True)
        if row is None:
            raise not_found("installation admin not found")
        if row.role is InstallationRole.PROXY_ADMIN:
            raise forbidden("the proxy_admin cannot be removed; transfer the role instead")
        await session.delete(row)
        audit.record(
            session,
            action="installation_admin.remove",
            resource_type="installation_admin",
            resource_id=user_id,
            principal=authz.principal,
            details={"user_id": user_id, "role": row.role.value},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/installation-admins/transfer",
    response_model=InstallationAdminOut,
    operation_id="transferProxyAdmin",
)
async def transfer_proxy_admin(
    body: InstallationAdminTransfer,
    authz: Authz,
    session: SessionDep,
) -> InstallationAdminOut:
    """Make another user proxy_admin; the previous proxy_admin becomes an admin."""
    await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
    async with mutation(session):
        await installation.lock_installation(session)
        await installation.recheck_principal(session, authz.principal)
        await installation.recheck_role(session, authz)
        await authz.require(Action.INSTALLATION_ADMIN_MANAGE)
        current = await session.scalar(
            select(InstallationAdmin)
            .where(InstallationAdmin.role == InstallationRole.PROXY_ADMIN)
            .with_for_update()
        )
        # Re-check after taking the lock: a concurrent transfer may have moved the role.
        if not authz.principal.is_break_glass:
            if current is None or current.user_id != authz.user_id:
                raise forbidden("only the proxy_admin or break-glass can transfer the role")
        target = await session.get(User, body.user_id, with_for_update=True)
        if target is None:
            raise not_found("user not found")
        if not target.is_active:
            raise conflict("the user is not active")
        if current is not None and current.user_id == target.id:
            raise conflict("this user is already the proxy_admin")
        await _require_signed_in(session, target.id)
        target_row = await session.get(InstallationAdmin, target.id, with_for_update=True)
        now = datetime.now(UTC)
        previous = None
        if current is not None:
            previous = current.user_id
            current.role = InstallationRole.ADMIN
            current.assigned_by = authz.user_id
            current.assigned_at = now
            # Release the unique proxy_admin slot before claiming it: the partial unique
            # index is checked by every flush, so a single batch updating both rows in
            # primary-key order would briefly show two proxy_admins and fail.
            await session.flush()
        if target_row is None:
            target_row = InstallationAdmin(user_id=target.id, role=InstallationRole.PROXY_ADMIN)
            session.add(target_row)
        target_row.role = InstallationRole.PROXY_ADMIN
        target_row.assigned_by = authz.user_id
        target_row.assigned_at = now
        await session.flush()
        revoked = await installation.revoke_user_pats(session, target.id, principal=authz.principal)
        audit.record(
            session,
            action="installation_admin.transfer",
            resource_type="installation_admin",
            resource_id=target.id,
            principal=authz.principal,
            details={
                "previous_user_id": previous,
                "new_user_id": target.id,
                "revoked": revoked,
            },
        )
        await session.refresh(target_row)
    return _out(target_row, target)

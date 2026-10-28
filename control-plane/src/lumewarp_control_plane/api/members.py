"""Team members: a user's role in a team and the member's own narrowing policy."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_grants_managed,
    get_team_or_404,
    lock_scope,
    mutation,
    parse_uuid_cursor,
    suspend_user_keys,
    validation_error,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    check_narrowing,
    policy_from_fields,
    policy_of,
    team_effective,
)
from lumewarp_control_plane.api.schemas import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    MemberUpsert,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import (
    ModelAccess,
    Team,
    TeamMember,
    TeamRole,
    User,
    member_scope_id,
)

router = APIRouter(tags=["members"])


def _out(member: TeamMember, user: User) -> MemberOut:
    return MemberOut(
        team_id=member.team_id,
        user_id=member.user_id,
        email=user.email,
        display_name=user.display_name,
        user_active=user.is_active,
        role=member.role,
        member_scope_id=member_scope_id(member.team_id, member.user_id),
        created_at=member.created_at,
        updated_at=member.updated_at,
        model_access=member.model_access,
        allowed_models=list(member.allowed_models),
        rpm_limit=member.rpm_limit,
        tpm_limit=member.tpm_limit,
        max_parallel=member.max_parallel,
        budget_nano_usd=member.budget_nano_usd,
        soft_budget_nano_usd=member.soft_budget_nano_usd,
    )


async def _ensure_other_admin(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """The team must keep an admin once `user_id` stops being one."""
    others = await session.scalar(
        select(func.count())
        .select_from(TeamMember)
        .where(
            TeamMember.team_id == team_id,
            TeamMember.role == TeamRole.ADMIN,
            TeamMember.user_id != user_id,
        )
    )
    if not others:
        raise conflict("a team must keep at least one admin")


async def _member_or_404(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> TeamMember:
    member = await session.get(TeamMember, (team_id, user_id), with_for_update=True)
    if member is None:
        raise not_found("member not found")
    return member


@router.get("/teams/{team_id}/members", response_model=Page[MemberOut], operation_id="listMembers")
async def list_members(
    team_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[MemberOut]:
    team = await get_team_or_404(session, authz, team_id)
    stmt = (
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id)
        .order_by(TeamMember.user_id)
    )
    if cursor is not None:
        stmt = stmt.where(TeamMember.user_id > parse_uuid_cursor(cursor))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0].user_id) if len(rows) > limit else None
    return Page[MemberOut](
        items=[_out(member, user) for member, user in rows[:limit]], next_cursor=next_cursor
    )


@router.put(
    "/teams/{team_id}/members/{user_id}",
    response_model=MemberOut,
    operation_id="putMember",
    responses={201: {"model": MemberOut, "description": "Member created"}},
)
async def put_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpsert,
    request: Request,
    response: Response,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> MemberOut:
    return await _save_member(team_id, user_id, body, request, response, authz, session, settings)


@router.post(
    "/teams/{team_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createMember",
)
async def create_member(
    team_id: uuid.UUID,
    body: MemberCreate,
    request: Request,
    response: Response,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> MemberOut:
    return await _save_member(
        team_id,
        body.user_id,
        body,
        request,
        response,
        authz,
        session,
        settings,
        email=body.email,
        create_only=True,
    )


async def _save_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID | None,
    body: MemberUpsert,
    request: Request,
    response: Response,
    authz: Authz,
    session: AsyncSession,
    settings: SettingsDep,
    *,
    email: str | None = None,
    create_only: bool = False,
) -> MemberOut:
    async with mutation(session):
        await lock_scope(session, settings)
        team = await get_team_or_404(session, authz, team_id)
        await authz.require(Action.TEAM_MEMBERS_MANAGE, team.id)
        ensure_grants_managed(request)
        user = (
            await session.scalar(select(User).where(func.lower(User.email) == func.lower(email)))
            if email is not None
            else await session.get(User, user_id)
        )
        if user is None:
            if create_only:
                raise ApiError(404, "user_not_found", "user not found")
            raise validation_error("user_id", "unknown user")
        if not user.is_active:
            raise conflict("user is not active")
        user_id = user.id
        policy = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        check_narrowing(team_effective(team), policy)
        row = await session.get(TeamMember, (team.id, user_id), with_for_update=True)
        created = row is None
        if create_only and not created:
            raise conflict("user is already a member of this team")
        if row is None:
            row = TeamMember(team_id=team.id, user_id=user_id, role=body.role)
            session.add(row)
        else:
            if row.role is TeamRole.ADMIN and body.role is not TeamRole.ADMIN:
                await _ensure_other_admin(session, team.id, user_id)
            row.role = body.role
        apply_policy(row, policy)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="member.add" if created else "member.update",
            resource_type="member",
            resource_id=user_id,
            principal=authz.principal,
            team_id=team.id,
            details={"user_id": user_id, "role": row.role.value},
        )
        await enqueue_recompile(session, settings, "api.member.put")
    if created:
        response.status_code = status.HTTP_201_CREATED
    return _out(row, user)


@router.patch(
    "/teams/{team_id}/members/{user_id}", response_model=MemberOut, operation_id="updateMember"
)
async def update_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpdate,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> MemberOut:
    async with mutation(session):
        await lock_scope(session, settings)
        team = await get_team_or_404(session, authz, team_id)
        row = await _member_or_404(session, team.id, user_id)
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("member not found")
        await authz.require(Action.TEAM_MEMBERS_MANAGE, team.id)
        ensure_grants_managed(request)
        if "role" in body.model_fields_set and body.role is None:
            raise validation_error("role", "cannot be null")
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        check_narrowing(team_effective(team), proposed)
        changed = proposed != base
        if changed:
            apply_policy(row, proposed)
        if body.role is not None and body.role is not row.role:
            if row.role is TeamRole.ADMIN:
                await _ensure_other_admin(session, team.id, user_id)
            row.role = body.role
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="member.update",
            resource_type="member",
            resource_id=user_id,
            principal=authz.principal,
            team_id=team.id,
            details={
                "user_id": user_id,
                "role": row.role.value,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.member.update")
    return _out(row, user)


@router.delete(
    "/teams/{team_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteMember",
)
async def delete_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    async with mutation(session):
        await lock_scope(session, settings)
        team: Team = await get_team_or_404(session, authz, team_id)
        row = await _member_or_404(session, team.id, user_id)
        await authz.require(Action.TEAM_MEMBERS_MANAGE, team.id)
        ensure_grants_managed(request)
        if row.role is TeamRole.ADMIN:
            await _ensure_other_admin(session, team.id, user_id)
        await session.delete(row)
        suspended = await suspend_user_keys(
            session,
            authz.principal,
            user_id=user_id,
            team_id=team.id,
            reason="owner_removed",
        )
        audit.record(
            session,
            action="member.remove",
            resource_type="member",
            resource_id=user_id,
            principal=authz.principal,
            team_id=team.id,
            details={"user_id": user_id, "suspended_keys": suspended},
        )
        await enqueue_recompile(session, settings, "api.member.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

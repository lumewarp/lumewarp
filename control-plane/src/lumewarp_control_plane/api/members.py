"""Team members: a user's role in a team and the member's own narrowing policy."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_grants_managed,
    get_team_or_404,
    lock_scope,
    mutation,
    parse_uuid_cursor,
    suspend_ineligible_user_keys,
    validation_error,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    policy_from_fields,
    save_grants,
    validate_owner_policy,
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
    InheritedFrom,
    InstallationAdmin,
    MemberPolicy,
    ModelAccess,
    OrgAdmin,
    Team,
    TeamMember,
    TeamRole,
    User,
    member_scope_id,
)
from lumewarp_control_plane.quota import Policy, policy_of

router = APIRouter(tags=["members"])


def _out(member: TeamMember, user: User, policy: MemberPolicy | None) -> MemberOut:
    """A member without a policy row reads as INHERIT with no limits or budget."""
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
        model_access=(policy.model_access if policy is not None else ModelAccess.INHERIT),
        allowed_models=(list(policy.allowed_models) if policy is not None else []),
        mcp_access=(policy.mcp_access if policy is not None else ModelAccess.INHERIT),
        mcp_servers=(list(policy.mcp_servers) if policy is not None else []),
        a2a_access=(policy.a2a_access if policy is not None else ModelAccess.INHERIT),
        a2a_agents=(list(policy.a2a_agents) if policy is not None else []),
        rpm_limit=policy.rpm_limit if policy is not None else None,
        tpm_limit=policy.tpm_limit if policy is not None else None,
        max_parallel=policy.max_parallel if policy is not None else None,
        budget_nano_usd=policy.budget_nano_usd if policy is not None else None,
    )


def _inherited_out(
    team_id: uuid.UUID,
    user: User,
    inherited: tuple[InheritedFrom, datetime],
    policy: MemberPolicy | None,
) -> MemberOut:
    """An admin of the team by inheritance, with no membership row of its own."""
    source, at = inherited
    out = _out(
        TeamMember(
            team_id=team_id, user_id=user.id, role=TeamRole.ADMIN, created_at=at, updated_at=at
        ),
        user,
        policy,
    )
    out.inherited_from = source
    out.direct = False
    return out


async def _policy_or_none(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> MemberPolicy | None:
    return (
        await session.get(MemberPolicy, (team_id, user_id), with_for_update=True)
        if lock
        else await session.get(MemberPolicy, (team_id, user_id))
    )


async def _ensure_admin_role_change(
    authz: Authz, team_id: uuid.UUID, before: TeamRole | None, after: TeamRole | None
) -> None:
    """Adding, promoting, demoting or removing an admin is installation-level.

    One guard for every member write (`_save_member`, `update_member`, `delete_member`):
    a team admin cannot hand out, take away or give up the team's admin role, including
    on themselves. Plain member changes stay `TEAM_MEMBERS_MANAGE`.
    """
    if before is after:
        return
    if before is TeamRole.ADMIN or after is TeamRole.ADMIN:
        await authz.require(Action.TEAM_ADMIN_ASSIGN, team_id)


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
    """Direct members plus the team's admins by inheritance, in one user-id order.

    Installation admins and the local admins of the team's org are listed with
    `inherited_from`; an external backend's org admins appear only as far as local rows
    exist, because a backend cannot list who holds a relation.
    """
    team = await get_team_or_404(session, authz, team_id)
    await authz.require(Action.TEAM_ADMIN_READ, team.id)
    ids = union(
        select(TeamMember.user_id.label("user_id")).where(TeamMember.team_id == team.id),
        select(OrgAdmin.user_id).where(OrgAdmin.org_id == team.org_id),
        select(InstallationAdmin.user_id),
    ).subquery()
    stmt = select(ids.c.user_id).order_by(ids.c.user_id)
    if cursor is not None:
        stmt = stmt.where(ids.c.user_id > parse_uuid_cursor(cursor))
    page_ids = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(page_ids[limit - 1]) if len(page_ids) > limit else None
    page_ids = page_ids[:limit]
    users = {
        user.id: user for user in await session.scalars(select(User).where(User.id.in_(page_ids)))
    }
    members = {
        member.user_id: member
        for member in await session.scalars(
            select(TeamMember).where(
                TeamMember.team_id == team.id, TeamMember.user_id.in_(page_ids)
            )
        )
    }
    policies = {
        row.user_id: row
        for row in await session.scalars(
            select(MemberPolicy).where(
                MemberPolicy.team_id == team.id, MemberPolicy.user_id.in_(page_ids)
            )
        )
    }
    granted: dict[uuid.UUID, tuple[InheritedFrom, datetime]] = {}
    for user_id, at in await session.execute(
        select(OrgAdmin.user_id, OrgAdmin.assigned_at).where(
            OrgAdmin.org_id == team.org_id, OrgAdmin.user_id.in_(page_ids)
        )
    ):
        granted[user_id] = (InheritedFrom.ORG, at)
    for user_id, at in await session.execute(
        select(InstallationAdmin.user_id, InstallationAdmin.assigned_at).where(
            InstallationAdmin.user_id.in_(page_ids)
        )
    ):
        granted[user_id] = (InheritedFrom.INSTALLATION, at)
    items = []
    for user_id in page_ids:
        user = users[user_id]
        inherited = granted.get(user_id)
        member = members.get(user_id)
        if member is not None:
            out = _out(member, user, policies.get(user_id))
            out.inherited_from = inherited[0] if inherited else None
        else:
            assert inherited is not None
            out = _inherited_out(team.id, user, inherited, policies.get(user_id))
        items.append(out)
    return Page[MemberOut](items=items, next_cursor=next_cursor)


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
        row = await session.get(TeamMember, (team.id, user_id), with_for_update=True)
        policy_row = await _policy_or_none(session, team.id, user_id, lock=True)
        # PUT replaces the member's policy: omitted fields take the INHERIT/no-limit
        # defaults instead of keeping the stored row. The stored row is the validation
        # base, so a value the PUT resends unchanged is not rechecked after a parent
        # decrease (a real change, or a clear, is still checked).
        base = policy_of(policy_row) if policy_row is not None else None
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        await validate_owner_policy(session, team, proposed, base)
        created = row is None
        if create_only and not created:
            raise conflict("user is already a member of this team")
        await _ensure_admin_role_change(
            authz, team.id, None if row is None else row.role, body.role
        )
        if row is None:
            row = TeamMember(team_id=team.id, user_id=user_id, role=body.role)
            session.add(row)
        else:
            row.role = body.role
        if policy_row is None and proposed == Policy(ModelAccess.INHERIT, ()):
            # No exception and no grants: leaving the row out keeps membership only.
            pass
        else:
            if policy_row is None:
                policy_row = MemberPolicy(team_id=team.id, user_id=user_id)
                session.add(policy_row)
            apply_policy(policy_row, proposed)
            await session.flush()
            await save_grants(session, policy_row, proposed)
            await session.refresh(policy_row)
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
    return _out(row, user, policy_row)


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
        policy_row = await _policy_or_none(session, team.id, user_id, lock=True)
        base = policy_of(policy_row) if policy_row is not None else Policy(ModelAccess.INHERIT, ())
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        await validate_owner_policy(session, team, proposed, None if policy_row is None else base)
        changed = proposed != base
        if changed:
            if policy_row is None:
                policy_row = MemberPolicy(team_id=team.id, user_id=user_id)
                session.add(policy_row)
            apply_policy(policy_row, proposed)
            await session.flush()
            await save_grants(session, policy_row, proposed)
            await session.refresh(policy_row)
        if body.role is not None and body.role is not row.role:
            await _ensure_admin_role_change(authz, team.id, row.role, body.role)
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
    return _out(row, user, policy_row)


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
    metrics: MetricsDep,
) -> Response:
    async with mutation(session):
        await lock_scope(session, settings)
        team: Team = await get_team_or_404(session, authz, team_id)
        row = await _member_or_404(session, team.id, user_id)
        await authz.require(Action.TEAM_MEMBERS_MANAGE, team.id)
        ensure_grants_managed(request)
        await _ensure_admin_role_change(authz, team.id, row.role, None)
        await session.delete(row)
        # An installation admin or org admin keeps their keys: they still administer
        # the team, and may own keys in it, without the membership.
        suspended = await suspend_ineligible_user_keys(
            session,
            authz.authorizer,
            authz.principal,
            user_id=user_id,
            team_ids={team.id},
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
    metrics.count_committed_suspensions(session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

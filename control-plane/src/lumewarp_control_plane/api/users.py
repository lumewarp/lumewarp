"""Admin users, the current user and personal access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    lock_scope,
    mutation,
    suspend_user_keys,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    MeOut,
    MeTeamOut,
    Page,
    TokenCreate,
    TokenCreated,
    TokenOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from lumewarp_control_plane.authz import Action, Authz, AuthzUnavailable, Relation, RequestAuthz
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import (
    ApiKey,
    PersonalAccessToken,
    Team,
    TeamMember,
    TeamRole,
    User,
)
from lumewarp_control_plane.security import Principal, SecretKind, generate_secret

router = APIRouter(tags=["users"])


def _check_not_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


async def _my_teams(
    authz: RequestAuthz, session: SessionDep, user_id: uuid.UUID
) -> list[MeTeamOut]:
    if authz.authorizer.manages_grants:
        rows = await session.execute(
            select(Team.id, Team.name, TeamMember.role)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user_id)
            .order_by(Team.name)
        )
        return [MeTeamOut(team_id=r.id, team_name=r.name, role=r.role) for r in rows]
    teams = (await session.execute(select(Team.id, Team.name).order_by(Team.name))).all()
    candidates = [t.id for t in teams]
    uid = str(user_id)
    members = await authz.authorizer.list_teams(uid, Relation.MEMBER, candidates)
    if members is None:
        raise AuthzUnavailable()
    admins = await authz.authorizer.list_teams(uid, Relation.ADMIN, candidates)
    if admins is None:
        raise AuthzUnavailable()
    return [
        MeTeamOut(
            team_id=t.id,
            team_name=t.name,
            role=TeamRole.ADMIN if t.id in admins else TeamRole.MEMBER,
        )
        for t in teams
        if t.id in members or t.id in admins
    ]


@router.get("/users", response_model=Page[UserOut], operation_id="listUsers")
async def list_users(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[UserOut]:
    await authz.require(Action.USER_MANAGE)
    stmt = select(User).order_by(User.id)
    rows, next_cursor = await uuid_page(session, stmt, User.id, limit, cursor)
    return Page[UserOut](
        items=[UserOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUser",
)
async def create_user(body: UserCreate, authz: Authz, session: SessionDep) -> UserOut:
    await authz.require(Action.USER_MANAGE)
    async with mutation(session):
        row = User(
            email=body.email,
            display_name=body.display_name,
            is_superuser=body.is_superuser,
            is_active=body.is_active,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="user.create",
            resource_type="user",
            resource_id=row.id,
            principal=authz.principal,
            details={"email": row.email},
        )
    return UserOut.model_validate(row)


@router.get("/users/me", response_model=MeOut, operation_id="getCurrentUser")
async def get_current_user(request: Request, authz: Authz, session: SessionDep) -> MeOut:
    principal = authz.principal
    manages_grants = request.app.state.authorizer.manages_grants
    if principal.user_id is None:
        return MeOut(
            kind=principal.kind.value,
            user_id=None,
            email=None,
            display_name="bootstrap",
            is_superuser=True,
            manages_grants=manages_grants,
            teams=[],
        )
    user = await session.get(User, principal.user_id)
    if user is None:
        raise not_found("user not found")
    return MeOut(
        kind=principal.kind.value,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_superuser=await authz.is_superuser(),
        manages_grants=manages_grants,
        teams=await _my_teams(authz, session, user.id),
    )


@router.get("/users/me/tokens", response_model=Page[TokenOut], operation_id="listOwnTokens")
async def list_own_tokens(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[TokenOut]:
    if authz.user_id is None:
        return Page[TokenOut](items=[], next_cursor=None)
    stmt = (
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == authz.user_id)
        .order_by(PersonalAccessToken.id)
    )
    rows, next_cursor = await uuid_page(session, stmt, PersonalAccessToken.id, limit, cursor)
    return Page[TokenOut](
        items=[TokenOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/users/me/tokens",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createOwnToken",
)
async def create_own_token(
    body: TokenCreate,
    authz: Authz,
    session: SessionDep,
) -> TokenCreated:
    if authz.user_id is None:
        raise ApiError(400, "bad_request", "the bootstrap token cannot own personal tokens")
    return await _issue_token(body, authz.principal, session, authz.user_id)


@router.post(
    "/users/{user_id}/tokens",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUserToken",
)
async def create_user_token(
    user_id: uuid.UUID,
    body: TokenCreate,
    authz: Authz,
    session: SessionDep,
    response: Response,
) -> TokenCreated:
    """Issue a PAT on behalf of a user; only installation superusers may do so."""
    await authz.require(Action.USER_MANAGE)
    response.headers["Cache-Control"] = "no-store"
    return await _issue_token(body, authz.principal, session, user_id)


async def _issue_token(
    body: TokenCreate, principal: Principal, session: SessionDep, user_id: uuid.UUID
) -> TokenCreated:
    new = generate_secret(SecretKind.PAT)
    async with mutation(session):
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("user not found")
        if not user.is_active:
            raise conflict("cannot issue a token for an inactive user")
        row = PersonalAccessToken(
            user_id=user_id,
            name=body.name,
            token_hash=new.hash,
            display_prefix=new.display_prefix,
            expires_at=body.expires_at,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="pat.create",
            resource_type="token",
            resource_id=row.id,
            principal=principal,
            details={"name": row.name, "user_id": user_id},
        )
    return TokenCreated(token=TokenOut.model_validate(row), secret=new.secret)


@router.post(
    "/users/me/tokens/{token_id}/revoke",
    response_model=TokenOut,
    operation_id="revokeOwnToken",
)
async def revoke_own_token(token_id: uuid.UUID, authz: Authz, session: SessionDep) -> TokenOut:
    if authz.user_id is None:
        raise not_found("token not found")
    async with mutation(session):
        row = await session.get(PersonalAccessToken, token_id)
        if row is None or row.user_id != authz.user_id:
            raise not_found("token not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            audit.record(
                session,
                action="pat.revoke",
                resource_type="token",
                resource_id=row.id,
                principal=authz.principal,
                details={"name": row.name},
            )
    return TokenOut.model_validate(row)


@router.get("/users/{user_id}", response_model=UserOut, operation_id="getUser")
async def get_user(user_id: uuid.UUID, authz: Authz, session: SessionDep) -> UserOut:
    await authz.require(Action.USER_MANAGE)
    row = await session.get(User, user_id)
    if row is None:
        raise not_found("user not found")
    return UserOut.model_validate(row)


@router.patch("/users/{user_id}", response_model=UserOut, operation_id="updateUser")
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> UserOut:
    await authz.require(Action.USER_MANAGE)
    _check_not_null(body, "email", "display_name", "is_superuser", "is_active")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(User, user_id)
        if row is None:
            raise not_found("user not found")
        if row.id == authz.user_id and (body.is_superuser is False or body.is_active is False):
            raise conflict("you cannot remove your own superuser or active status")
        was_active = row.is_active
        for name in ("email", "display_name", "is_superuser", "is_active"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        suspended = 0
        if was_active and not row.is_active:
            suspended = await suspend_user_keys(
                session,
                authz.principal,
                user_id=row.id,
                team_id=None,
                reason="owner_deactivated",
            )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="user.update",
            resource_type="user",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "email": row.email,
                "changes": body.model_dump(exclude_unset=True),
                "suspended_keys": suspended,
            },
        )
        await enqueue_recompile(session, settings, "api.user.update")
    metrics.count_committed_suspensions(session)
    return UserOut.model_validate(row)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteUser",
)
async def delete_user(
    user_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> Response:
    await authz.require(Action.USER_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(User, user_id)
        if row is None:
            raise not_found("user not found")
        if row.id == authz.user_id:
            raise conflict("you cannot delete yourself")
        key = await session.scalar(select(ApiKey.id).where(ApiKey.user_id == row.id).limit(1))
        if key is not None:
            raise conflict("user owns keys (kept for usage history); deactivate the user instead")
        other_admin_teams = select(TeamMember.team_id).where(
            TeamMember.user_id != row.id, TeamMember.role == TeamRole.ADMIN
        )
        last_admin_team = await session.scalar(
            select(TeamMember.team_id)
            .where(
                TeamMember.user_id == row.id,
                TeamMember.role == TeamRole.ADMIN,
                TeamMember.team_id.not_in(other_admin_teams),
            )
            .limit(1)
        )
        if last_admin_team is not None:
            raise conflict("user is the last admin of a team; assign another admin first")
        email = row.email
        await session.delete(row)
        audit.record(
            session,
            action="user.delete",
            resource_type="user",
            resource_id=row.id,
            principal=authz.principal,
            details={"email": email},
        )
        # Memberships cascade, which changes the compiled owners.
        await enqueue_recompile(session, settings, "api.user.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

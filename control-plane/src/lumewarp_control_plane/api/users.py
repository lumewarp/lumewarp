"""Admin users, the current user and personal access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select

from lumewarp_control_plane import audit, rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    GrantOut,
    MeOut,
    Page,
    TokenCreate,
    TokenCreated,
    TokenOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import PersonalAccessToken, User
from lumewarp_control_plane.security import CurrentPrincipal, SecretKind, generate_secret

router = APIRouter(tags=["users"])

_INSTALLATION = rbac.ResourceScope()


def _check_not_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


@router.get("/users", response_model=Page[UserOut], operation_id="listUsers")
async def list_users(
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[UserOut]:
    rbac.require(principal, rbac.Action.USER_MANAGE, _INSTALLATION)
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
async def create_user(
    body: UserCreate, principal: CurrentPrincipal, session: SessionDep
) -> UserOut:
    rbac.require(principal, rbac.Action.USER_MANAGE, _INSTALLATION)
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
            principal=principal,
            details={"email": row.email},
        )
    return UserOut.model_validate(row)


@router.get("/users/me", response_model=MeOut, operation_id="getCurrentUser")
async def get_current_user(principal: CurrentPrincipal, session: SessionDep) -> MeOut:
    if principal.user_id is None:
        return MeOut(
            kind=principal.kind.value,
            user_id=None,
            email=None,
            display_name="bootstrap",
            is_superuser=True,
            grants=[],
        )
    user = await session.get(User, principal.user_id)
    if user is None:
        raise not_found("user not found")
    return MeOut(
        kind=principal.kind.value,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_superuser=user.is_superuser,
        grants=[
            GrantOut(org_id=g.org_id, team_id=g.team_id, role=g.role) for g in principal.grants
        ],
    )


@router.get("/users/me/tokens", response_model=Page[TokenOut], operation_id="listOwnTokens")
async def list_own_tokens(
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[TokenOut]:
    if principal.user_id is None:
        return Page[TokenOut](items=[], next_cursor=None)
    stmt = (
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == principal.user_id)
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
    principal: CurrentPrincipal,
    session: SessionDep,
) -> TokenCreated:
    if principal.user_id is None:
        raise ApiError(400, "bad_request", "the bootstrap token cannot own personal tokens")
    new = generate_secret(SecretKind.PAT)
    async with mutation(session):
        row = PersonalAccessToken(
            user_id=principal.user_id,
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
            details={"name": row.name},
        )
    return TokenCreated(token=TokenOut.model_validate(row), secret=new.secret)


@router.post(
    "/users/me/tokens/{token_id}/revoke",
    response_model=TokenOut,
    operation_id="revokeOwnToken",
)
async def revoke_own_token(
    token_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep
) -> TokenOut:
    if principal.user_id is None:
        raise not_found("token not found")
    async with mutation(session):
        row = await session.get(PersonalAccessToken, token_id)
        if row is None or row.user_id != principal.user_id:
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
                principal=principal,
                details={"name": row.name},
            )
    return TokenOut.model_validate(row)


@router.get("/users/{user_id}", response_model=UserOut, operation_id="getUser")
async def get_user(user_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep) -> UserOut:
    rbac.require(principal, rbac.Action.USER_MANAGE, _INSTALLATION)
    row = await session.get(User, user_id)
    if row is None:
        raise not_found("user not found")
    return UserOut.model_validate(row)


@router.patch("/users/{user_id}", response_model=UserOut, operation_id="updateUser")
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> UserOut:
    rbac.require(principal, rbac.Action.USER_MANAGE, _INSTALLATION)
    _check_not_null(body, "email", "display_name", "is_superuser", "is_active")
    async with mutation(session):
        row = await session.get(User, user_id)
        if row is None:
            raise not_found("user not found")
        for name in ("email", "display_name", "is_superuser", "is_active"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="user.update",
            resource_type="user",
            resource_id=row.id,
            principal=principal,
            details={"email": row.email, "changes": body.model_dump(exclude_unset=True)},
        )
    return UserOut.model_validate(row)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteUser",
)
async def delete_user(user_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep) -> None:
    rbac.require(principal, rbac.Action.USER_MANAGE, _INSTALLATION)
    async with mutation(session):
        row = await session.get(User, user_id)
        if row is None:
            raise not_found("user not found")
        email = row.email
        await session.delete(row)
        audit.record(
            session,
            action="user.delete",
            resource_type="user",
            resource_id=row.id,
            principal=principal,
            details={"email": email},
        )
    return None

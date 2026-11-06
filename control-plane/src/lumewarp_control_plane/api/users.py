"""Admin users, the current user and personal access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import and_, func, or_, select

from lumewarp_control_plane import audit, installation
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
from lumewarp_control_plane.api.policy import policy_of
from lumewarp_control_plane.api.schemas import (
    MeBudgetOut,
    MeKeyLimitsOut,
    MeLimitsOut,
    MeOrgOut,
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
from lumewarp_control_plane.errors import ApiError, forbidden, not_found
from lumewarp_control_plane.models import (
    ApiKey,
    CatalogName,
    InstallationRole,
    KeyLifecycle,
    KeyOwnerKind,
    MemberPolicy,
    Org,
    PersonalAccessToken,
    ScopeType,
    Team,
    TeamMember,
    TeamRole,
    UsageTotal,
    User,
    member_scope_id,
)
from lumewarp_control_plane.orgs import live_orgs
from lumewarp_control_plane.quota import (
    BudgetLevel,
    org_effective,
    owner_effective,
    remaining_nano_usd,
    team_effective,
)
from lumewarp_control_plane.security import Principal, SecretKind, generate_secret
from lumewarp_control_plane.windows import month_window

router = APIRouter(tags=["users"])


def _check_not_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


async def _ensure_proxy_admin_protected(
    authz: Authz,
    session: SessionDep,
    target_id: uuid.UUID,
    action: Action | None = None,
) -> None:
    """Only the proxy_admin itself and break-glass may change or act as that account.

    Every write that can deactivate, delete, re-address or mint a token for the account
    calls this; the role cannot be removed through the installation-admin endpoints either.
    The check runs under the installation lock, so a concurrent promotion (transfer or
    first login) cannot slip in between "not the proxy_admin" and the write.
    """
    await installation.lock_installation(session)
    # Authentication happened before the lock; revalidate this request's own credential
    # after acquiring it. A PAT revoked while we waited must not act (or mint a new token).
    await installation.recheck_principal(session, authz.principal)
    # Re-read the caller's role too: a role removed while we waited must fail the action
    # that the cached role authorized before the lock.
    await installation.recheck_role(session, authz)
    if action is not None:
        await authz.require(action)
    if authz.principal.is_break_glass or target_id == authz.user_id:
        return
    if await installation.role_of(session, target_id) is InstallationRole.PROXY_ADMIN:
        raise forbidden("the proxy_admin account is protected")


async def _my_teams(
    authz: RequestAuthz,
    session: SessionDep,
    user_id: uuid.UUID,
    *,
    installation_admin: bool,
) -> list[MeTeamOut]:
    rows: list[tuple[Team, TeamRole, TeamMember | None]]
    if authz.authorizer.manages_grants:
        result = await session.execute(
            select(Team, TeamMember)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user_id)
            .order_by(Team.name)
        )
        rows = [(team, member.role, member) for team, member in result]
    else:
        teams = (await session.scalars(select(Team).order_by(Team.name))).all()
        local = {
            member.team_id: member
            for member in await session.scalars(
                select(TeamMember).where(TeamMember.user_id == user_id)
            )
        }
        candidates = [team.id for team in teams]
        uid = str(user_id)
        members = await authz.authorizer.list_teams(uid, Relation.MEMBER, candidates)
        if members is None:
            raise AuthzUnavailable()
        admins = await authz.authorizer.list_teams(uid, Relation.ADMIN, candidates)
        if admins is None:
            raise AuthzUnavailable()
        rows = [
            (
                team,
                TeamRole.ADMIN if team.id in admins else TeamRole.MEMBER,
                local.get(team.id),
            )
            for team in teams
            if team.id in members or team.id in admins
        ]
    return await _team_details(session, user_id, rows, installation_admin=installation_admin)


async def _team_details(
    session: SessionDep,
    user_id: uuid.UUID,
    rows: list[tuple[Team, TeamRole, TeamMember | None]],
    *,
    installation_admin: bool,
) -> list[MeTeamOut]:
    """Enrich memberships with what a key of this member could do in each team.

    The model list and the limits are the member's *effective* policy from the one
    resolver (`quota`), org included; they never widen what a member may read. The org and
    team budgets and spends are only filled for a team admin or an installation admin: a
    plain member gets the effective remaining amount, computed server-side. Spend is the
    current UTC month. A missing local member row (external IAM) still gets the team's
    member defaults, exactly as in the compiler.
    """
    if not rows:
        return []
    catalog = sorted((await session.scalars(select(CatalogName.name))).all())
    window_start, window_end = month_window(datetime.now(UTC))
    team_ids = [team.id for team, _, _ in rows]
    org_ids = {team.org_id for team, _, _ in rows}
    orgs = {org.id: org for org in await session.scalars(select(Org).where(Org.id.in_(org_ids)))}
    # Every team of these orgs, so the org spend is derived from its whole subtree.
    org_team_ids = {
        team_id: org_id
        for team_id, org_id in await session.execute(
            select(Team.id, Team.org_id).where(Team.org_id.in_(org_ids))
        )
    }
    member_ids = [
        member_scope_id(team.id, user_id) for team, _, member in rows if member is not None
    ]
    scope_filters = [
        and_(UsageTotal.scope_type == ScopeType.TEAM, UsageTotal.scope_id.in_(org_team_ids))
    ]
    if member_ids:
        scope_filters.append(
            and_(UsageTotal.scope_type == ScopeType.MEMBER, UsageTotal.scope_id.in_(member_ids))
        )
    spent = {
        (scope_type, scope_id): cost
        for scope_type, scope_id, cost in await session.execute(
            select(UsageTotal.scope_type, UsageTotal.scope_id, UsageTotal.cost_nano_usd).where(
                UsageTotal.window_start == window_start, or_(*scope_filters)
            )
        )
    }
    org_spent: dict[uuid.UUID, int] = {}
    for team_id, org_id in org_team_ids.items():
        org_spent[org_id] = org_spent.get(org_id, 0) + spent.get((ScopeType.TEAM, team_id), 0)
    key_counts = {
        team_id: count
        for team_id, count in await session.execute(
            select(ApiKey.team_id, func.count())
            .where(
                ApiKey.team_id.in_(team_ids),
                ApiKey.owner_kind == KeyOwnerKind.USER,
                ApiKey.user_id == user_id,
                ApiKey.lifecycle != KeyLifecycle.REVOKED,
            )
            .group_by(ApiKey.team_id)
        )
    }
    member_policies = {
        (policy.team_id, policy.user_id): policy
        for policy in await session.scalars(
            select(MemberPolicy).where(
                MemberPolicy.team_id.in_(team_ids), MemberPolicy.user_id == user_id
            )
        )
    }
    details: list[MeTeamOut] = []
    for team, role, _member in rows:
        org = orgs[team.org_id]
        team_limits = policy_of(team)
        member_policy = member_policies.get((team.id, user_id))
        member_limits = policy_of(member_policy) if member_policy is not None else None
        team_eff = team_effective(org_effective(org), team)
        effective_limits = owner_effective(
            team_eff,
            team,
            member_limits,
            service_account=False,
        )
        # The member scope exists for external-backend members too (their keys compile
        # with the same uuid5 id), so read it even without a local membership row.
        member_spent = spent.get((ScopeType.MEMBER, member_scope_id(team.id, user_id)), 0)
        team_spent = spent.get((ScopeType.TEAM, team.id), 0)
        org_spend = org_spent.get(org.id, 0)
        budgets: list[BudgetLevel] = []
        for level, scope_id, budget, used in (
            (ScopeType.ORG, org.id, org.budget_nano_usd, org_spend),
            (ScopeType.TEAM, team.id, team_limits.budget_nano_usd, team_spent),
            (
                ScopeType.MEMBER,
                member_scope_id(team.id, user_id),
                member_limits.budget_nano_usd if member_limits else None,
                member_spent,
            ),
        ):
            if budget is not None:
                budgets.append(
                    BudgetLevel(
                        level=level,
                        scope_id=scope_id,
                        budget_nano_usd=budget,
                        spent_nano_usd=used,
                    )
                )
        # The team scope is the team-admin view. An installation admin passes every
        # team's checks, even without a direct admin role. The org scope is visible to
        # org admins too; `role` is only the direct team role, so it follows the team view.
        can_read_team = installation_admin or role is TeamRole.ADMIN
        details.append(
            MeTeamOut(
                team_id=team.id,
                team_name=team.name,
                role=role,
                models=catalog
                if effective_limits.models is None
                else sorted(effective_limits.models),
                all_models=effective_limits.models is None,
                limits=MeLimitsOut(
                    rpm_limit=effective_limits.rpm_limit,
                    tpm_limit=effective_limits.tpm_limit,
                    max_parallel=effective_limits.max_parallel,
                ),
                key_limits=MeKeyLimitsOut(
                    max_keys_per_member=team.max_keys_per_member,
                    max_member_key_lifetime_seconds=team.max_member_key_lifetime_seconds,
                    key_count=key_counts.get(team.id, 0),
                ),
                budget=MeBudgetOut(
                    org_budget_nano_usd=(org.budget_nano_usd if can_read_team else None),
                    org_spent_nano_usd=(org_spend if can_read_team else None),
                    team_budget_nano_usd=(team_limits.budget_nano_usd if can_read_team else None),
                    team_spent_nano_usd=team_spent if can_read_team else None,
                    member_budget_nano_usd=(
                        member_limits.budget_nano_usd if member_limits else None
                    ),
                    member_spent_nano_usd=member_spent,
                    remaining_nano_usd=remaining_nano_usd(budgets),
                    window_start=window_start,
                    window_end=window_end,
                ),
            )
        )
    return details


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
            display_name="break-glass",
            is_admin=True,
            installation_role=None,
            manages_grants=manages_grants,
            teams=[],
        )
    user = await session.get(User, principal.user_id)
    if user is None:
        raise not_found("user not found")
    role = await authz.installation_role()
    unavailable = False
    try:
        teams = await _my_teams(authz, session, user.id, installation_admin=role is not None)
    except AuthzUnavailable:
        teams = []
        unavailable = True
    admin_orgs: list[MeOrgOut] = []
    try:
        # Direct org-admin grants only. `RequestAuthz` applies the team inheritance from
        # them; `/users/me` never mixes those teams into the direct-membership list.
        orgs = await live_orgs(session)
        administered = (
            {org.id for org in orgs}
            if role is not None
            else (await authz.admin_orgs([org.id for org in orgs])) or set()
        )
        admin_orgs = [MeOrgOut(id=org.id, name=org.name) for org in orgs if org.id in administered]
    except AuthzUnavailable:
        unavailable = True
    return MeOut(
        kind=principal.kind.value,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=role is not None,
        installation_role=role,
        authz_unavailable=unavailable,
        manages_grants=manages_grants,
        teams=teams,
        admin_orgs=admin_orgs,
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
        raise ApiError(400, "bad_request", "break-glass sessions cannot own personal tokens")
    return await _issue_token(body, authz.principal, session, authz.user_id, authz=authz)


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
    """Issue a PAT on behalf of a user; requires an installation admin."""
    await authz.require(Action.USER_MANAGE)
    response.headers["Cache-Control"] = "no-store"
    return await _issue_token(
        body,
        authz.principal,
        session,
        user_id,
        authz=authz,
        action=Action.USER_MANAGE,
    )


async def _issue_token(
    body: TokenCreate,
    principal: Principal,
    session: SessionDep,
    user_id: uuid.UUID,
    *,
    authz: RequestAuthz | None = None,
    action: Action | None = None,
) -> TokenCreated:
    new = generate_secret(SecretKind.PAT)
    async with mutation(session):
        # Inside the same transaction: a separate read would start one and make
        # `mutation` fail with "a transaction is already begun".
        if authz is not None:
            await _ensure_proxy_admin_protected(authz, session, user_id, action=action)
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
    _check_not_null(body, "email", "display_name", "is_active")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(User, user_id)
        if row is None:
            raise not_found("user not found")
        await _ensure_proxy_admin_protected(authz, session, row.id, action=Action.USER_MANAGE)
        if row.id == authz.user_id and body.is_active is False:
            raise conflict("you cannot remove your own active status")
        was_active = row.is_active
        for name in ("email", "display_name", "is_active"):
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
        await _ensure_proxy_admin_protected(authz, session, row.id, action=Action.USER_MANAGE)
        if row.id == authz.user_id:
            raise conflict("you cannot delete yourself")
        key = await session.scalar(select(ApiKey.id).where(ApiKey.user_id == row.id).limit(1))
        if key is not None:
            raise conflict("user owns keys (kept for usage history); deactivate the user instead")
        # A team without admins is valid: an installation admin is always above it, and
        # another admin can be assigned by one.
        role = await installation.role_of(session, row.id)
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
        if role is not None:
            # The role row goes with the account via ON DELETE CASCADE; record the
            # privilege removal explicitly so audit consumers see one remove event.
            audit.record(
                session,
                action="installation_admin.remove",
                resource_type="installation_admin",
                resource_id=row.id,
                principal=authz.principal,
                details={"user_id": row.id, "role": role.value},
            )
        # Memberships cascade, which changes the compiled owners.
        await enqueue_recompile(session, settings, "api.user.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

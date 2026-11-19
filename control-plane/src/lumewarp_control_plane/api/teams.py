"""Teams: the top-level grouping that owns members, service accounts and keys."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    DefaultZoneDep,
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_not_null,
    get_team_or_404,
    lock_hierarchy,
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    grant_ids,
    policy_from_fields,
    policy_of,
    save_grants,
    team_effective_of,
    validate_team_policy,
)
from lumewarp_control_plane.api.schemas import Page, TeamBasicOut, TeamCreate, TeamOut, TeamUpdate
from lumewarp_control_plane.api.zone_limits import (
    apply_default_zone_limits,
    limits_by_zone,
    limits_of,
    member_defaults_by_zone,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    ApiKey,
    KeyLifecycle,
    ModelAccess,
    Org,
    ServiceAccount,
    Team,
)

router = APIRouter(tags=["teams"])


async def _team_out(session: AsyncSession, row: Team, zone: str) -> TeamOut:
    """The full team with its effective model set: org ∩ team, groups expanded."""
    out = TeamOut.model_validate(row)
    models = (await team_effective_of(session, row, zone)).models
    out.effective_models = None if models is None else sorted(models)
    apply_default_zone_limits(
        out,
        (await limits_by_zone(session, "team", {row.id})).get(("team", row.id), {}),
        default_zone=zone,
        member_defaults=(await member_defaults_by_zone(session, {row.id})).get(row.id, {}),
    )
    return out


@router.get(
    "/teams",
    response_model=Page[TeamOut | TeamBasicOut],
    operation_id="listTeams",
)
async def list_teams(
    authz: Authz,
    session: SessionDep,
    zone: DefaultZoneDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[TeamOut | TeamBasicOut]:
    """Teams the caller can read: every live team for an installation admin, otherwise the teams
    the caller belongs to or administers. A team admin gets the full record; any other member
    gets the basic one and reads their own effective policy from `GET /users/me`. Archived teams
    are not listed.
    """
    stmt = select(Team).where(Team.deleted_at.is_(None)).order_by(Team.id)
    is_admin = await authz.is_admin()
    if not is_admin:
        candidates = set(await session.scalars(select(Team.id).where(Team.deleted_at.is_(None))))
        readable = await authz.readable_teams(candidates)
        if readable is not None:
            stmt = stmt.where(Team.id.in_(readable))
    rows, next_cursor = await uuid_page(session, stmt, Team.id, limit, cursor)
    # The full team policy only goes to team admins; every other member gets the basic
    # record and reads their own effective policy from /users/me.
    admin_teams = None if is_admin else await authz.admin_teams([row.id for row in rows])
    items = [
        await _team_out(session, row, zone)
        if admin_teams is None or row.id in admin_teams
        else TeamBasicOut.model_validate(row)
        for row in rows
    ]
    return Page[TeamOut | TeamBasicOut](items=items, next_cursor=next_cursor)


@router.post(
    "/teams",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createTeam",
)
async def create_team(
    body: TeamCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    """Create a team in `org_id`. Requires an admin of that org; anyone else gets the same 404 as
    for an unknown org. The team's policy may not exceed the org's effective policy, and an
    archived org answers 409.
    """
    async with mutation(session):
        await lock_hierarchy(session)
        org = await session.get(Org, body.org_id)
        # A caller who does not administer the org sees the same 404 as an unknown org.
        if org is None or not await authz.is_org_admin(org.id):
            raise not_found("org not found")
        if org.deleted_at is not None:
            raise conflict("the org is archived")
        policy = policy_from_fields(body, default_mode=ModelAccess.ALL)
        row = Team(
            org_id=org.id,
            name=body.name,
            max_keys_per_member=body.max_keys_per_member,
            max_member_key_lifetime_seconds=body.max_member_key_lifetime_seconds,
        )
        # A new team is a child write too: it may not start above its org's effective
        # policy in the default zone. Its per-zone limits are written afterwards through
        # `PUT /zones/{zone}/limits`.
        await validate_team_policy(session, settings.zone, row, policy, None)
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await save_grants(session, row, policy)
        await session.refresh(row)
        audit.record(
            session,
            action="team.create",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            org_id=row.org_id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.team.create")
    return await _team_out(session, row, settings.zone)


@router.get(
    "/teams/{team_id}",
    response_model=TeamOut | TeamBasicOut,
    operation_id="getTeam",
)
async def get_team(
    team_id: uuid.UUID, authz: Authz, session: SessionDep, zone: DefaultZoneDep
) -> TeamOut | TeamBasicOut:
    """A team admin gets the full record; any other member gets the basic one. A caller outside the
    team gets 404.
    """
    row = await get_team_or_404(session, authz, team_id)
    if await authz.allowed(Action.TEAM_ADMIN_READ, row.id):
        return await _team_out(session, row, zone)
    return TeamBasicOut.model_validate(row)


@router.patch("/teams/{team_id}", response_model=TeamOut, operation_id="updateTeam")
async def update_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    """Changing models, limits or the budget requires an org admin; changing the name, the member
    key limits or the member defaults requires a team admin. A body that resends the current
    policy unchanged is not a policy change. A member default may not exceed the team's
    effective limit.
    """
    async with mutation(session):
        await lock_hierarchy(session)
        ensure_not_null(body, "name")
        row = await get_team_or_404(session, authz, team_id)
        zone = settings.zone
        limits = await limits_of(session, zone, {"team_id": row.id})
        base = policy_of(row, limits)
        proposed = policy_from_fields(body, default_mode=ModelAccess.ALL, base=base)
        changed = proposed != base
        settings_fields = (
            "max_keys_per_member",
            "max_member_key_lifetime_seconds",
        )
        field_changes = {
            field: getattr(body, field)
            for field in settings_fields
            if field in body.model_fields_set and getattr(body, field) != getattr(row, field)
        }
        settings_changed = (body.name is not None and body.name != row.name) or bool(field_changes)
        # Decide from what actually changes: a form that resends the current policy must
        # still be usable by a team admin who may not set quota.
        if changed:
            await authz.require(Action.TEAM_QUOTA_SET, row.id)
        if settings_changed or not changed:
            await authz.require(Action.TEAM_SETTINGS_UPDATE, row.id)
        if changed:
            await grant_ids(session, proposed.allowed_models)
            await validate_team_policy(session, zone, row, proposed, base)
            apply_policy(row, proposed)
            await save_grants(session, row, proposed)
            await session.refresh(row)
        if body.name is not None:
            row.name = body.name
        for field, value in field_changes.items():
            setattr(row, field, value)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="team.update",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            org_id=row.org_id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.team.update")
    return await _team_out(session, row, settings.zone)


@router.delete(
    "/teams/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteTeam",
)
async def delete_team(
    team_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Archive a team. Irreversible: keys are revoked, service accounts deactivated and the
    name becomes free for a new live team. History (usage, audit) keeps resolving.
    """
    async with mutation(session):
        await lock_hierarchy(session)
        # Read without the live-team guard: an already archived team answers 409 to a
        # reader, while anyone who cannot read it gets 404.
        row = await session.get(Team, team_id, with_for_update=True)
        if row is None or not await authz.allowed(Action.TEAM_READ, row.id):
            raise not_found("team not found")
        if row.deleted_at is not None:
            raise conflict("the team is already archived")
        await authz.require(Action.TEAM_DELETE, row.id)
        now = datetime.now(UTC)
        keys = (
            await session.scalars(
                select(ApiKey).where(
                    ApiKey.team_id == row.id, ApiKey.lifecycle != KeyLifecycle.REVOKED
                )
            )
        ).all()
        for key in keys:
            key.lifecycle = KeyLifecycle.REVOKED
            key.suspended_reason = None
            key.revoked_at = now
            # Revoking kills both credentials; never leave a grace hash behind.
            key.previous_key_hash = None
            key.previous_key_expires_at = None
        accounts = (
            await session.scalars(
                select(ServiceAccount).where(
                    ServiceAccount.team_id == row.id,
                    ServiceAccount.deactivated_at.is_(None),
                )
            )
        ).all()
        for account in accounts:
            account.deactivated_at = now
        row.deleted_at = now
        await session.flush()
        audit.record(
            session,
            action="team.archive",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            org_id=row.org_id,
            details={
                "name": row.name,
                "revoked_keys": len(keys),
                "deactivated_service_accounts": len(accounts),
            },
        )
        await enqueue_recompile(session, settings, "api.team.archive")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

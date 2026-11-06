"""Org administration: the org API, archiving and the audit/budget consequences.

Every check is over HTTP so the documented matrix (installation admin vs org admin vs team
admin vs member) is what is tested, not an internal function.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import (
    AuditLog,
    ModelAccess,
    OrgAdmin,
    OrgBudgetWindow,
    ScopeType,
    Team,
    TeamRole,
    UsageTotal,
    UserIdentity,
)
from lumewarp_control_plane.usage.org_budget import enforce_org_budgets
from lumewarp_control_plane.windows import month_window
from tests.api.helpers import (
    ADMIN_AUTH,
    auth,
    create_org,
    suffix,
    usage_event,
    user_with_token,
)
from tests.factories import make_key, make_member, make_org, make_team, make_user


class World:
    def __init__(self, org_a, org_b, team_a, team_b, admin_pat, team_admin_pat, member_pat):
        self.org_a, self.org_b = org_a, org_b
        self.team_a, self.team_b = team_a, team_b
        self.admin = auth(admin_pat) if admin_pat else None
        self.team_admin = auth(team_admin_pat)
        self.member = auth(member_pat)
        self.org_admin_user = None


@pytest.fixture
async def world(client, committed) -> World:
    org_a = await make_org(committed, f"org-a-{suffix()}")
    org_b = await make_org(committed, f"org-b-{suffix()}")
    team_a = await make_team(committed, org=org_a)
    team_b = await make_team(committed, org=org_b)
    org_admin, org_admin_pat = await user_with_token(committed)
    team_admin, team_admin_pat = await user_with_token(committed)
    member, member_pat = await user_with_token(committed)
    committed.add(OrgAdmin(org_id=org_a.id, user_id=org_admin.id))
    await make_member(committed, team_a, team_admin, role=TeamRole.ADMIN)
    await make_member(committed, team_a, member)
    await committed.commit()
    state = World(org_a, org_b, team_a, team_b, org_admin_pat, team_admin_pat, member_pat)
    state.org_admin_user = org_admin
    return state


def test_list_and_get_orgs_respect_the_reader(client, committed, world: World) -> None:
    # Installation admin: every live org.
    listed = client.get("/api/v1/orgs?limit=500", headers=ADMIN_AUTH)
    assert listed.status_code == 200, listed.text
    ids = {row["id"] for row in listed.json()["items"]}
    assert {str(world.org_a.id), str(world.org_b.id)} <= ids
    assert client.get(f"/api/v1/orgs/{world.org_a.id}", headers=ADMIN_AUTH).status_code == 200

    # Org admin: only its own org, and another org does not exist for it.
    listed = client.get("/api/v1/orgs", headers=world.admin)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"]] == [str(world.org_a.id)]
    assert client.get(f"/api/v1/orgs/{world.org_a.id}", headers=world.admin).status_code == 200
    assert client.get(f"/api/v1/orgs/{world.org_b.id}", headers=world.admin).status_code == 404

    # A plain member administers no org and gets no existence hint.
    assert client.get("/api/v1/orgs", headers=world.member).json()["items"] == []
    assert client.get(f"/api/v1/orgs/{world.org_a.id}", headers=world.member).status_code == 404


def test_only_an_installation_admin_manages_orgs(client, world: World) -> None:
    for method, path, body in (
        ("POST", "/api/v1/orgs", {"name": f"by-org-admin-{suffix()}"}),
        ("PATCH", f"/api/v1/orgs/{world.org_a.id}", {"budget_nano_usd": 1}),
        ("DELETE", f"/api/v1/orgs/{world.org_a.id}", None),
        ("PUT", f"/api/v1/orgs/{world.org_a.id}/admins/{world.org_admin_user.id}", None),
        ("DELETE", f"/api/v1/orgs/{world.org_a.id}/admins/{world.org_admin_user.id}", None),
    ):
        response = client.request(method, path, json=body, headers=world.admin)
        assert response.status_code == 403, (method, path, response.text)
        member = client.request(method, path, json=body, headers=world.member)
        assert member.status_code == 403, (method, path, member.text)


def test_installation_creates_and_patches_org_policy(client, world: World) -> None:
    created = client.post(
        "/api/v1/orgs",
        json={
            "name": f"new-{suffix()}",
            "model_access": "list",
            "allowed_models": [],
            "rpm_limit": 10,
            "budget_nano_usd": 1000,
        },
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    org = created.json()
    assert (org["model_access"], org["allowed_models"]) == ("list", [])
    assert (org["rpm_limit"], org["budget_nano_usd"]) == (10, 1000)

    patched = client.patch(
        f"/api/v1/orgs/{org['id']}",
        json={"name": f"renamed-{suffix()}", "model_access": "all", "rpm_limit": None},
        headers=ADMIN_AUTH,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["model_access"] == "all"
    assert patched.json()["rpm_limit"] is None

    # The org is the root: a team cannot start above its current effective policy.
    new_org = create_org(client, rpm_limit=10)
    too_wide = client.post(
        "/api/v1/teams",
        json={"name": f"wide-{suffix()}", "org_id": new_org["id"], "rpm_limit": 11},
        headers=ADMIN_AUTH,
    )
    assert too_wide.status_code == 422, too_wide.text
    inside = client.post(
        "/api/v1/teams",
        json={"name": f"inside-{suffix()}", "org_id": new_org["id"], "rpm_limit": 10},
        headers=ADMIN_AUTH,
    )
    assert inside.status_code == 201, inside.text


async def test_org_admin_creates_and_archives_teams_only_in_its_org(
    client, committed, world: World
) -> None:
    created = client.post(
        "/api/v1/teams",
        json={"name": f"by-org-admin-{suffix()}", "org_id": str(world.org_a.id)},
        headers=world.admin,
    )
    assert created.status_code == 201, created.text
    assert created.json()["org_id"] == str(world.org_a.id)

    # Another org answers 404: the caller cannot even see it.
    foreign = client.post(
        "/api/v1/teams",
        json={"name": f"foreign-{suffix()}", "org_id": str(world.org_b.id)},
        headers=world.admin,
    )
    assert foreign.status_code == 404, foreign.text

    # A team admin does not get team creation or archiving (org level).
    as_team_admin = client.post(
        "/api/v1/teams",
        json={"name": f"nope-{suffix()}", "org_id": str(world.org_a.id)},
        headers=world.team_admin,
    )
    assert as_team_admin.status_code == 404, as_team_admin.text
    assert (
        client.delete(f"/api/v1/teams/{world.team_a.id}", headers=world.team_admin).status_code
        == 403
    )
    # A member of a foreign team (the org admin is not) and another org's team.
    assert client.delete(f"/api/v1/teams/{world.team_b.id}", headers=world.admin).status_code == 404

    team_id = world.team_a.id
    archived = client.delete(f"/api/v1/teams/{team_id}", headers=world.admin)
    assert archived.status_code == 204, archived.text
    committed.expire_all()
    assert (await committed.get(Team, team_id)).deleted_at is not None


async def test_org_admin_assignment_is_installation_only_and_needs_a_login(
    client, committed, world: World
) -> None:
    target, _ = await user_with_token(committed)
    await committed.commit()
    path = f"/api/v1/orgs/{world.org_a.id}/admins/{target.id}"

    # Not signed in through the IdP yet: the grant would be impossible to use.
    assert client.put(path, headers=ADMIN_AUTH).status_code == 409
    committed.add(UserIdentity(user_id=target.id, issuer="test", subject=str(uuid.uuid4())))
    await committed.commit()

    assigned = client.put(path, headers=ADMIN_AUTH)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["user_id"] == str(target.id)
    # Idempotent.
    assert client.put(path, headers=ADMIN_AUTH).status_code == 200
    listed = client.get(f"/api/v1/orgs/{world.org_a.id}/admins", headers=ADMIN_AUTH)
    assert str(target.id) in {row["user_id"] for row in listed.json()["items"]}

    # The target now administers the org; an org admin still cannot assign.
    target_auth = auth(await login_pat(committed, target.id))
    assert client.get("/api/v1/orgs", headers=target_auth).status_code == 200
    assert client.put(path, headers=target_auth).status_code == 403

    assert client.delete(path, headers=ADMIN_AUTH).status_code == 204
    assert client.delete(path, headers=ADMIN_AUTH).status_code == 404

    # Under an external backend the local grant table is read-only for writes.
    authorizer = client.app.state.authorizer
    original = authorizer.manages_grants
    authorizer.manages_grants = False
    try:
        assert client.put(path, headers=ADMIN_AUTH).status_code == 409
        assert client.delete(path, headers=ADMIN_AUTH).status_code == 409
    finally:
        authorizer.manages_grants = original


async def login_pat(session: AsyncSession, user_id: uuid.UUID) -> str:
    """A fresh PAT for a user id (the test needs HTTP, not just a row)."""
    from lumewarp_control_plane.models import PersonalAccessToken
    from lumewarp_control_plane.security import SecretKind, generate_secret

    new = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user_id,
            name=f"test-{suffix()}",
            token_hash=new.hash,
            display_prefix=new.display_prefix,
        )
    )
    await session.flush()
    await session.commit()
    return new.secret


def test_org_archive_needs_no_live_team_and_blocks_new_ones(client) -> None:
    org = create_org(client)
    team = client.post(
        "/api/v1/teams",
        json={"name": f"archived-{suffix()}", "org_id": org["id"]},
        headers=ADMIN_AUTH,
    ).json()
    assert client.delete(f"/api/v1/orgs/{org['id']}", headers=ADMIN_AUTH).status_code == 409
    assert client.delete(f"/api/v1/teams/{team['id']}", headers=ADMIN_AUTH).status_code == 204
    assert client.delete(f"/api/v1/orgs/{org['id']}", headers=ADMIN_AUTH).status_code == 204
    # Irreversible, and the org cannot receive a team any more.
    assert client.delete(f"/api/v1/orgs/{org['id']}", headers=ADMIN_AUTH).status_code == 409
    assert (
        client.patch(
            f"/api/v1/orgs/{org['id']}", json={"name": "nope"}, headers=ADMIN_AUTH
        ).status_code
        == 409
    )
    blocked = client.post(
        "/api/v1/teams",
        json={"name": f"into-archive-{suffix()}", "org_id": org["id"]},
        headers=ADMIN_AUTH,
    )
    assert blocked.status_code == 409, blocked.text
    # The archived org is no longer listed.
    listed = client.get("/api/v1/orgs?limit=500", headers=ADMIN_AUTH).json()["items"]
    assert org["id"] not in {row["id"] for row in listed}


async def test_org_usage_is_grouped_by_team_at_query_time(client, committed, world: World) -> None:
    member = await make_user(committed)
    await make_member(committed, world.team_a, member)
    key = await make_key(committed, world.team_a, member)
    committed.add(usage_event(key, ts=datetime.now(UTC), cost_nano_usd=1234))
    await committed.commit()

    usage = client.get(f"/api/v1/orgs/{world.org_a.id}/usage", headers=world.admin)
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["org_id"] == str(world.org_a.id)
    assert body["cost_nano_usd"] >= 1234
    row = next(item for item in body["teams"] if item["team_id"] == str(world.team_a.id))
    assert (row["requests"], row["cost_nano_usd"], row["archived"]) == (1, 1234, False)

    assert (
        client.get(f"/api/v1/orgs/{world.org_b.id}/usage", headers=world.admin).status_code == 404
    )
    assert (
        client.get(f"/api/v1/orgs/{world.org_a.id}/usage", headers=world.member).status_code == 404
    )


async def test_raising_an_org_budget_resets_its_month_window(client, committed) -> None:
    org = create_org(client, budget_nano_usd=100)
    team = client.post(
        "/api/v1/teams",
        json={"name": f"budget-{suffix()}", "org_id": org["id"]},
        headers=ADMIN_AUTH,
    ).json()
    org_id, team_id = uuid.UUID(org["id"]), uuid.UUID(team["id"])
    window_start, _ = month_window(datetime.now(UTC))

    async def spend(cost: int) -> None:
        async with AsyncSession(committed.bind) as session, session.begin():
            total = await session.scalar(
                select(UsageTotal).where(
                    UsageTotal.scope_type == ScopeType.TEAM,
                    UsageTotal.scope_id == team_id,
                    UsageTotal.window_start == window_start,
                )
            )
            if total is None:
                session.add(
                    UsageTotal(
                        scope_type=ScopeType.TEAM,
                        scope_id=team_id,
                        window_start=window_start,
                        cost_nano_usd=cost,
                        request_count=1,
                    )
                )
            else:
                total.cost_nano_usd = cost
            await enforce_org_budgets(session, "gw-test", {team_id}, window_start)

    async def audits() -> int:
        async with AsyncSession(committed.bind) as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action == "budget.hard_exceeded",
                        AuditLog.resource_type == ScopeType.ORG.value,
                        AuditLog.resource_id == str(org_id),
                    )
                )
                or 0
            )

    await spend(200)
    assert await audits() == 1
    async with AsyncSession(committed.bind) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgBudgetWindow)
                .where(OrgBudgetWindow.org_id == org_id)
            )
            == 1
        )

    # Raising the budget clears the exceeded state and forgets the month's report, so the
    # next crossing must audit and recompile again.
    raised = client.patch(
        f"/api/v1/orgs/{org_id}", json={"budget_nano_usd": 1000}, headers=ADMIN_AUTH
    )
    assert raised.status_code == 200, raised.text
    async with AsyncSession(committed.bind) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgBudgetWindow)
                .where(OrgBudgetWindow.org_id == org_id)
            )
            == 0
        )

    await spend(2000)
    assert await audits() == 2


def test_team_quota_cannot_exceed_the_org(client, world: World) -> None:
    org = create_org(client, rpm_limit=5, budget_nano_usd=100)
    team = client.post(
        "/api/v1/teams",
        json={"name": f"narrow-{suffix()}", "org_id": org["id"], "rpm_limit": 5},
        headers=ADMIN_AUTH,
    )
    assert team.status_code == 201, team.text
    too_wide = client.patch(
        f"/api/v1/teams/{team.json()['id']}", json={"rpm_limit": 6}, headers=ADMIN_AUTH
    )
    assert too_wide.status_code == 422, too_wide.text


async def test_org_policy_uses_all_as_the_root_default(client, committed) -> None:
    org = create_org(client)
    assert org["model_access"] == ModelAccess.ALL.value
    assert org["allowed_models"] == []

    # A team in the org can name a model the org allows; an INHERIT org is refused.
    invalid = client.post(
        "/api/v1/orgs",
        json={"name": f"inherit-{suffix()}", "model_access": "inherit"},
        headers=ADMIN_AUTH,
    )
    assert invalid.status_code == 422, invalid.text


async def test_org_admins_read_org_events_and_archived_team_audit(client, world: World) -> None:
    # An org-level event: its row has org_id set and team_id NULL.
    patched = client.patch(
        f"/api/v1/orgs/{world.org_a.id}", json={"budget_nano_usd": 5000}, headers=ADMIN_AUTH
    )
    assert patched.status_code == 200, patched.text
    assert client.delete(f"/api/v1/teams/{world.team_a.id}", headers=world.admin).status_code == 204

    org_admin = client.get("/api/v1/audit?limit=500", headers=world.admin)
    assert org_admin.status_code == 200, org_admin.text
    actions = {row["action"] for row in org_admin.json()["items"]}
    assert {"org.update", "team.archive"} <= actions

    # A team admin of the (now archived) team reads its team rows but never the org event.
    team_admin = client.get("/api/v1/audit?limit=500", headers=world.team_admin)
    assert team_admin.status_code == 200, team_admin.text
    rows = team_admin.json()["items"]
    assert any(row["action"] == "team.archive" for row in rows)
    assert all(row["action"] != "org.update" for row in rows)

    # Explicitly filtered history of the archived team is readable by its org admin.
    filtered = client.get(
        "/api/v1/audit",
        params={"team_id": str(world.team_a.id)},
        headers=world.admin,
    )
    assert filtered.status_code == 200, filtered.text

    # The org filter returns the org's own rows (team_id NULL included) and nothing from
    # another org; a team admin cannot use it.
    by_org = client.get(
        "/api/v1/audit",
        params={"org_id": str(world.org_a.id), "limit": 500},
        headers=world.admin,
    )
    assert by_org.status_code == 200, by_org.text
    rows = by_org.json()["items"]
    assert rows and all(row["org_id"] == str(world.org_a.id) for row in rows)
    assert any(row["action"] == "org.update" and row["team_id"] is None for row in rows)
    assert (
        client.get(
            "/api/v1/audit", params={"org_id": str(world.org_b.id)}, headers=world.admin
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/audit", params={"org_id": str(world.org_a.id)}, headers=world.team_admin
        ).status_code
        == 404
    )

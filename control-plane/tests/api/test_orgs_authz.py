"""Org administration: inheritance, its boundaries and the direct-membership rule.

The org write API does not exist yet, so orgs and `org_admins` rows are created directly.
Every check here is what `RequestAuthz` adds on top of the backend's direct relations.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.models import AuditLog, OrgAdmin, TeamMember, TeamRole
from tests.api.helpers import ADMIN_AUTH, auth, suffix, usage_event, user_with_token
from tests.factories import make_key, make_member, make_org, make_team

TS = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


class World:
    """Two orgs with one team each, an org admin (not a member), a team admin and a member."""

    def __init__(self, org_a, org_b, team_a, team_b, org_admin, team_admin, member, key):
        self.org_a, self.org_b = org_a, org_b
        self.team_a, self.team_b = team_a, team_b
        self.org_admin, self.team_admin, self.member = org_admin, team_admin, member
        self.key = key
        self.org_admin_pat = None
        self.org_admin_session = None
        self.team_admin_pat = None
        self.member_pat = None


@pytest.fixture
async def world(client, committed) -> World:
    org_a = await make_org(committed, f"org-a-{suffix()}")
    org_b = await make_org(committed, f"org-b-{suffix()}")
    team_a = await make_team(committed, org=org_a, rpm_limit=100, budget_nano_usd=10_000_000)
    team_b = await make_team(committed, org=org_b, rpm_limit=100)
    org_admin, org_admin_pat = await user_with_token(committed)
    team_admin, team_admin_pat = await user_with_token(committed)
    member, member_pat = await user_with_token(committed)
    committed.add(OrgAdmin(org_id=org_a.id, user_id=org_admin.id))
    await make_member(committed, team_a, team_admin, role=TeamRole.ADMIN)
    await make_member(committed, team_a, member)
    key = await make_key(committed, team_a, member, name="member-key")
    committed.add(usage_event(key, ts=TS))
    session_token = await sessions.create_session(committed, org_admin.id, 3600)
    await committed.commit()
    state = World(org_a, org_b, team_a, team_b, org_admin, team_admin, member, key)
    state.org_admin_pat = auth(org_admin_pat)
    state.org_admin_session = auth("session:" + session_token)
    state.team_admin_pat = auth(team_admin_pat)
    state.member_pat = auth(member_pat)
    return state


USAGE = {"from": "2026-03-01T00:00:00Z", "to": "2026-04-01T00:00:00Z"}


def test_org_admin_reads_every_team_of_its_org(client, world: World) -> None:
    headers = world.org_admin_pat
    listed = client.get("/api/v1/teams", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [str(world.team_a.id)]
    # The org admin gets the whole-team view (policy included), not TeamBasicOut.
    assert listed.json()["items"][0]["rpm_limit"] == 100
    detail = client.get(f"/api/v1/teams/{world.team_a.id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["budget_nano_usd"] == 10_000_000
    for path in ("members", "keys", "service-accounts"):
        response = client.get(f"/api/v1/teams/{world.team_a.id}/{path}", headers=headers)
        assert response.status_code == 200, (path, response.text)
    members = client.get(f"/api/v1/teams/{world.team_a.id}/members", headers=headers)
    assert {row["user_id"] for row in members.json()["items"]} == {
        str(world.member.id),
        str(world.team_admin.id),
    }
    keys = client.get(f"/api/v1/teams/{world.team_a.id}/keys", headers=headers).json()["items"]
    assert [row["id"] for row in keys] == [str(world.key.id)]
    usage = client.get("/api/v1/usage", params={**USAGE, "group_by": "team"}, headers=headers)
    assert usage.status_code == 200, usage.text
    assert [row["team_id"] for row in usage.json()["items"]] == [str(world.team_a.id)]
    audit = client.get("/api/v1/audit", headers=headers)
    assert audit.status_code == 200, audit.text


def test_org_admin_administers_its_teams_and_another_org_is_404(client, world: World) -> None:
    headers = world.org_admin_pat
    other = world.team_b.id

    # Quota, team admin assignment, service accounts and another member's key.
    quota = client.patch(
        f"/api/v1/teams/{world.team_a.id}", json={"rpm_limit": 42}, headers=headers
    )
    assert quota.status_code == 200, quota.text
    promoted = client.put(
        f"/api/v1/teams/{world.team_a.id}/members/{world.member.id}",
        json={"role": "admin"},
        headers=headers,
    )
    assert promoted.status_code in (200, 201), promoted.text
    assert promoted.json()["role"] == "admin"
    account = client.post(
        f"/api/v1/teams/{world.team_a.id}/service-accounts",
        json={"name": "by-org-admin"},
        headers=headers,
    )
    assert account.status_code == 201, account.text
    suspended = client.patch(
        f"/api/v1/keys/{world.key.id}", json={"lifecycle": "suspended"}, headers=headers
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["suspended_reason"] == "admin"

    # Nothing of another org exists for this caller, including the workspace filters.
    assert client.get(f"/api/v1/teams/{other}", headers=headers).status_code == 404
    assert (
        client.patch(f"/api/v1/teams/{other}", json={"rpm_limit": 1}, headers=headers).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/v1/teams/{other}/members/{world.member.id}",
            json={"role": "member"},
            headers=headers,
        ).status_code
        == 404
    )
    for path in ("members", "keys", "service-accounts"):
        assert client.get(f"/api/v1/teams/{other}/{path}", headers=headers).status_code == 404
    assert (
        client.get(
            "/api/v1/usage", params={**USAGE, "team_id": str(other)}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get("/api/v1/audit", params={"team_id": str(other)}, headers=headers).status_code
        == 404
    )
    # An org team it does not administer never shows up in an unfiltered list either.
    assert client.get("/api/v1/teams", headers=headers).json()["items"] == [
        row
        for row in client.get("/api/v1/teams", headers=headers).json()["items"]
        if row["id"] == str(world.team_a.id)
    ]


def test_org_admin_without_membership_cannot_create_a_key(client, world: World) -> None:
    response = client.post(
        f"/api/v1/teams/{world.team_a.id}/keys",
        json={"name": "by-org-admin"},
        headers=world.org_admin_pat,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert "not a member" in response.json()["error"]["message"]


def test_team_admin_can_neither_set_quota_nor_assign_admins(client, world: World) -> None:
    headers = world.team_admin_pat
    response = client.patch(
        f"/api/v1/teams/{world.team_a.id}", json={"rpm_limit": 5}, headers=headers
    )
    assert response.status_code == 403, response.text
    response = client.put(
        f"/api/v1/teams/{world.team_a.id}/members/{world.member.id}",
        json={"role": "admin"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    # Settings below quota stay a team-admin action.
    renamed = client.patch(
        f"/api/v1/teams/{world.team_a.id}",
        json={"name": f"renamed-{suffix()}"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text


def test_installation_admin_can_do_everything(client, world: World) -> None:
    for team in (world.team_a, world.team_b):
        assert client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).status_code == 200
        assert (
            client.patch(
                f"/api/v1/teams/{team.id}", json={"rpm_limit": 7}, headers=ADMIN_AUTH
            ).status_code
            == 200
        )
        assert client.get(f"/api/v1/teams/{team.id}/members", headers=ADMIN_AUTH).status_code == 200


def test_pat_and_session_agree(client, world: World) -> None:
    requests = [
        ("GET", "/teams", None),
        ("GET", f"/teams/{world.team_a.id}", None),
        ("GET", f"/teams/{world.team_a.id}/members", None),
        ("GET", f"/teams/{world.team_a.id}/keys", None),
        ("GET", "/usage", USAGE),
        ("GET", "/audit", None),
        ("PATCH", f"/teams/{world.team_a.id}", {"name": f"pat-{suffix()}"}),
        ("GET", f"/teams/{world.team_b.id}", None),
        ("POST", f"/teams/{world.team_a.id}/keys", {"name": "session"}),
    ]
    for method, path, body in requests:
        statuses = [
            client.request(method, "/api/v1" + path, json=body, headers=headers).status_code
            for headers in (world.org_admin_pat, world.org_admin_session)
        ]
        assert statuses[0] == statuses[1], (method, path, statuses)


def test_users_me_lists_admin_orgs_separately_from_direct_memberships(client, world: World) -> None:
    response = client.get("/api/v1/users/me", headers=world.org_admin_pat)
    assert response.status_code == 200, response.text
    me = response.json()
    assert me["is_admin"] is False
    assert [org["id"] for org in me["admin_orgs"]] == [str(world.org_a.id)]
    assert me["admin_orgs"][0]["name"] == world.org_a.name
    # Direct memberships only: the org's team is not mixed in, even though the org admin
    # administers it.
    assert me["teams"] == []

    member_me = client.get("/api/v1/users/me", headers=world.member_pat).json()
    assert [team["team_id"] for team in member_me["teams"]] == [str(world.team_a.id)]
    assert member_me["admin_orgs"] == []

    admin_me = client.get("/api/v1/users/me", headers=ADMIN_AUTH).json()
    assert admin_me["is_admin"] is True
    assert admin_me["admin_orgs"] == []


def test_org_admin_audit_covers_its_teams_only(client, world: World) -> None:
    # Produce one team-scoped row through the org admin itself.
    assert (
        client.patch(
            f"/api/v1/teams/{world.team_a.id}", json={"rpm_limit": 11}, headers=world.org_admin_pat
        ).status_code
        == 200
    )
    rows = client.get("/api/v1/audit", headers=world.org_admin_pat).json()["items"]
    assert rows and all(row["team_id"] == str(world.team_a.id) for row in rows)
    assert {row["action"] for row in rows} >= {"team.update"}
    # A plain member has no audit view at all.
    assert client.get("/api/v1/audit", headers=world.member_pat).status_code == 403


async def test_audit_rows_carry_the_team_org(client, committed, world: World) -> None:
    assert (
        client.patch(
            f"/api/v1/teams/{world.team_a.id}", json={"rpm_limit": 13}, headers=world.org_admin_pat
        ).status_code
        == 200
    )
    row = await committed.scalar(
        select(AuditLog)
        .where(AuditLog.action == "team.update", AuditLog.team_id == world.team_a.id)
        .order_by(AuditLog.id.desc())
    )
    assert row is not None and row.org_id == world.org_a.id


async def test_usage_without_team_filter_is_scoped_to_the_org(
    client, committed, world: World
) -> None:
    other = await make_key(committed, world.team_b, world.member)
    committed.add(usage_event(other, ts=TS, requested="other"))
    await committed.commit()
    response = client.get(
        "/api/v1/usage",
        params={**USAGE, "group_by": "team"},
        headers=world.org_admin_pat,
    )
    assert response.status_code == 200, response.text
    assert {row["team_id"] for row in response.json()["items"]} == {str(world.team_a.id)}


async def test_org_admin_does_not_gain_direct_membership(client, committed, world: World) -> None:
    """`member of team` stays direct: the org relation must not create a members row or a key."""
    assert (
        await committed.scalar(
            select(TeamMember.user_id).where(TeamMember.user_id == world.org_admin.id)
        )
        is None
    )
    response = client.post(
        f"/api/v1/teams/{world.team_a.id}/keys",
        json={"name": "not-a-member"},
        headers=world.org_admin_pat,
    )
    assert response.status_code == 422

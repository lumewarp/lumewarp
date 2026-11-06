"""Team CRUD, visibility and history-preserving deletion."""

import uuid

import pytest
from sqlalchemy import select

from lumewarp_control_plane.models import Team, TeamMember, TeamRole
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team, make_user


def test_team_crud_and_unique_name(client) -> None:
    name = f"team-{suffix()}"
    created = client.post(
        "/api/v1/teams",
        json={"name": name, "budget_nano_usd": 1000},
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    team = created.json()
    assert team["model_access"] == "all"
    assert team["budget_nano_usd"] == 1000
    assert team["org_id"]
    assert client.post("/api/v1/teams", json={"name": name}, headers=ADMIN_AUTH).status_code == 409
    path = f"/api/v1/teams/{team['id']}"
    assert client.get(path, headers=ADMIN_AUTH).json() == team
    updated = client.patch(
        path,
        json={"name": f"renamed-{suffix()}", "budget_nano_usd": None},
        headers=ADMIN_AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["budget_nano_usd"] is None
    assert client.delete(path, headers=ADMIN_AUTH).status_code == 204
    assert client.get(path, headers=ADMIN_AUTH).status_code == 404


async def test_team_list_only_returns_readable_teams(client, committed) -> None:
    user, token = await user_with_token(committed)
    own = await make_team(committed)
    other = await make_team(committed)
    await make_member(committed, own, user)
    await committed.commit()
    listed = client.get("/api/v1/teams", headers=auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [str(own.id)]
    for team_id in (other.id, uuid.uuid4()):
        assert client.get(f"/api/v1/teams/{team_id}", headers=auth(token)).status_code == 404


@pytest.mark.parametrize("dependency", ["key", "service_account"])
async def test_team_delete_preserves_dependencies(client, committed, dependency) -> None:
    team = await make_team(committed)
    if dependency == "key":
        owner = await make_user(committed)
        key = await make_key(committed, team, owner)
    else:
        await make_service_account(committed, team)
    await committed.commit()
    if dependency == "key":
        assert client.post(f"/api/v1/keys/{key.id}/revoke", headers=ADMIN_AUTH).status_code == 200
    response = client.delete(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH)
    assert response.status_code == 409
    assert client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).status_code == 200


async def test_team_admin_may_change_settings_but_not_quota(client, committed) -> None:
    team = await make_team(
        committed,
        rpm_limit=10,
        budget_nano_usd=1000,
        max_keys_per_member=2,
        max_member_key_lifetime_seconds=3600,
    )
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}"
    headers = auth(token)

    for body in (
        {"rpm_limit": 20},
        {"budget_nano_usd": 2000},
        {"model_access": "list", "allowed_models": []},
    ):
        response = client.patch(path, json=body, headers=headers)
        assert response.status_code == 403, (body, response.text)
        assert response.json()["error"]["code"] == "forbidden"
    unchanged = await committed.get(Team, team.id)
    assert unchanged is not None
    await committed.refresh(unchanged)
    assert (unchanged.rpm_limit, unchanged.budget_nano_usd) == (10, 1000)

    # Name and the member key limits are the team admin's own settings.
    renamed = client.patch(
        path,
        json={"name": f"renamed-{suffix()}", "max_keys_per_member": 3},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["max_keys_per_member"] == 3
    # Resending the unchanged policy values still works for a team admin.
    resend = client.patch(
        path,
        json={
            "model_access": "all",
            "allowed_models": [],
            "rpm_limit": 10,
            "tpm_limit": None,
            "max_parallel": None,
            "budget_nano_usd": 1000,
            "max_member_key_lifetime_seconds": 3600,
        },
        headers=headers,
    )
    assert resend.status_code == 200, resend.text
    # A plain member may not touch the team at all, not even with a no-op payload.
    for body in ({"name": "member-edit"}, {"model_access": "all"}, {}):
        response = client.patch(path, json=body, headers=auth(member_token))
        assert response.status_code == 403, (body, response.text)
    # Installation admin sets quota.
    quota = client.patch(path, json={"rpm_limit": 5}, headers=ADMIN_AUTH)
    assert quota.status_code == 200, quota.text
    assert quota.json()["rpm_limit"] == 5


async def test_team_read_is_basic_for_a_plain_member(client, committed) -> None:
    team = await make_team(
        committed,
        rpm_limit=10,
        budget_nano_usd=1000,
        max_keys_per_member=2,
        max_member_key_lifetime_seconds=3600,
    )
    member, member_token = await user_with_token(committed)
    admin, admin_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    await committed.commit()
    basic_keys = {
        "id",
        "org_id",
        "name",
        "created_at",
        "updated_at",
        "max_keys_per_member",
        "max_member_key_lifetime_seconds",
    }
    for path in (f"/api/v1/teams/{team.id}", "/api/v1/teams"):
        response = client.get(path, headers=auth(member_token))
        assert response.status_code == 200, response.text
        body = response.json() if path.endswith(str(team.id)) else response.json()["items"][0]
        assert set(body) == basic_keys, path
        assert (body["name"], body["max_keys_per_member"]) == (team.name, 2)
    full = client.get(f"/api/v1/teams/{team.id}", headers=auth(admin_token))
    assert full.status_code == 200
    assert {"model_access", "allowed_models", "rpm_limit", "budget_nano_usd"} <= set(full.json())
    listed = client.get("/api/v1/teams", headers=auth(admin_token)).json()["items"]
    assert {"model_access", "allowed_models", "rpm_limit", "budget_nano_usd"} <= set(listed[0])
    assert client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).status_code == 200


async def test_team_delete_cascades_members(client, committed) -> None:
    team = await make_team(committed)
    member = await make_member(committed, team)
    await committed.commit()
    assert client.delete(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).status_code == 204
    assert (
        await committed.scalar(
            select(TeamMember).where(
                TeamMember.team_id == team.id, TeamMember.user_id == member.user_id
            )
        )
        is None
    )

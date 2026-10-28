"""Team CRUD, visibility and history-preserving deletion."""

import uuid

import pytest
from sqlalchemy import select

from lumewarp_control_plane.models import TeamMember
from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team, make_user


def test_team_crud_and_unique_name(client) -> None:
    name = f"team-{suffix()}"
    created = client.post(
        "/api/v1/teams",
        json={"name": name, "budget_nano_usd": 1000},
        headers=BOOTSTRAP_AUTH,
    )
    assert created.status_code == 201, created.text
    team = created.json()
    assert team["model_access"] == "all"
    assert team["budget_nano_usd"] == 1000
    assert "org_id" not in team
    assert (
        client.post("/api/v1/teams", json={"name": name}, headers=BOOTSTRAP_AUTH).status_code == 409
    )
    path = f"/api/v1/teams/{team['id']}"
    assert client.get(path, headers=BOOTSTRAP_AUTH).json() == team
    updated = client.patch(
        path,
        json={"name": f"renamed-{suffix()}", "budget_nano_usd": None},
        headers=BOOTSTRAP_AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["budget_nano_usd"] is None
    assert client.delete(path, headers=BOOTSTRAP_AUTH).status_code == 204
    assert client.get(path, headers=BOOTSTRAP_AUTH).status_code == 404


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
        assert (
            client.post(f"/api/v1/keys/{key.id}/revoke", headers=BOOTSTRAP_AUTH).status_code == 200
        )
    response = client.delete(f"/api/v1/teams/{team.id}", headers=BOOTSTRAP_AUTH)
    assert response.status_code == 409
    assert client.get(f"/api/v1/teams/{team.id}", headers=BOOTSTRAP_AUTH).status_code == 200


async def test_team_delete_cascades_members(client, committed) -> None:
    team = await make_team(committed)
    member = await make_member(committed, team)
    await committed.commit()
    assert client.delete(f"/api/v1/teams/{team.id}", headers=BOOTSTRAP_AUTH).status_code == 204
    assert (
        await committed.scalar(
            select(TeamMember).where(
                TeamMember.team_id == team.id, TeamMember.user_id == member.user_id
            )
        )
        is None
    )

"""UUID cursor pagination for teams, members, service accounts and keys."""

import pytest

from tests.api.helpers import ADMIN_AUTH
from tests.factories import make_key, make_member, make_service_account, make_team


@pytest.mark.parametrize("group", ["teams", "members", "service-accounts", "keys"])
async def test_entity_pagination(client, committed, group) -> None:
    team = await make_team(committed)
    expected = set()
    for _ in range(3):
        if group == "teams":
            row = await make_team(committed)
        elif group == "members":
            row = await make_member(committed, team)
        else:
            row = await make_service_account(committed, team)
            if group == "keys":
                row = await make_key(committed, team, row)
        expected.add(str(row.user_id if group == "members" else row.id))
    await committed.commit()
    path = "/api/v1/teams" if group == "teams" else f"/api/v1/teams/{team.id}/{group}"
    seen = []
    cursor = None
    for _ in range(1000):
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(path, params=params, headers=ADMIN_AUTH)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 2
        seen.extend(r["user_id" if group == "members" else "id"] for r in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("pagination did not terminate")
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen)
    assert expected <= set(seen)
    assert client.get(path, params={"cursor": "nope"}, headers=ADMIN_AUTH).status_code == 422
    for limit in (0, 501):
        assert client.get(path, params={"limit": limit}, headers=ADMIN_AUTH).status_code == 422

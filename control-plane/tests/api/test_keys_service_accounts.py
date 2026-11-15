"""`GET /users/me/keys` can include the service-account keys a caller administers."""

from lumewarp_control_plane.models import TeamRole
from tests.api.helpers import ADMIN_AUTH, auth, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_org,
    make_org_admin,
    make_service_account,
    make_team,
)


async def _list_keys(client, headers, **params) -> list[dict]:
    response = client.get("/api/v1/users/me/keys", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _all_keys(client, headers, **params) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while True:
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        response = client.get("/api/v1/users/me/keys", params=query, headers=headers)
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items


async def test_team_admin_lists_own_and_service_account_keys(client, committed) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    colleague, _ = await user_with_token(committed)
    await make_member(committed, team, colleague)
    own = await make_key(committed, team, admin)
    colleague_key = await make_key(committed, team, colleague)
    account = await make_service_account(committed, team, name="batch-worker")
    sa_key = await make_key(committed, team, account)
    await committed.commit()
    headers = auth(token)

    default = await _list_keys(client, headers)
    assert [(row["id"], row["service_account_name"]) for row in default] == [(str(own.id), None)]

    listed = await _list_keys(client, headers, include_service_accounts="true")
    by_id = {row["id"]: row for row in listed}
    assert set(by_id) == {str(own.id), str(sa_key.id)}
    assert str(colleague_key.id) not in by_id
    assert by_id[str(sa_key.id)]["service_account_name"] == account.name
    assert by_id[str(sa_key.id)]["owner_active"] is None

    # The name is on every key response, not only the own-key list.
    read = client.get(f"/api/v1/keys/{sa_key.id}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["service_account_name"] == account.name
    team_keys = client.get(f"/api/v1/teams/{team.id}/keys", headers=headers)
    assert team_keys.status_code == 200, team_keys.text
    assert {row["id"]: row["service_account_name"] for row in team_keys.json()["items"]} == {
        str(own.id): None,
        str(colleague_key.id): None,
        str(sa_key.id): account.name,
    }


async def test_member_only_sees_service_accounts_of_teams_they_administer(
    client, committed
) -> None:
    member_team = await make_team(committed)
    admin_team = await make_team(committed)
    member, token = await user_with_token(committed)
    await make_member(committed, member_team, member)
    await make_member(committed, admin_team, member, role=TeamRole.ADMIN)
    own = await make_key(committed, member_team, member)
    hidden_account = await make_service_account(committed, member_team)
    hidden_key = await make_key(committed, member_team, hidden_account)
    visible_account = await make_service_account(committed, admin_team)
    visible_key = await make_key(committed, admin_team, visible_account)
    await committed.commit()

    without_flag = await _list_keys(client, auth(token))
    assert [row["id"] for row in without_flag] == [str(own.id)]

    with_flag = await _list_keys(client, auth(token), include_service_accounts="true")
    assert {row["id"] for row in with_flag} == {str(own.id), str(visible_key.id)}
    assert str(hidden_key.id) not in {row["id"] for row in with_flag}


async def test_plain_member_never_sees_service_account_keys(client, committed) -> None:
    team = await make_team(committed)
    member, token = await user_with_token(committed)
    await make_member(committed, team, member)
    own = await make_key(committed, team, member)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account)
    await committed.commit()

    listed = await _list_keys(client, auth(token), include_service_accounts="true")
    assert [row["id"] for row in listed] == [str(own.id)]
    assert str(sa_key.id) != str(own.id)


async def test_org_admin_inherits_service_accounts_of_its_org(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    foreign_team = await make_team(committed)
    org_admin, token = await user_with_token(committed)
    await make_org_admin(committed, org, org_admin)
    account = await make_service_account(committed, team, name="org-worker")
    key = await make_key(committed, team, account)
    foreign_account = await make_service_account(committed, foreign_team)
    foreign_key = await make_key(committed, foreign_team, foreign_account)
    await committed.commit()

    listed = await _list_keys(client, auth(token), include_service_accounts="true")
    assert [(row["id"], row["service_account_name"]) for row in listed] == [
        (str(key.id), "org-worker")
    ]
    assert str(foreign_key.id) not in {row["id"] for row in listed}


async def test_installation_admin_stays_within_me_teams_and_skips_archived(
    client, committed
) -> None:
    live = await make_team(committed)
    archived = await make_team(committed)
    admin, token = await user_with_token(committed, installation_admin=True)
    live_account = await make_service_account(committed, live, name="live-worker")
    live_key = await make_key(committed, live, live_account)
    archived_account = await make_service_account(committed, archived)
    archived_key = await make_key(committed, archived, archived_account)
    await committed.commit()
    headers = auth(token)

    response = client.delete(f"/api/v1/teams/{archived.id}", headers=headers)
    assert response.status_code == 204, response.text
    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200, me.text
    me_teams = {row["team_id"] for row in me.json()["teams"]}

    listed = await _all_keys(client, headers, include_service_accounts="true")
    ids = {row["id"] for row in listed}
    assert str(live_key.id) in ids
    assert str(archived_key.id) not in ids
    # The set is exactly the teams /users/me lists, never every team on the installation.
    assert {row["team_id"] for row in listed} <= me_teams
    assert all(row["service_account_name"] for row in listed)


async def test_archived_team_service_account_keys_disappear_for_team_admin(
    client, committed
) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account)
    await committed.commit()

    response = client.delete(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH)
    assert response.status_code == 204, response.text
    listed = await _list_keys(client, auth(token), include_service_accounts="true")
    assert str(sa_key.id) not in {row["id"] for row in listed}


async def test_own_key_pages_stay_stable_over_mixed_owners(client, committed) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    own = await make_key(committed, team, admin)
    accounts = [await make_service_account(committed, team) for _ in range(3)]
    sa_keys = [await make_key(committed, team, account) for account in accounts]
    await committed.commit()

    listed = await _all_keys(client, auth(token), include_service_accounts="true", limit=1)
    ids = [row["id"] for row in listed]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == {str(own.id), *(str(key.id) for key in sa_keys)}


async def test_break_glass_service_account_list_stays_empty(client, committed) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    await make_key(committed, team, account)
    await committed.commit()

    response = client.get(
        "/api/v1/users/me/keys",
        params={"include_service_accounts": "true"},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}

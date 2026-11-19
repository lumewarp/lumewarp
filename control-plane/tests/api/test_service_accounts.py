"""Service-account CRUD, deactivation and retention of key ownership history."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import TeamRole
from tests.api.helpers import ADMIN_AUTH, auth, put_zone_limits, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team


async def test_service_account_crud_and_team_local_names(client, committed) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/service-accounts"
    created = client.post(
        path,
        json={"name": "batch", "description": "nightly"},
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["team_id"] == str(team.id)
    assert account["model_access"] == "inherit"
    put_zone_limits(
        client,
        "api-test",
        {"team_id": str(team.id), "service_account_id": account["id"]},
        rpm_limit=5,
    )
    account = client.get(f"/api/v1/service-accounts/{account['id']}", headers=ADMIN_AUTH).json()
    assert account["rpm_limit"] == 5
    assert client.post(path, json={"name": "batch"}, headers=ADMIN_AUTH).status_code == 409
    assert (
        client.post(
            f"/api/v1/teams/{other.id}/service-accounts",
            json={"name": "batch"},
            headers=ADMIN_AUTH,
        ).status_code
        == 201
    )
    detail = f"/api/v1/service-accounts/{account['id']}"
    assert client.get(detail, headers=ADMIN_AUTH).json() == account
    assert [r["id"] for r in client.get(path, headers=ADMIN_AUTH).json()["items"]] == [
        account["id"]
    ]
    updated = client.patch(
        detail,
        json={"name": "renamed", "description": ""},
        headers=ADMIN_AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == ""
    put_zone_limits(
        client,
        "api-test",
        {"team_id": str(team.id), "service_account_id": account["id"]},
        rpm_limit=None,
    )
    assert client.get(detail, headers=ADMIN_AUTH).json()["rpm_limit"] is None
    assert client.delete(detail, headers=ADMIN_AUTH).status_code == 204
    assert client.get(detail, headers=ADMIN_AUTH).status_code == 404


@pytest.mark.parametrize("revoke", [False, True])
async def test_service_account_with_key_cannot_be_deleted(client, committed, revoke) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, account)
    await committed.commit()
    if revoke:
        assert client.post(f"/api/v1/keys/{key.id}/revoke", headers=ADMIN_AUTH).status_code == 200
    assert (
        client.delete(f"/api/v1/service-accounts/{account.id}", headers=ADMIN_AUTH).status_code
        == 409
    )


async def test_service_account_creator_is_recorded(client, committed) -> None:
    from lumewarp_control_plane.models import TeamRole

    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/service-accounts",
        json={"name": "ci"},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    assert created.json()["created_by_user_id"] == str(user.id)


async def test_a_team_admin_deactivates_an_account_and_its_keys_leave_the_snapshot(
    client, committed, api_scope
) -> None:
    team = await make_team(committed)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, account)
    await committed.commit()
    headers = auth(token)

    deactivated = client.patch(
        f"/api/v1/service-accounts/{account.id}", json={"is_active": False}, headers=headers
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False
    # A new key for a deactivated account is refused.
    assert (
        client.post(
            f"/api/v1/teams/{team.id}/keys",
            json={"name": "no", "service_account_id": str(account.id)},
            headers=headers,
        ).status_code
        == 409
    )

    async with AsyncSession(committed.bind) as session:
        compiled = await compile_zone(session, api_scope, require_local_membership=True)
    assert str(key.id) not in {row.key_id for row in compiled.snapshot.api_keys}

    reactivated = client.patch(
        f"/api/v1/service-accounts/{account.id}", json={"is_active": True}, headers=headers
    )
    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    async with AsyncSession(committed.bind) as session:
        compiled = await compile_zone(session, api_scope, require_local_membership=True)
    assert str(key.id) in {row.key_id for row in compiled.snapshot.api_keys}

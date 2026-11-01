"""Members and service accounts can only narrow their team's policy."""

import pytest

from tests.api.helpers import ADMIN_AUTH
from tests.factories import catalog_name, make_member, make_service_account, make_team, make_user


async def grant_names(session) -> None:
    for name in ("a", "b"):
        await catalog_name(session, name)


@pytest.mark.parametrize("owner", ["member", "service_account"])
@pytest.mark.parametrize(
    "policy, field",
    [
        ({"model_access": "list", "allowed_models": ["c"]}, "allowed_models"),
        ({"model_access": "all"}, "model_access"),
        ({"rpm_limit": 101}, "rpm_limit"),
        ({"tpm_limit": 1001}, "tpm_limit"),
        ({"max_parallel": 11}, "max_parallel"),
        ({"budget_nano_usd": 1001}, "budget_nano_usd"),
        ({"soft_budget_nano_usd": 501}, "soft_budget_nano_usd"),
    ],
)
async def test_owner_cannot_widen_team(client, committed, owner, policy, field) -> None:
    team = await make_team(
        committed,
        model_access="list",
        allowed_models=["a", "b"],
        rpm_limit=100,
        tpm_limit=1000,
        max_parallel=10,
        budget_nano_usd=1000,
        soft_budget_nano_usd=500,
    )
    user = await make_user(committed)
    await committed.commit()
    if owner == "member":
        path = f"/api/v1/teams/{team.id}/members/{user.id}"
        bad = client.put(path, json={"role": "member", **policy}, headers=ADMIN_AUTH)
        assert client.put(path, json={"role": "member"}, headers=ADMIN_AUTH).status_code == 201
    else:
        path = f"/api/v1/teams/{team.id}/service-accounts"
        bad = client.post(path, json={"name": "bad", **policy}, headers=ADMIN_AUTH)
        created = client.post(path, json={"name": "good"}, headers=ADMIN_AUTH)
        assert created.status_code == 201
        path = f"/api/v1/service-accounts/{created.json()['id']}"
    assert bad.status_code == 422, bad.text
    assert field in bad.json()["error"]["message"]
    updated = client.patch(path, json=policy, headers=ADMIN_AUTH)
    assert updated.status_code == 422, updated.text
    assert field in updated.json()["error"]["message"]
    deny_all = client.patch(
        path, json={"model_access": "list", "allowed_models": []}, headers=ADMIN_AUTH
    )
    assert deny_all.status_code == 200
    assert deny_all.json()["model_access"] == "list"
    assert deny_all.json()["allowed_models"] == []


@pytest.mark.parametrize("owner", ["member", "service_account"])
@pytest.mark.parametrize(
    "owner_policy, team_update, field",
    [
        ({"rpm_limit": 50}, {"rpm_limit": 40}, "rpm_limit"),
    ],
)
async def test_team_narrowing_names_conflicting_owner(
    client, committed, owner, owner_policy, team_update, field
) -> None:
    team = await make_team(committed, rpm_limit=100)
    if owner == "member":
        row = await make_member(committed, team, **owner_policy)
        prefix = f"members.{row.user_id}"
    else:
        row = await make_service_account(committed, team, **owner_policy)
        prefix = f"service_accounts.{row.id}"
    await committed.commit()
    path = f"/api/v1/teams/{team.id}"
    response = client.patch(path, json=team_update, headers=ADMIN_AUTH)
    assert response.status_code == 422, response.text
    assert f"{prefix}.{field}" in response.json()["error"]["message"]
    unchanged = client.get(path, headers=ADMIN_AUTH).json()
    assert unchanged["rpm_limit"] == 100
    assert unchanged["model_access"] == "all"


@pytest.mark.parametrize(
    "body",
    [
        {"model_access": "inherit"},
        {"budget_nano_usd": 100, "soft_budget_nano_usd": 101},
        {"model_access": None},
        {"allowed_models": None},
        {"allowed_models": ["a"]},
    ],
)
def test_team_policy_validation(client, body) -> None:
    from tests.api.helpers import suffix

    response = client.post(
        "/api/v1/teams", json={"name": f"policy-{suffix()}", **body}, headers=ADMIN_AUTH
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("owner", ["member", "service_account"])
async def test_team_narrowing_keeps_owner_models_and_effective_is_intersection(
    client, committed, owner
) -> None:
    team = await make_team(committed)
    await grant_names(committed)
    if owner == "member":
        row = await make_member(committed, team, model_access="list", allowed_models=["a", "b"])
        path = f"/api/v1/teams/{team.id}/members/{row.user_id}"
        listing = f"/api/v1/teams/{team.id}/members"
    else:
        row = await make_service_account(
            committed, team, model_access="list", allowed_models=["a", "b"]
        )
        path = f"/api/v1/service-accounts/{row.id}"
        listing = f"/api/v1/teams/{team.id}/service-accounts"
    await committed.commit()
    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"model_access": "list", "allowed_models": ["a"]},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    # The owner's stored setting survives; `narrow` applies at compile time.
    stored = client.get(listing, headers=ADMIN_AUTH).json()["items"][0]
    assert stored["allowed_models"] == ["a", "b"]
    # Saving unrelated fields does not force the owner to re-narrow.
    assert client.patch(path, json={"rpm_limit": 5}, headers=ADMIN_AUTH).status_code == 200
    # A list that changes must be inside the team's scope.
    widened = client.patch(path, json={"allowed_models": ["b"]}, headers=ADMIN_AUTH)
    assert widened.status_code == 422
    assert "allowed_models" in widened.json()["error"]["message"]


async def test_team_locks_scope_before_validating_owners(client, committed, monkeypatch) -> None:
    from lumewarp_control_plane.api import teams

    team = await make_team(committed, rpm_limit=100)
    await make_member(committed, team, rpm_limit=90)
    await committed.commit()
    original_lock = teams.lock_scope
    original_validate = teams.validate_owners_after_team_update
    calls = []

    async def lock(session, settings):
        await original_lock(session, settings)
        calls.append("locked")

    async def validate(session, row, proposed):
        assert calls == ["locked"]
        calls.append("validate")
        await original_validate(session, row, proposed)

    monkeypatch.setattr(teams, "lock_scope", lock)
    monkeypatch.setattr(teams, "validate_owners_after_team_update", validate)
    response = client.patch(f"/api/v1/teams/{team.id}", json={"rpm_limit": 10}, headers=ADMIN_AUTH)
    assert response.status_code == 422
    assert calls == ["locked", "validate"]

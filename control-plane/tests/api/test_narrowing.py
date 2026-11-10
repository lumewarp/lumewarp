"""Members and service accounts can only narrow their team's policy."""

import pytest

from tests.api.helpers import ADMIN_AUTH, create_org, suffix
from tests.factories import (
    catalog_name,
    make_key,
    make_member,
    make_service_account,
    make_team,
    make_user,
)


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
async def test_parent_decrease_succeeds_and_effective_values_clamp(
    client, committed, owner, api_engine
) -> None:
    """Lowering a team below its child rows is a legitimate write; the minimum applies.

    Only changed values are rechecked, so the old owner row and its limits stay as they
    are and a later unrelated edit still succeeds.
    """
    team = await make_team(committed, rpm_limit=100)
    if owner == "member":
        row = await make_member(committed, team, rpm_limit=50)
        path = f"/api/v1/teams/{team.id}/members/{row.user_id}"
        from lumewarp_control_plane.models import User

        user = await committed.get(User, row.user_id)
        key = await make_key(committed, team, user)
    else:
        row = await make_service_account(committed, team, rpm_limit=50)
        path = f"/api/v1/service-accounts/{row.id}"
        key = await make_key(committed, team, row)
    await committed.commit()
    response = client.patch(f"/api/v1/teams/{team.id}", json={"rpm_limit": 40}, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["rpm_limit"] == 40
    # The child kept its stored setting, as the compiler's minimum expects.
    stored = client.get(path, headers=ADMIN_AUTH) if owner == "service_account" else None
    if stored is not None:
        assert stored.json()["rpm_limit"] == 50
    # An unrelated child edit still succeeds although the stored 50 exceeds the team's 40.
    assert client.patch(path, json={"rpm_limit": 50}, headers=ADMIN_AUTH).status_code == 200

    from sqlalchemy.ext.asyncio import AsyncSession

    from lumewarp_control_plane.compiler import compile_scope

    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_scope(session, "api-test", require_local_membership=True)
    wire = next(k for k in compiled.snapshot.api_keys if k.key_id == str(key.id))
    scopes = {s.id: s.limit.requests_per_minute for s in wire.policy.rate_limit_scopes}
    team_scope = scopes[f"team:{team.id}"]
    owner_scope = next(v for k, v in scopes.items() if k.startswith(("member:", "sa:")))
    assert (team_scope, owner_scope) == (40, 40)


@pytest.mark.parametrize(
    "body",
    [
        {"model_access": "inherit"},
        {"model_access": None},
        {"allowed_models": None},
        {"allowed_models": ["a"]},
    ],
)
def test_team_policy_validation(client, body) -> None:
    org = create_org(client)
    response = client.post(
        "/api/v1/teams",
        json={"name": f"policy-{suffix()}", "org_id": org["id"], **body},
        headers=ADMIN_AUTH,
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
    # Installation admins are listed among members by inheritance; skip them.
    stored = next(
        row
        for row in client.get(listing + "?limit=500", headers=ADMIN_AUTH).json()["items"]
        if row.get("direct", True)
    )
    assert stored["allowed_models"] == ["a", "b"]
    # Saving unrelated fields does not force the owner to re-narrow.
    assert client.patch(path, json={"rpm_limit": 5}, headers=ADMIN_AUTH).status_code == 200
    # A list that changes must be inside the team's scope.
    widened = client.patch(path, json={"allowed_models": ["b"]}, headers=ADMIN_AUTH)
    assert widened.status_code == 422
    assert "allowed_models" in widened.json()["error"]["message"]


async def test_team_locks_scope_before_validation(client, committed, monkeypatch) -> None:
    from lumewarp_control_plane.api import teams

    team = await make_team(committed, rpm_limit=100)
    await committed.commit()
    original_lock = teams.lock_scope
    original_validate = teams.validate_team_policy
    calls = []

    async def lock(session, settings):
        await original_lock(session, settings)
        calls.append("locked")

    async def validate(session, row, proposed, base):
        assert calls == ["locked"]
        calls.append("validate")
        await original_validate(session, row, proposed, base)

    monkeypatch.setattr(teams, "lock_scope", lock)
    monkeypatch.setattr(teams, "validate_team_policy", validate)
    response = client.patch(f"/api/v1/teams/{team.id}", json={"rpm_limit": 10}, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert calls == ["locked", "validate"]

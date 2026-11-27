"""Members and service accounts can only narrow their team's policy."""

import pytest

from tests.api.helpers import ADMIN_AUTH, create_org, suffix
from tests.factories import (
    catalog_name,
    make_key,
    make_member,
    make_model,
    make_org,
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
    [({"model_access": "list", "allowed_models": ["c"]}, "allowed_models")],
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
@pytest.mark.parametrize(
    "field, value, parent_value",
    [
        ("rpm_limit", 101, 100),
        ("tpm_limit", 1001, 1000),
        ("max_parallel", 11, 10),
        ("budget_nano_usd", 1001, 1000),
    ],
)
async def test_owner_limits_cannot_widen_team(
    client, committed, api_scope, owner, field, value, parent_value
) -> None:
    """Limits live in `zone_limits` now; a child's own value still may not exceed the team's."""
    team = await make_team(
        committed,
        rpm_limit=100,
        tpm_limit=1000,
        max_parallel=10,
        budget_nano_usd=1000,
    )
    user = await make_user(committed)
    await committed.commit()
    if owner == "member":
        await make_member(committed, team, user)
        await committed.commit()
        reference = {"team_id": str(team.id), "user_id": str(user.id)}
    else:
        account = await make_service_account(committed, team)
        await committed.commit()
        reference = {"team_id": str(team.id), "service_account_id": str(account.id)}
    path = f"/api/v1/zones/{api_scope}/limits"
    bad = client.put(path, json={**reference, field: value}, headers=ADMIN_AUTH)
    assert bad.status_code == 422, bad.text
    assert field in bad.json()["error"]["message"]
    good = client.put(path, json={**reference, field: parent_value}, headers=ADMIN_AUTH)
    assert good.status_code == 200, good.text
    assert good.json()[field] == parent_value


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
    response = client.put(
        "/api/v1/zones/api-test/limits",
        json={"team_id": str(team.id), "rpm_limit": 40},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["rpm_limit"] == 40
    # The child kept its stored setting, as the compiler's minimum expects.
    if owner == "service_account":
        stored = client.get(path, headers=ADMIN_AUTH).json()
    else:
        stored = next(
            item
            for item in client.get(
                f"/api/v1/teams/{team.id}/members?limit=500", headers=ADMIN_AUTH
            ).json()["items"]
            if item["user_id"] == str(row.user_id)
        )
    assert stored["rpm_limit"] == 50
    # An unrelated child edit still succeeds although the stored 50 exceeds the team's 40.
    reference = (
        {"team_id": str(team.id), "service_account_id": str(row.id)}
        if owner == "service_account"
        else {"team_id": str(team.id), "user_id": str(row.user_id)}
    )
    assert (
        client.put(
            "/api/v1/zones/api-test/limits",
            json={**reference, "rpm_limit": 50},
            headers=ADMIN_AUTH,
        ).status_code
        == 200
    )

    from sqlalchemy.ext.asyncio import AsyncSession

    from lumewarp_control_plane.compiler import compile_zone

    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_zone(session, "api-test", require_local_membership=True)
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
    owner = (
        {"team_id": str(team.id), "service_account_id": str(row.id)}
        if owner == "service_account"
        else {"team_id": str(team.id), "user_id": str(row.user_id)}
    )
    assert (
        client.put(
            "/api/v1/zones/api-test/limits",
            json={**owner, "rpm_limit": 5},
            headers=ADMIN_AUTH,
        ).status_code
        == 200
    )
    # A list that changes must be inside the team's scope.
    widened = client.patch(path, json={"allowed_models": ["b"]}, headers=ADMIN_AUTH)
    assert widened.status_code == 422
    assert "allowed_models" in widened.json()["error"]["message"]


async def test_team_locks_scope_before_validation(client, committed, monkeypatch) -> None:
    from lumewarp_control_plane.api import zone_limits

    team = await make_team(committed, rpm_limit=100)
    await committed.commit()
    original_lock = zone_limits.lock_hierarchy
    original_validate = zone_limits.check_narrowing
    calls = []

    async def lock(session):
        await original_lock(session)
        calls.append("locked")

    def validate(parent, proposed, **kwargs):
        assert calls == ["locked"]
        calls.append("validate")
        return original_validate(parent, proposed, **kwargs)

    monkeypatch.setattr(zone_limits, "lock_hierarchy", lock)
    monkeypatch.setattr(zone_limits, "check_narrowing", validate)
    response = client.put(
        "/api/v1/zones/api-test/limits",
        json={"team_id": str(team.id), "rpm_limit": 10},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    assert calls == ["locked", "validate"]


async def test_all_below_the_root_is_everything_the_parent_allows(
    client, committed, api_engine
) -> None:
    """A team (or an owner) at `all` under a restricted parent is accepted and resolves to the
    parent's set: switching a listed team back to `all` after its org was restricted saves.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from lumewarp_control_plane.compiler import compile_zone

    a = (await make_model(committed, f"all-{suffix()}")).name
    b = (await make_model(committed, f"all-{suffix()}")).name
    org = await make_org(committed)
    team = await make_team(committed, org=org, model_access="list", allowed_models=[a])
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    await committed.commit()
    group = client.post(
        "/api/v1/model-groups", json={"name": f"g-{suffix()}", "members": [a]}, headers=ADMIN_AUTH
    ).json()
    restricted = client.patch(
        f"/api/v1/orgs/{org.id}",
        json={"model_access": "list", "allowed_groups": [group["name"]]},
        headers=ADMIN_AUTH,
    )
    assert restricted.status_code == 200, restricted.text

    for body in (
        {"model_access": "all", "allowed_models": [], "allowed_groups": []},
        {"mcp_access": "all", "a2a_access": "all"},
    ):
        saved = client.patch(f"/api/v1/teams/{team.id}", json=body, headers=ADMIN_AUTH)
        assert saved.status_code == 200, saved.text
    team_out = client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).json()
    assert team_out["model_access"] == "all"
    assert team_out["effective_models"] == [a]
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_zone(session, "api-test", require_local_membership=True)
    published = next(k for k in compiled.snapshot.api_keys if k.key_id == str(key.id))
    assert (published.policy.allow_all_models, list(published.policy.allowed_models)) == (
        False,
        [a],
    )
    # Listing a name outside the org is still refused.
    outside = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"model_access": "list", "allowed_models": [b]},
        headers=ADMIN_AUTH,
    )
    assert outside.status_code == 422, outside.text

"""HTTP acceptance for the org -> team -> owner -> key quota hierarchy."""

from datetime import UTC, datetime

import pytest

from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.models import ModelAccess, ScopeType, TeamRole, member_scope_id
from lumewarp_control_plane.windows import month_window
from tests.api.helpers import ADMIN_AUTH, auth, suffix, usage_total, user_with_token
from tests.factories import make_key, make_member, make_org, make_team, make_user


async def test_team_admin_cannot_set_team_totals_but_sets_member_defaults(
    client, committed
) -> None:
    team = await make_team(committed, rpm_limit=100)
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    await committed.commit()
    headers = auth(token)
    path = f"/api/v1/teams/{team.id}"

    # Team totals and models are org level: a team admin may not touch them.
    for body in ({"rpm_limit": 200}, {"model_access": "list"}, {"budget_nano_usd": 10}):
        response = client.patch(path, json=body, headers=headers)
        assert response.status_code == 403, (body, response.text)
    # Member defaults are team settings.
    response = client.patch(path, json={"member_rpm_default": 10}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["member_rpm_default"] == 10
    # A changed default may not exceed the team's own effective limit.
    assert client.patch(path, json={"member_rpm_default": 200}, headers=headers).status_code == 422
    # A form that only resends the current values still works for a team admin.
    assert client.patch(path, json={"rpm_limit": 100}, headers=headers).status_code == 200


async def test_member_key_limits_are_capped_by_their_own_limit(client, committed) -> None:
    team = await make_team(committed, rpm_limit=100)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, rpm_limit=20)
    await committed.commit()
    headers = auth(token)
    path = f"/api/v1/teams/{team.id}/keys"

    above = client.post(path, json={"name": "above", "rpm_limit": 21}, headers=headers)
    assert above.status_code == 422, above.text
    assert "rpm_limit" in above.json()["error"]["message"]
    created = client.post(path, json={"name": "own", "rpm_limit": 10}, headers=headers)
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]

    # An administrator lowers the key; the owner may raise it back within their own limit.
    lowered = client.patch(f"/api/v1/keys/{key_id}", json={"rpm_limit": 5}, headers=ADMIN_AUTH)
    assert lowered.status_code == 200, lowered.text
    raised = client.patch(f"/api/v1/keys/{key_id}", json={"rpm_limit": 20}, headers=headers)
    assert raised.status_code == 200, raised.text
    assert raised.json()["rpm_limit"] == 20
    too_high = client.patch(f"/api/v1/keys/{key_id}", json={"rpm_limit": 21}, headers=headers)
    assert too_high.status_code == 422, too_high.text


async def test_limits_patch_keeps_lifecycle_and_reason(client, committed) -> None:
    team = await make_team(committed, rpm_limit=100)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, rpm_limit=20)
    key = await make_key(committed, team, user)
    await committed.commit()
    headers = auth(token)
    suspended = client.patch(
        f"/api/v1/keys/{key.id}", json={"lifecycle": "suspended"}, headers=headers
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["suspended_reason"] == "owner"
    patched = client.patch(f"/api/v1/keys/{key.id}", json={"rpm_limit": 7}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert (patched.json()["lifecycle"], patched.json()["suspended_reason"]) == (
        "suspended",
        "owner",
    )
    assert patched.json()["rpm_limit"] == 7


async def test_me_remaining_budget_includes_the_org_level(client, committed) -> None:
    org = await make_org(committed, f"org-{suffix()}", budget_nano_usd=1000)
    team = await make_team(committed, org=org, budget_nano_usd=1000)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, budget_nano_usd=1000)
    window_start, _ = month_window(datetime.now(UTC))
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=window_start, cost_nano_usd=900)
    )
    committed.add(
        usage_total(
            ScopeType.MEMBER,
            member_scope_id(team.id, user.id),
            window_start=window_start,
            cost_nano_usd=50,
        )
    )
    await committed.commit()

    response = client.get("/api/v1/users/me", headers=auth(token))
    assert response.status_code == 200, response.text
    [row] = response.json()["teams"]
    # The org's derived spend (its teams' totals) and the team's are both 900; the member
    # has spent 50 of 1000. The remaining amount is the minimum of the three.
    assert row["budget"]["remaining_nano_usd"] == 100
    # A plain member does not see the org's own budget numbers.
    assert (row["budget"]["org_budget_nano_usd"], row["budget"]["org_spent_nano_usd"]) == (
        None,
        None,
    )


async def test_me_org_and_team_scopes_are_visible_to_a_team_admin(client, committed) -> None:
    org = await make_org(committed, f"org-{suffix()}", budget_nano_usd=5000)
    team = await make_team(committed, org=org, budget_nano_usd=1000)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    window_start, _ = month_window(datetime.now(UTC))
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=window_start, cost_nano_usd=300)
    )
    await committed.commit()
    response = client.get("/api/v1/users/me", headers=auth(token))
    assert response.status_code == 200, response.text
    [row] = response.json()["teams"]
    budget = row["budget"]
    assert (budget["org_budget_nano_usd"], budget["org_spent_nano_usd"]) == (5000, 300)
    assert (budget["team_budget_nano_usd"], budget["team_spent_nano_usd"]) == (1000, 300)
    assert budget["remaining_nano_usd"] == 700


async def test_external_backend_without_member_rows_gets_defaults_and_owner_scope(
    client, committed, monkeypatch, api_engine
) -> None:
    """No `team_members` row at all: team member defaults still cap the member's key."""
    team = await make_team(committed, rpm_limit=100, member_rpm_default=10)
    user, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if resource.type == "team" and resource.id == str(team.id):
            return Decision.ALLOW if relation is Relation.MEMBER else Decision.DENY
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    headers = auth(token)
    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200, me.text
    row = me.json()["teams"][0]
    assert row["limits"]["rpm_limit"] == 10
    assert row["all_models"] is True

    path = f"/api/v1/teams/{team.id}/keys"
    above = client.post(path, json={"name": "above", "rpm_limit": 11}, headers=headers)
    assert above.status_code == 422, above.text
    created = client.post(path, json={"name": "ok", "rpm_limit": 10}, headers=headers)
    assert created.status_code == 201, created.text

    from sqlalchemy.ext.asyncio import AsyncSession

    from lumewarp_control_plane.compiler import compile_scope

    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_scope(session, "api-test", require_local_membership=False)
    wire = next(k for k in compiled.snapshot.api_keys if k.key_id == created.json()["key"]["id"])
    ids = [(scope.id, scope.limit.requests_per_minute) for scope in wire.policy.rate_limit_scopes]
    assert ids == [
        (f"team:{team.id}", 100),
        (f"member:{member_scope_id(team.id, user.id)}", 10),
        (f"key:{created.json()['key']['id']}", 10),
    ]


async def test_org_model_list_narrows_a_team_change(client, committed) -> None:
    org = await make_org(committed, f"org-{suffix()}", model_access=ModelAccess.LIST)
    team = await make_team(committed, org=org, model_access=ModelAccess.LIST)
    await committed.commit()
    # The org currently allows nothing; a team cannot widen beyond it.
    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"model_access": "list", "allowed_models": ["a"]},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 422, response.text
    assert "allowed_models" in response.json()["error"]["message"]


@pytest.mark.parametrize("owner_kind", ["member", "service_account"])
async def test_admin_key_limits_are_capped_by_the_owner(client, committed, owner_kind) -> None:
    team = await make_team(committed, rpm_limit=100)
    user = await make_user(committed)
    if owner_kind == "member":
        await make_member(committed, team, user, rpm_limit=20)
        owner_id = user.id
        key = await make_key(committed, team, user)
    else:
        from tests.factories import make_service_account

        account = await make_service_account(committed, team, rpm_limit=20)
        owner_id = account.id
        key = await make_key(committed, team, account)
    await committed.commit()
    response = client.patch(f"/api/v1/keys/{key.id}", json={"rpm_limit": 21}, headers=ADMIN_AUTH)
    assert response.status_code == 422, (owner_kind, owner_id, response.text)


async def test_key_limit_patch_checks_only_changed_values(client, committed) -> None:
    """After an owner decrease, another limit (or the unchanged one) still updates."""
    team = await make_team(committed, rpm_limit=100)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, rpm_limit=50)
    key = await make_key(committed, team, user, rpm_limit=40)
    await committed.commit()
    headers = auth(token)
    path = f"/api/v1/keys/{key.id}"

    # A parent decrease always succeeds and leaves the key's stored value untouched.
    lowered = client.patch(
        f"/api/v1/teams/{team.id}/members/{user.id}",
        json={"rpm_limit": 10},
        headers=ADMIN_AUTH,
    )
    assert lowered.status_code == 200, lowered.text

    # A different limit is the only change: the stale stored rpm 40 is not rechecked.
    other = client.patch(path, json={"tpm_limit": 500}, headers=headers)
    assert other.status_code == 200, other.text
    assert (other.json()["rpm_limit"], other.json()["tpm_limit"]) == (40, 500)
    # Resending the stored value is not a change either.
    same = client.patch(path, json={"rpm_limit": 40}, headers=headers)
    assert same.status_code == 200, same.text
    # An actual change above the new owner cap is still rejected, for the owner and admin.
    for who in (headers, ADMIN_AUTH):
        raised = client.patch(path, json={"rpm_limit": 11}, headers=who)
        assert raised.status_code == 422, raised.text
        assert "rpm_limit" in raised.json()["error"]["message"]


async def test_member_put_resending_a_value_survives_parent_decrease(client, committed) -> None:
    """A PUT that resends an unchanged quota value is not rechecked against the new team."""
    team = await make_team(committed, rpm_limit=100)
    user = await make_user(committed)
    await make_member(committed, team, user, rpm_limit=50)
    await committed.commit()
    lowered = client.patch(f"/api/v1/teams/{team.id}", json={"rpm_limit": 20}, headers=ADMIN_AUTH)
    assert lowered.status_code == 200, lowered.text
    path = f"/api/v1/teams/{team.id}/members/{user.id}"
    response = client.put(path, json={"role": "admin", "rpm_limit": 50}, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert (response.json()["role"], response.json()["rpm_limit"]) == ("admin", 50)
    # A value the PUT actually changes is still checked against the parent.
    raised = client.put(path, json={"role": "admin", "rpm_limit": 60}, headers=ADMIN_AUTH)
    assert raised.status_code == 422, raised.text
    assert "rpm_limit" in raised.json()["error"]["message"]

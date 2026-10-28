"""Admin API paths that update control-plane metrics."""

from lumewarp_control_plane.models import KeyLifecycle
from tests.api.helpers import BOOTSTRAP_AUTH, auth, user_with_token
from tests.factories import make_key, make_member, make_team


def _suspended(client, reason: str) -> float:
    return client.app.state.metrics.value("lumewarp_cp_keys_suspended_total", {"reason": reason})


async def test_key_patch_counts_active_to_suspended_only(client, committed) -> None:
    team = await make_team(committed)
    owner, owner_token = await user_with_token(committed)
    await make_member(committed, team, owner)
    by_admin = await make_key(committed, team, owner)
    by_owner = await make_key(committed, team, owner)
    await committed.commit()

    path = f"/api/v1/keys/{by_admin.id}"
    assert client.patch(path, json={"lifecycle": "suspended"}, headers=BOOTSTRAP_AUTH).is_success
    assert _suspended(client, "admin") == 1
    # Already suspended: no transition, no count.
    assert client.patch(path, json={"lifecycle": "suspended"}, headers=BOOTSTRAP_AUTH).is_success
    assert _suspended(client, "admin") == 1

    path = f"/api/v1/keys/{by_owner.id}"
    response = client.patch(path, json={"lifecycle": "suspended"}, headers=auth(owner_token))
    assert response.json()["suspended_reason"] == "owner"
    assert _suspended(client, "owner") == 1
    # An unauthenticated request commits nothing and counts nothing.
    assert client.patch(path, json={"lifecycle": "suspended", "name": None}).status_code == 401
    assert _suspended(client, "owner") == 1


async def test_member_removal_and_deactivation_count_suspended_keys(client, committed) -> None:
    team = await make_team(committed)
    other_team = await make_team(committed)
    removed, _ = await user_with_token(committed)
    await make_member(committed, team, removed)
    await make_key(committed, team, removed)
    await make_key(committed, team, removed)
    await make_key(committed, team, removed, lifecycle=KeyLifecycle.SUSPENDED)
    deactivated, _ = await user_with_token(committed)
    await make_member(committed, team, deactivated)
    await make_member(committed, other_team, deactivated)
    await make_key(committed, team, deactivated)
    await make_key(committed, other_team, deactivated)
    await committed.commit()

    path = f"/api/v1/teams/{team.id}/members/{removed.id}"
    assert client.delete(path, headers=BOOTSTRAP_AUTH).status_code == 204
    # Only the two active keys changed state.
    assert _suspended(client, "owner_removed") == 2

    path = f"/api/v1/users/{deactivated.id}"
    assert client.patch(path, json={"is_active": False}, headers=BOOTSTRAP_AUTH).is_success
    assert _suspended(client, "owner_deactivated") == 2


async def test_api_authorization_checks_are_counted(client, committed) -> None:
    team = await make_team(committed)
    member, token = await user_with_token(committed)
    await make_member(committed, team, member)
    outsider, outsider_token = await user_with_token(committed)
    await committed.commit()
    metrics = client.app.state.metrics

    def checks(decision: str) -> float:
        return metrics.value(
            "lumewarp_cp_authz_checks_total", {"backend": "builtin", "decision": decision}
        )

    assert client.get(f"/api/v1/teams/{team.id}", headers=auth(token)).status_code == 200
    allowed, denied = checks("allow"), checks("deny")
    assert allowed >= 1
    assert client.get(f"/api/v1/teams/{team.id}", headers=auth(outsider_token)).status_code == 404
    assert checks("deny") > denied
    assert checks("error") == 0

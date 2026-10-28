"""Members can read their teams' audit rows, without installation rows or secrets."""

import json
import uuid

from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix, user_with_token
from tests.factories import make_member, make_team


async def test_audit_visibility_filters_pagination_and_redaction(client, committed) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": "audited"}, headers=auth(token)
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]
    secret = created.json()["secret"]
    assert (
        client.patch(
            f"/api/v1/keys/{key_id}", json={"name": "renamed"}, headers=auth(token)
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/teams/{other.id}", json={"name": f"other-{suffix()}"}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/users", json={"email": f"audit-{suffix()}@example.com"}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 201
    )
    rows = client.get("/api/v1/audit?limit=500", headers=auth(token))
    assert rows.status_code == 200
    assert {r["team_id"] for r in rows.json()["items"]} == {str(team.id)}
    assert {r["action"] for r in rows.json()["items"]} == {"key.create", "key.update"}
    assert secret not in rows.text and "key_hash" not in rows.text
    filters = {
        "team_id": str(team.id),
        "actor_id": str(user.id),
        "action": "key.create",
        "resource_type": "key",
        "resource_id": key_id,
    }
    filtered = client.get("/api/v1/audit", params=filters, headers=auth(token))
    assert filtered.status_code == 200
    assert len(filtered.json()["items"]) == 1
    assert "org_id" not in filtered.json()["items"][0]
    assert secret not in json.dumps(filtered.json()["items"][0]["details"])
    first = client.get(
        "/api/v1/audit", params={"team_id": str(team.id), "limit": 1}, headers=auth(token)
    ).json()
    second = client.get(
        "/api/v1/audit",
        params={"team_id": str(team.id), "limit": 1, "cursor": first["next_cursor"]},
        headers=auth(token),
    ).json()
    assert first["items"][0]["id"] > second["items"][0]["id"]
    for team_id in (other.id, uuid.uuid4()):
        assert (
            client.get(
                "/api/v1/audit", params={"team_id": str(team_id)}, headers=auth(token)
            ).status_code
            == 404
        )
    all_rows = client.get("/api/v1/audit?limit=500", headers=BOOTSTRAP_AUTH).json()["items"]
    assert any(r["team_id"] is None for r in all_rows)
    assert any(r["team_id"] == str(other.id) for r in all_rows)
    assert client.get("/api/v1/audit?cursor=nope", headers=auth(token)).status_code == 422

"""Audit reads are scoped and never expose secrets."""

from __future__ import annotations

import json

from tests.api.helpers import (
    BOOTSTRAP_AUTH,
    auth,
    grant,
    key_row,
    org_tree,
    suffix,
    user_with_token,
)


async def test_audit_scope_and_redaction(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="audit")
    key = await key_row(committed, project, name="audit-key")
    admin, admin_secret = await user_with_token(committed)
    team_admin, team_admin_secret = await user_with_token(committed)
    member, member_secret = await user_with_token(committed)
    await grant(committed, admin, org, role="admin")
    await grant(committed, team_admin, org, team=team, role="admin")
    await grant(committed, member, org, team=team, role="member")
    await committed.commit()

    # Mutations through the API write audit rows in the same transaction.
    resp = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"name": f"team-{suffix()}"},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200, resp.text
    created_key = client.post(
        f"/api/v1/projects/{project.id}/keys",
        json={"name": "audited-key"},
        headers=BOOTSTRAP_AUTH,
    )
    assert created_key.status_code == 201, created_key.text
    secret = created_key.json()["secret"]
    assert secret.startswith("lw-")

    # Org admins read the whole org; team admins their team; members nothing.
    org_rows = client.get(f"/api/v1/audit?org_id={org.id}", headers=auth(admin_secret))
    assert org_rows.status_code == 200, org_rows.text
    assert any(row["action"] == "team.update" for row in org_rows.json()["items"])
    team_rows = client.get(
        f"/api/v1/audit?org_id={org.id}&team_id={team.id}",
        headers=auth(team_admin_secret),
    )
    assert team_rows.status_code == 200
    assert any(row["action"] == "team.update" for row in team_rows.json()["items"])
    assert (
        client.get(f"/api/v1/audit?org_id={org.id}", headers=auth(team_admin_secret)).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/audit?org_id={org.id}", headers=auth(member_secret)).status_code == 403
    )
    assert (
        client.get(
            f"/api/v1/audit?org_id={org.id}&team_id={team.id}",
            headers=auth(member_secret),
        ).status_code
        == 403
    )

    key_rows = client.get(
        f"/api/v1/audit?org_id={org.id}&resource_type=key&action=key.create",
        headers=BOOTSTRAP_AUTH,
    )
    assert key_rows.status_code == 200
    key_audit = next(
        row
        for row in key_rows.json()["items"]
        if row["resource_id"] == str(created_key.json()["key"]["id"])
    )
    encoded = json.dumps(key_audit["details"])
    assert secret not in encoded
    assert "key_hash" not in encoded
    assert key.display_prefix not in encoded

    # Pagination is newest-first and stable.
    first = client.get(f"/api/v1/audit?org_id={org.id}&limit=1", headers=BOOTSTRAP_AUTH)
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(
        f"/api/v1/audit?org_id={org.id}&limit=1&cursor={cursor}",
        headers=BOOTSTRAP_AUTH,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]

    # Installation-wide rows are superuser-only; an explicit org filter must exist.
    assert client.get("/api/v1/audit", headers=auth(admin_secret)).status_code == 200
    assert (
        client.get(
            "/api/v1/audit?org_id=00000000-0000-0000-0000-000000000000", headers=BOOTSTRAP_AUTH
        ).status_code
        == 404
    )

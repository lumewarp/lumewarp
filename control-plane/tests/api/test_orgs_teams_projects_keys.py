"""CRUD and policy plumbing for orgs, teams, projects and keys."""

from __future__ import annotations

from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix


def _org(client, **overrides):
    body = {"name": f"org-{suffix()}"}
    body.update(overrides)
    resp = client.post("/api/v1/orgs", json=body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _team(client, org_id, **overrides):
    body = {"name": f"team-{suffix()}"}
    body.update(overrides)
    resp = client.post(f"/api/v1/orgs/{org_id}/teams", json=body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _project(client, team_id, **overrides):
    body = {"name": f"proj-{suffix()}"}
    body.update(overrides)
    resp = client.post(f"/api/v1/teams/{team_id}/projects", json=body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _key(client, project_id, **overrides):
    body = {"name": f"key-{suffix()}"}
    body.update(overrides)
    resp = client.post(f"/api/v1/projects/{project_id}/keys", json=body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_org_team_project_key_crud(client) -> None:
    org = _org(client, model_access="all", budget_nano_usd=10_000)
    assert org["model_access"] == "all"
    assert org["budget_nano_usd"] == 10_000

    team = _team(client, org["id"], model_access="list", allowed_models=["a", "b"])
    assert team["org_id"] == org["id"]
    assert team["allowed_models"] == ["a", "b"]

    project = _project(
        client,
        team["id"],
        model_access="list",
        allowed_models=["a"],
        rpm_limit=10,
        budget_nano_usd=500,
    )
    assert project["team_id"] == team["id"]
    assert project["rpm_limit"] == 10

    created = _key(
        client,
        project["id"],
        model_access="list",
        allowed_models=["a"],
        rpm_limit=5,
        expires_at="2099-01-01T00:00:00Z",
    )
    key = created["key"]
    assert created["secret"].startswith("lw-")
    assert key["display_prefix"] == created["secret"][:9]
    assert key["lifecycle"] == "active"
    assert key["allowed_models"] == ["a"]
    assert "secret" not in key
    assert "key_hash" not in key

    # Detail reads never return the secret again.
    resp = client.get(f"/api/v1/keys/{key['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200
    assert "secret" not in resp.text
    assert resp.json()["display_prefix"] == key["display_prefix"]

    # Lists are scoped and paginated.
    resp = client.get(f"/api/v1/orgs/{org['id']}/teams", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [team["id"]]
    resp = client.get(f"/api/v1/teams/{team['id']}/projects", headers=BOOTSTRAP_AUTH)
    assert [item["id"] for item in resp.json()["items"]] == [project["id"]]
    resp = client.get(f"/api/v1/projects/{project['id']}/keys", headers=BOOTSTRAP_AUTH)
    assert [item["id"] for item in resp.json()["items"]] == [key["id"]]

    # Update policy and name.
    resp = client.patch(
        f"/api/v1/keys/{key['id']}",
        json={"name": "renamed", "rpm_limit": 4},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "renamed"
    assert resp.json()["rpm_limit"] == 4

    # Revoke is terminal and idempotent.
    resp = client.post(f"/api/v1/keys/{key['id']}/revoke", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle"] == "revoked"
    assert resp.json()["revoked_at"] is not None
    resp = client.post(f"/api/v1/keys/{key['id']}/revoke", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200
    resp = client.patch(f"/api/v1/keys/{key['id']}", json={"name": "nope"}, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 409

    # Deleting a parent with children is a conflict, not a cascade.
    resp = client.delete(f"/api/v1/projects/{project['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 409
    resp = client.delete(f"/api/v1/teams/{team['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 409
    resp = client.delete(f"/api/v1/orgs/{org['id']}", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 409

    # Empty parents can be deleted.
    empty_org = _org(client)
    empty_team = _team(client, empty_org["id"])
    empty_project = _project(client, empty_team["id"])
    assert (
        client.delete(f"/api/v1/projects/{empty_project['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/teams/{empty_team['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/orgs/{empty_org['id']}", headers=BOOTSTRAP_AUTH).status_code == 204
    )


def test_nested_parent_ids_must_match(client) -> None:
    org = _org(client)
    other = _org(client)

    resp = client.post(
        f"/api/v1/orgs/{org['id']}/teams",
        json={"name": f"t-{suffix()}", "org_id": other["id"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 404

    team = _team(client, org["id"])
    resp = client.post(
        f"/api/v1/teams/{team['id']}/projects",
        json={"name": f"p-{suffix()}", "team_id": org["id"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 404

    project = _project(client, team["id"])
    resp = client.post(
        f"/api/v1/projects/{project['id']}/keys",
        json={"name": f"k-{suffix()}", "project_id": org["id"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 404


async def test_unknown_writes_are_role_checked_and_404(client, committed) -> None:
    # A random UUID must not leak existence to an ordinary principal.
    import uuid

    from tests.api.helpers import grant, org_tree, user_with_token

    org, team, _ = await org_tree(committed, prefix="rbac-parent")
    user, secret = await user_with_token(committed)
    await grant(committed, user, org, team=team, role="viewer")
    await committed.commit()
    missing = uuid.uuid4()
    for path in (
        f"/api/v1/orgs/{missing}",
        f"/api/v1/teams/{missing}",
        f"/api/v1/projects/{missing}",
        f"/api/v1/keys/{missing}",
    ):
        resp = client.get(path, headers=auth(secret))
        assert resp.status_code == 404, (path, resp.text)

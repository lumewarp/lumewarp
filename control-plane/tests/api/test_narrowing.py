"""Children may narrow their parents; widening is a 422 with a field path."""

from __future__ import annotations

from tests.api.helpers import BOOTSTRAP_AUTH, suffix


def _org(client, **body):
    body = {"name": f"org-{suffix()}", **body}
    resp = client.post("/api/v1/orgs", json=body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _team(client, org_id, **body):
    body = {"name": f"team-{suffix()}", **body}
    resp = client.post(f"/api/v1/orgs/{org_id}/teams", json=body, headers=BOOTSTRAP_AUTH)
    return resp


def _project(client, team_id, **body):
    body = {"name": f"proj-{suffix()}", **body}
    resp = client.post(f"/api/v1/teams/{team_id}/projects", json=body, headers=BOOTSTRAP_AUTH)
    return resp


def _key(client, project_id, **body):
    body = {"name": f"key-{suffix()}", **body}
    resp = client.post(f"/api/v1/projects/{project_id}/keys", json=body, headers=BOOTSTRAP_AUTH)
    return resp


def test_allowlist_subset_and_deny_all(client) -> None:
    org = _org(client, model_access="list", allowed_models=["a", "b"])
    assert _team(client, org["id"], model_access="list", allowed_models=["a"]).status_code == 201
    bad = _team(client, org["id"], model_access="list", allowed_models=["c"])
    assert bad.status_code == 422
    assert "allowed_models" in bad.json()["error"]["message"]
    assert "c" in bad.json()["error"]["message"]

    team = _team(client, org["id"], model_access="list", allowed_models=["a", "b"]).json()
    empty = _project(client, team["id"], model_access="list", allowed_models=[])
    assert empty.status_code == 201
    assert empty.json()["allowed_models"] == []
    assert empty.json()["model_access"] == "list"

    bad_all = _project(client, team["id"], model_access="all")
    assert bad_all.status_code == 422
    assert "model_access" in bad_all.json()["error"]["message"]


def test_inherit_chain_is_narrowed_by_the_root(client) -> None:
    org = _org(client, model_access="all")
    team = _team(client, org["id"], model_access="inherit").json()
    project = _project(client, team["id"], model_access="list", allowed_models=["a"]).json()
    assert project["allowed_models"] == ["a"]
    key = _key(client, project["id"], model_access="inherit")
    assert key.status_code == 201
    assert key.json()["key"]["allowed_models"] == []


def test_limits_and_budgets_must_not_exceed_parent(client) -> None:
    org = _org(client, rpm_limit=100, tpm_limit=1000, max_parallel=10, budget_nano_usd=1000)
    bad = _team(client, org["id"], rpm_limit=101)
    assert bad.status_code == 422
    assert "rpm_limit" in bad.json()["error"]["message"]
    bad = _team(client, org["id"], tpm_limit=1001)
    assert bad.status_code == 422
    assert "tpm_limit" in bad.json()["error"]["message"]
    bad = _team(client, org["id"], max_parallel=11)
    assert bad.status_code == 422
    assert "max_parallel" in bad.json()["error"]["message"]
    bad = _team(client, org["id"], budget_nano_usd=1001)
    assert bad.status_code == 422
    assert "budget_nano_usd" in bad.json()["error"]["message"]

    team = _team(client, org["id"], rpm_limit=50, budget_nano_usd=500).json()
    bad = _project(client, team["id"], rpm_limit=51)
    assert bad.status_code == 422
    bad = _project(client, team["id"], budget_nano_usd=501)
    assert bad.status_code == 422
    project = _project(client, team["id"], rpm_limit=50, budget_nano_usd=500).json()
    bad = _key(client, project["id"], rpm_limit=51)
    assert bad.status_code == 422
    assert _key(client, project["id"], rpm_limit=1).status_code == 201


def test_soft_budget_cannot_exceed_hard_budget(client) -> None:
    resp = client.post(
        "/api/v1/orgs",
        json={
            "name": f"org-{suffix()}",
            "budget_nano_usd": 100,
            "soft_budget_nano_usd": 101,
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    assert "soft_budget_nano_usd" in resp.json()["error"]["message"]


def test_org_cannot_inherit(client) -> None:
    resp = client.post(
        "/api/v1/orgs",
        json={"name": f"org-{suffix()}", "model_access": "inherit"},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    assert "model_access" in resp.json()["error"]["message"]


def test_parent_update_cannot_strand_children(client) -> None:
    # org ALL -> team [a,b] -> project [b]. Narrowing the org to [a] must fail.
    org = _org(client, model_access="all")
    team = _team(client, org["id"], model_access="list", allowed_models=["a", "b"]).json()
    project = _project(client, team["id"], model_access="list", allowed_models=["b"]).json()
    _key(client, project["id"], model_access="list", allowed_models=["b"])

    resp = client.patch(
        f"/api/v1/orgs/{org['id']}",
        json={"model_access": "list", "allowed_models": ["a"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert f"teams.{team['id']}" in message
    assert "allowed_models" in message


def test_parent_limit_update_cannot_strand_children(client) -> None:
    org = _org(client, rpm_limit=100)
    team = _team(client, org["id"], rpm_limit=50).json()
    resp = client.patch(f"/api/v1/orgs/{org['id']}", json={"rpm_limit": 40}, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert f"teams.{team['id']}" in message
    assert "rpm_limit" in message


def test_inherit_intermediate_still_validates_grandchildren(client) -> None:
    org = _org(client, model_access="all")
    team = _team(client, org["id"], model_access="inherit").json()
    project = _project(client, team["id"], model_access="list", allowed_models=["a"]).json()
    resp = client.patch(
        f"/api/v1/orgs/{org['id']}",
        json={"model_access": "list", "allowed_models": ["b"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert f"projects.{project['id']}" in message
    assert "allowed_models" in message


def test_team_update_cannot_widen_parent(client) -> None:
    org = _org(client, model_access="list", allowed_models=["a"])
    team = _team(client, org["id"], model_access="list", allowed_models=["a"]).json()
    resp = client.patch(
        f"/api/v1/teams/{team['id']}",
        json={"allowed_models": ["a", "b"]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    assert "allowed_models" in resp.json()["error"]["message"]

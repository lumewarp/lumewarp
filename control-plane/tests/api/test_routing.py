"""Upstreams, deployments and routes."""

from __future__ import annotations

import uuid

from tests.api.helpers import BOOTSTRAP_AUTH, suffix


def _upstream(client, **body):
    body = {
        "name": f"up-{suffix()}",
        "kind": "vllm",
        "base_url": "http://127.0.0.1:9000/v1",
        **body,
    }
    return client.post("/api/v1/upstreams", json=body, headers=BOOTSTRAP_AUTH)


def _deployment(client, upstream_id, **body):
    body = {
        "name": f"dep-{suffix()}",
        "upstream_id": upstream_id,
        "upstream_model": "model",
        **body,
    }
    return client.post("/api/v1/deployments", json=body, headers=BOOTSTRAP_AUTH)


def test_upstream_deployment_route_crud(client) -> None:
    up = _upstream(client, api_key_ref="OPENAI_API_KEY", timeout_ms=100)
    assert up.status_code == 201, up.text
    upstream = up.json()
    assert upstream["api_key_ref"] == "OPENAI_API_KEY"
    assert "api_key" not in upstream

    dep = _deployment(
        client,
        upstream["id"],
        price_input_nano_usd_per_mtok=10,
        price_output_nano_usd_per_mtok=20,
    )
    assert dep.status_code == 201, dep.text
    deployment = dep.json()
    assert deployment["price_input_nano_usd_per_mtok"] == 10

    route = client.post(
        "/api/v1/routes",
        json={
            "model": f"alias-{suffix()}",
            "strategy": "weighted",
            "deployment_ids": [deployment["id"]],
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert route.status_code == 201, route.text
    route_body = route.json()
    assert route_body["deployments"] == [{"deployment_id": deployment["id"], "position": 0}]

    # Updating a route replaces the ordered deployment list.
    dep2 = _deployment(client, upstream["id"]).json()
    resp = client.patch(
        f"/api/v1/routes/{route_body['id']}",
        json={"deployment_ids": [dep2["id"], deployment["id"]]},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert [d["deployment_id"] for d in resp.json()["deployments"]] == [
        dep2["id"],
        deployment["id"],
    ]

    # Read aliases are available to every authenticated principal.
    resp = client.get("/api/v1/routes", headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200
    assert any(item["id"] == route_body["id"] for item in resp.json()["items"])

    # Dependency conflicts are 409, not cascades.
    assert (
        client.delete(f"/api/v1/upstreams/{upstream['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 409
    )
    assert (
        client.delete(f"/api/v1/deployments/{deployment['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 409
    )
    assert (
        client.delete(f"/api/v1/routes/{route_body['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/deployments/{deployment['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/deployments/{dep2['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/upstreams/{upstream['id']}", headers=BOOTSTRAP_AUTH).status_code
        == 204
    )


def test_routing_validation(client) -> None:
    bad = _upstream(client, base_url="ftp://example.com/v1")
    assert bad.status_code == 422
    bad = _upstream(client, base_url="http://user:pass@example.com/v1")
    assert bad.status_code == 422
    bad = _upstream(client, api_key_ref="not a name")
    assert bad.status_code == 422

    upstream = _upstream(client).json()
    bad = _deployment(client, str(uuid.uuid4()))
    assert bad.status_code == 422
    assert "upstream_id" in bad.json()["error"]["message"]

    bad = _deployment(client, upstream["id"], price_input_nano_usd_per_mtok=1)
    assert bad.status_code == 422
    assert "price" in bad.json()["error"]["message"]

    dep = _deployment(
        client,
        upstream["id"],
        price_input_nano_usd_per_mtok=1,
        price_output_nano_usd_per_mtok=2,
    ).json()
    # Explicitly clearing both halves is allowed; clearing one is not.
    resp = client.patch(
        f"/api/v1/deployments/{dep['id']}",
        json={"price_input_nano_usd_per_mtok": None},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 422
    resp = client.patch(
        f"/api/v1/deployments/{dep['id']}",
        json={
            "price_input_nano_usd_per_mtok": 5,
            "price_output_nano_usd_per_mtok": 6,
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200
    resp = client.patch(
        f"/api/v1/deployments/{dep['id']}",
        json={"price_input_nano_usd_per_mtok": None, "price_output_nano_usd_per_mtok": None},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["price_input_nano_usd_per_mtok"] is None

    route_body = {
        "model": f"alias-{suffix()}",
        "strategy": "lowest_latency",
        "deployment_ids": [dep["id"]],
    }
    resp = client.post("/api/v1/routes", json=route_body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 422
    assert "strategy" in resp.json()["error"]["message"]

    route_body["strategy"] = "weighted"
    route_body["deployment_ids"] = [str(uuid.uuid4())]
    resp = client.post("/api/v1/routes", json=route_body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 422
    assert "deployment_ids" in resp.json()["error"]["message"]

    route_body["deployment_ids"] = [dep["id"], dep["id"]]
    resp = client.post("/api/v1/routes", json=route_body, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["error"]["message"]


def test_unique_names_conflict(client) -> None:
    name = f"up-{suffix()}"
    first = _upstream(client, name=name)
    assert first.status_code == 201
    second = _upstream(client, name=name)
    assert second.status_code == 409

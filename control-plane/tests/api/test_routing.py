"""Upstreams, models and routers."""

from __future__ import annotations

import uuid

from tests.api.helpers import ADMIN_AUTH, suffix


def _upstream(client, **body):
    body = {
        "name": f"up-{suffix()}",
        "kind": "vllm",
        "base_url": "http://127.0.0.1:9000/v1",
        **body,
    }
    return client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)


def _model(client, upstream_id, **body):
    body = {
        "name": f"dep-{suffix()}",
        "upstream_id": upstream_id,
        "upstream_model": "name",
        **body,
    }
    return client.post("/api/v1/models", json=body, headers=ADMIN_AUTH)


def test_upstream_model_router_crud(client) -> None:
    up = _upstream(client, api_key_ref="OPENAI_API_KEY", timeout_ms=100)
    assert up.status_code == 201, up.text
    upstream = up.json()
    assert upstream["api_key_ref"] == "OPENAI_API_KEY"
    assert "api_key" not in upstream

    dep = _model(
        client,
        upstream["id"],
        price_input_nano_usd_per_mtok=10,
        price_output_nano_usd_per_mtok=20,
    )
    assert dep.status_code == 201, dep.text
    model = dep.json()
    assert model["price_input_nano_usd_per_mtok"] == 10

    router = client.post(
        "/api/v1/routers",
        json={
            "name": f"alias-{suffix()}",
            "strategy": "weighted",
            "model_ids": [model["id"]],
        },
        headers=ADMIN_AUTH,
    )
    assert router.status_code == 201, router.text
    router_body = router.json()
    assert router_body["models"] == [{"model_id": model["id"], "position": 0}]

    # Updating a router replaces the ordered model list.
    dep2 = _model(client, upstream["id"]).json()
    resp = client.patch(
        f"/api/v1/routers/{router_body['id']}",
        json={"model_ids": [dep2["id"], model["id"]]},
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert [d["model_id"] for d in resp.json()["models"]] == [
        dep2["id"],
        model["id"],
    ]

    # Read aliases are available to every authenticated principal.
    resp = client.get("/api/v1/routers", headers=ADMIN_AUTH)
    assert resp.status_code == 200
    assert any(item["id"] == router_body["id"] for item in resp.json()["items"])

    # Dependency conflicts are 409, not cascades.
    assert (
        client.delete(f"/api/v1/upstreams/{upstream['id']}", headers=ADMIN_AUTH).status_code == 409
    )
    assert client.delete(f"/api/v1/models/{model['id']}", headers=ADMIN_AUTH).status_code == 409
    assert (
        client.delete(f"/api/v1/routers/{router_body['id']}", headers=ADMIN_AUTH).status_code == 204
    )
    assert client.delete(f"/api/v1/models/{model['id']}", headers=ADMIN_AUTH).status_code == 204
    assert client.delete(f"/api/v1/models/{dep2['id']}", headers=ADMIN_AUTH).status_code == 204
    assert (
        client.delete(f"/api/v1/upstreams/{upstream['id']}", headers=ADMIN_AUTH).status_code == 204
    )


def test_routing_validation(client) -> None:
    bad = _upstream(client, base_url="ftp://example.com/v1")
    assert bad.status_code == 422
    bad = _upstream(client, base_url="http://user:pass@example.com/v1")
    assert bad.status_code == 422
    bad = _upstream(client, api_key_ref="not a name")
    assert bad.status_code == 422

    upstream = _upstream(client).json()
    bad = _model(client, str(uuid.uuid4()))
    assert bad.status_code == 422
    assert "upstream_id" in bad.json()["error"]["message"]

    bad = _model(client, upstream["id"], price_input_nano_usd_per_mtok=1)
    assert bad.status_code == 422
    assert "price" in bad.json()["error"]["message"]

    dep = _model(
        client,
        upstream["id"],
        price_input_nano_usd_per_mtok=1,
        price_output_nano_usd_per_mtok=2,
    ).json()
    # Explicitly clearing both halves is allowed; clearing one is not.
    resp = client.patch(
        f"/api/v1/models/{dep['id']}",
        json={"price_input_nano_usd_per_mtok": None},
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 422
    resp = client.patch(
        f"/api/v1/models/{dep['id']}",
        json={
            "price_input_nano_usd_per_mtok": 5,
            "price_output_nano_usd_per_mtok": 6,
        },
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    resp = client.patch(
        f"/api/v1/models/{dep['id']}",
        json={"price_input_nano_usd_per_mtok": None, "price_output_nano_usd_per_mtok": None},
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["price_input_nano_usd_per_mtok"] is None

    router_body = {
        "name": f"alias-{suffix()}",
        "strategy": "no_such_strategy",
        "model_ids": [dep["id"]],
    }
    resp = client.post("/api/v1/routers", json=router_body, headers=ADMIN_AUTH)
    assert resp.status_code == 422
    assert "strategy" in resp.json()["error"]["message"]

    router_body["strategy"] = "weighted"
    router_body["model_ids"] = [str(uuid.uuid4())]
    resp = client.post("/api/v1/routers", json=router_body, headers=ADMIN_AUTH)
    assert resp.status_code == 422
    assert "model_ids" in resp.json()["error"]["message"]

    router_body["model_ids"] = [dep["id"], dep["id"]]
    resp = client.post("/api/v1/routers", json=router_body, headers=ADMIN_AUTH)
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["error"]["message"]


def test_unique_names_conflict(client) -> None:
    name = f"up-{suffix()}"
    first = _upstream(client, name=name)
    assert first.status_code == 201
    second = _upstream(client, name=name)
    assert second.status_code == 409

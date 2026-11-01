"""Routing, gateway isolation, schema and validation regressions."""

from __future__ import annotations

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import (
    GatewaySession,
    GatewayToken,
    Scope,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import (
    ADMIN_AUTH,
    auth,
    suffix,
    user_with_token,
)

API = "/api/v1"


def _post(client, path, **body):
    return client.post(f"{API}{path}", json=body, headers=ADMIN_AUTH)


def _patch(client, path, **body):
    return client.patch(f"{API}{path}", json=body, headers=ADMIN_AUTH)


def _deployment(client, *, weight=1, upstream_id=None):
    if upstream_id is None:
        upstream_id = _post(
            client, "/upstreams", name=f"up-{suffix()}", kind="vllm", base_url="http://h:1/v1"
        ).json()["id"]
    resp = _post(
        client,
        "/deployments",
        name=f"dep-{suffix()}",
        upstream_id=upstream_id,
        upstream_model="m",
        weight=weight,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------------------ B1


def test_weighted_route_needs_positive_total_weight(client) -> None:
    zero = _deployment(client, weight=0)
    live = _deployment(client, weight=2, upstream_id=zero["upstream_id"])

    bad = _post(client, "/routes", model=f"m-{suffix()}", deployment_ids=[zero["id"]])
    assert bad.status_code == 422
    assert "deployment_ids" in bad.json()["error"]["message"]

    # least_in_flight ignores weights, so an all-zero route is fine there.
    ok = _post(
        client,
        "/routes",
        model=f"m-{suffix()}",
        strategy="least_in_flight",
        deployment_ids=[zero["id"]],
    )
    assert ok.status_code == 201, ok.text
    route_id = ok.json()["id"]
    # ...but switching it to weighted is not.
    assert _patch(client, f"/routes/{route_id}", strategy="weighted").status_code == 422
    assert _patch(client, f"/routes/{route_id}", deployment_ids=[live["id"]]).status_code == 200
    assert _patch(client, f"/routes/{route_id}", strategy="weighted").status_code == 200
    # Replacing the deployments of a weighted route with all-zero ones is rejected.
    assert _patch(client, f"/routes/{route_id}", deployment_ids=[zero["id"]]).status_code == 422


def test_deployment_weight_update_cannot_starve_weighted_route(client) -> None:
    dep = _deployment(client, weight=1)
    spare = _deployment(client, weight=1, upstream_id=dep["upstream_id"])
    route = _post(client, "/routes", model=f"m-{suffix()}", deployment_ids=[dep["id"]]).json()

    resp = _patch(client, f"/deployments/{dep['id']}", weight=0)
    assert resp.status_code == 422
    assert "weight" in resp.json()["error"]["message"]
    assert client.get(f"{API}/deployments/{dep['id']}", headers=ADMIN_AUTH).json()["weight"] == 1

    # With another positive deployment in the route the weight may drop to 0.
    _patch(client, f"/routes/{route['id']}", deployment_ids=[dep["id"], spare["id"]])
    assert _patch(client, f"/deployments/{dep['id']}", weight=0).status_code == 200
    # Zeroing the last positive one is rejected again.
    assert _patch(client, f"/deployments/{spare['id']}", weight=0).status_code == 422
    # Weights on unrelated deployments are unaffected.
    other = _deployment(client, weight=3, upstream_id=dep["upstream_id"])
    assert _patch(client, f"/deployments/{other['id']}", weight=0).status_code == 200


# ------------------------------------------------------------------------------ S3


async def test_gateways_are_scoped(client, committed, api_scope) -> None:
    other_scope = f"other-{suffix()}"
    committed.add(Scope(name=other_scope))
    await committed.flush()
    foreign_id = f"gw-foreign-{suffix()}"
    secret = generate_secret(SecretKind.GATEWAY_TOKEN)
    foreign_token = GatewayToken(
        scope=other_scope,
        gateway_id=foreign_id,
        name="foreign",
        token_hash=secret.hash,
        display_prefix=secret.display_prefix,
    )
    committed.add(foreign_token)
    committed.add(GatewaySession(scope=other_scope, gateway_id=foreign_id, connected=True))
    await committed.commit()

    listed = client.get(f"{API}/gateways?limit=500", headers=ADMIN_AUTH).json()["items"]
    assert foreign_id not in {item["gateway_id"] for item in listed}
    revoke = client.post(
        f"{API}/gateways/{foreign_id}/tokens/{foreign_token.id}/revoke", headers=ADMIN_AUTH
    )
    assert revoke.status_code == 404

    # Pagination happens in the database and walks every gateway exactly once.
    ids = [f"gw-page-{suffix()}" for _ in range(3)]
    for gateway_id in ids:
        assert _post(client, "/gateways", gateway_id=gateway_id).status_code == 201
    seen: list[str] = []
    cursor = None
    while True:
        url = f"{API}/gateways?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=ADMIN_AUTH).json()
        seen.extend(item["gateway_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)
    assert seen == sorted(seen)


# ------------------------------------------------------------------------------ S5


def test_openapi_documents_the_real_error_body(api_settings: Settings) -> None:
    schema = create_app(api_settings).openapi()
    operations = [
        (path, method, op)
        for path, item in schema["paths"].items()
        if path.startswith(API)
        for method, op in item.items()
    ]
    for path, method, op in operations:
        for status in ("401", "403", "404", "409", "422"):
            ref = op["responses"][status]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/ErrorBody"), (path, method, status)
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert set(schema["components"]["schemas"]["ErrorBody"]["properties"]) == {"error"}
    delete_user = schema["paths"][f"{API}/users/{{user_id}}"]["delete"]
    assert delete_user["operationId"] == "deleteUser"
    assert "204" in delete_user["responses"]


# ------------------------------------------------------------------------------ S6


def test_list_routes_returns_ordered_deployments_for_every_route(client) -> None:
    deployments = [_deployment(client)]
    deployments += [
        _deployment(client, upstream_id=deployments[0]["upstream_id"]) for _ in range(2)
    ]
    expected = {}
    for index in range(4):
        order = [d["id"] for d in deployments[index % 3 :] + deployments[: index % 3]]
        route = _post(client, "/routes", model=f"m-{suffix()}", deployment_ids=order)
        assert route.status_code == 201
        expected[route.json()["id"]] = order
    items = client.get(f"{API}/routes?limit=500", headers=ADMIN_AUTH).json()["items"]
    for item in items:
        if item["id"] in expected:
            assert [d["deployment_id"] for d in item["deployments"]] == expected[item["id"]]
            assert [d["position"] for d in item["deployments"]] == [0, 1, 2]


# ------------------------------------------------------------------------------ N1-N2


def test_api_key_ref_is_ascii_only(client) -> None:
    for ref in ("ÉNV", "ＫＥＹ１", "1ABC", "A-B"):
        resp = _post(
            client,
            "/upstreams",
            name=f"u-{suffix()}",
            kind="vllm",
            base_url="http://h:1",
            api_key_ref=ref,
        )
        assert resp.status_code == 422, ref
    up = _post(
        client, "/upstreams", name=f"u-{suffix()}", kind="vllm", base_url="http://h:1"
    ).json()
    assert _patch(client, f"/upstreams/{up['id']}", api_key_ref="ÉNV").status_code == 422
    assert _patch(client, f"/upstreams/{up['id']}", api_key_ref="_OK_1").status_code == 200


def test_names_and_aliases_are_clean(client) -> None:
    dep = _deployment(client)
    resp = _post(client, "/routes", model="  spaced alias\n", deployment_ids=[dep["id"]])
    assert resp.status_code == 422
    resp = _post(client, "/routes", model="  padded  ", deployment_ids=[dep["id"]])
    assert resp.status_code == 201
    assert resp.json()["model"] == "padded"

    assert _post(client, "/teams", name="   ").status_code == 422
    team = _post(client, "/teams", name=f"team-{suffix()}").json()
    assert _patch(client, f"/teams/{team['id']}", name=" \t").status_code == 422
    assert (
        _patch(
            client, f"/teams/{team['id']}", allowed_models=["a b"], model_access="list"
        ).status_code
        == 422
    )
    # Allowlist entries and route aliases share one normalisation.
    ok = _patch(client, f"/teams/{team['id']}", allowed_models=[" padded "], model_access="list")
    assert ok.status_code == 200
    assert ok.json()["allowed_models"] == ["padded"]


# ------------------------------------------------------------------------------ N3-N6


async def test_group_admin_cannot_lock_itself_out(client, committed) -> None:
    admin, secret = await user_with_token(committed, group_admin=True)
    await committed.commit()
    admin = {"id": str(admin.id)}
    admin_auth = auth(secret)
    resp = client.patch(f"{API}/users/{admin['id']}", json={"is_active": False}, headers=admin_auth)
    assert resp.status_code == 409
    assert client.delete(f"{API}/users/{admin['id']}", headers=admin_auth).status_code == 409
    # Emergency sessions may still deactivate another user.
    assert _patch(client, f"/users/{admin['id']}", is_active=False).status_code == 200


def _pat_for(client, user_id: str) -> str:
    response = _post(client, f"/users/{user_id}/tokens", name="t")
    assert response.status_code == 201, response.text
    return response.json()["secret"]


def test_query_ranges_are_capped(client) -> None:
    team = _post(client, "/teams", name=f"team-{suffix()}").json()
    wide = {"from": "2024-01-01T00:00:00+00:00", "to": "2026-01-01T00:00:00+00:00"}
    for path in ("/audit", "/usage"):
        resp = client.get(
            f"{API}{path}", params={**wide, "team_id": team["id"]}, headers=ADMIN_AUTH
        )
        assert resp.status_code == 422, path
        assert "400 days" in resp.json()["error"]["message"]
    ok = {"from": "2026-01-01T00:00:00+00:00", "to": "2026-06-01T00:00:00+00:00"}
    for path in ("/audit", "/usage"):
        resp = client.get(f"{API}{path}", params={**ok, "team_id": team["id"]}, headers=ADMIN_AUTH)
        assert resp.status_code == 200, (path, resp.text)

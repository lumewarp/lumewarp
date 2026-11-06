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


def _members(*ids, weight=1):
    return [{"model_id": i, "weight": weight} for i in ids]


def _team(client, **body):
    """A team needs an explicit org; there is no implicit default."""
    org = _post(client, "/orgs", name=f"org-{suffix()}").json()
    return _post(client, "/teams", org_id=org["id"], **body)


def _model(client, *, upstream_id=None):
    if upstream_id is None:
        upstream_id = _post(
            client, "/upstreams", name=f"up-{suffix()}", kind="vllm", base_url="http://h:1/v1"
        ).json()["id"]
    resp = _post(
        client,
        "/models",
        name=f"dep-{suffix()}",
        upstream_id=upstream_id,
        upstream_model="m",
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------------------ B1


def test_weighted_router_needs_positive_total_weight(client) -> None:
    dep = _model(client)

    bad = _post(client, "/routers", name=f"m-{suffix()}", members=_members(dep["id"], weight=0))
    assert bad.status_code == 422
    assert "members" in bad.json()["error"]["message"]

    # least_in_flight ignores weights, so an all-zero router is fine there.
    ok = _post(
        client,
        "/routers",
        name=f"m-{suffix()}",
        strategy="least_in_flight",
        members=_members(dep["id"], weight=0),
    )
    assert ok.status_code == 201, ok.text
    router_id = ok.json()["id"]
    # ...but switching it to weighted is not.
    assert _patch(client, f"/routers/{router_id}", strategy="weighted").status_code == 422
    live = _members(dep["id"], weight=2)
    assert _patch(client, f"/routers/{router_id}", members=live).status_code == 200
    assert _patch(client, f"/routers/{router_id}", strategy="weighted").status_code == 200
    # Replacing the members of a weighted router with all-zero ones is rejected.
    zeroed = _members(dep["id"], weight=0)
    assert _patch(client, f"/routers/{router_id}", members=zeroed).status_code == 422


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


def test_list_routers_returns_ordered_models_for_every_router(client) -> None:
    models = [_model(client)]
    models += [_model(client, upstream_id=models[0]["upstream_id"]) for _ in range(2)]
    expected = {}
    for index in range(4):
        order = [d["id"] for d in models[index % 3 :] + models[: index % 3]]
        router = _post(client, "/routers", name=f"m-{suffix()}", members=_members(*order))
        assert router.status_code == 201
        expected[router.json()["id"]] = order
    items = client.get(f"{API}/routers?limit=500", headers=ADMIN_AUTH).json()["items"]
    for item in items:
        if item["id"] in expected:
            assert [d["model_id"] for d in item["members"]] == expected[item["id"]]


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
    dep = _model(client)
    resp = _post(client, "/routers", name="  spaced alias\n", members=_members(dep["id"]))
    assert resp.status_code == 422
    padded = f"padded-{suffix()}"
    resp = _post(client, "/routers", name=f"  {padded}  ", members=_members(dep["id"]))
    assert resp.status_code == 201
    assert resp.json()["name"] == padded

    assert _team(client, name="   ").status_code == 422
    team = _team(client, name=f"team-{suffix()}").json()
    assert _patch(client, f"/teams/{team['id']}", name=" \t").status_code == 422
    assert (
        _patch(
            client, f"/teams/{team['id']}", allowed_models=["a b"], model_access="list"
        ).status_code
        == 422
    )
    # Allowlist entries and router aliases share one normalisation.
    ok = _patch(client, f"/teams/{team['id']}", allowed_models=[f" {padded} "], model_access="list")
    assert ok.status_code == 200
    assert ok.json()["allowed_models"] == [padded]


# ------------------------------------------------------------------------------ N3-N6


async def test_installation_admin_cannot_lock_itself_out(client, committed) -> None:
    admin, secret = await user_with_token(committed, installation_admin=True)
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
    team = _team(client, name=f"team-{suffix()}").json()
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

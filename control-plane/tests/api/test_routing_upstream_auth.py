"""LLM upstreams with a WASM auth plugin, and the saved Test now that goes through a gateway."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import upstream_client
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane.compiler.compile import compile_zone
from lumewarp_control_plane.grpc import ProbeGatewayGone, ProbeTimeout, ProbeUnavailable
from lumewarp_control_plane.models import (
    AuditLog,
    GatewaySession,
    GatewayToken,
    Upstream,
)
from tests.api.helpers import ADMIN_AUTH, app_zone, create_model, report_plugins, suffix

H = ADMIN_AUTH
PREFIX = "up-auth-test-"
PLUGIN = "hmac_signature"
AUTH_SCHEMA = {
    "type": "object",
    "properties": {"ttl_ms": {"type": "integer", "minimum": 1}},
    "required": ["ttl_ms"],
    "additionalProperties": False,
}
AUTH = {"plugin": PLUGIN, "params": {"ttl_ms": 1000}, "timeout_ms": 2500}


def _create(client, **body):
    body = {
        "name": f"up-{suffix()}",
        "kind": "vllm",
        "base_url": "http://vllm.test/v1",
        **body,
    }
    return client.post("/api/v1/upstreams", json=body, headers=H)


def _model(client, upstream_id, **body):
    model = create_model(client, upstream_id, name=f"dep-{suffix()}", upstream_model="name", **body)
    return client.get(f"/api/v1/models/{model['id']}", headers=H)


async def _row(committed: AsyncSession, upstream_id: str) -> Upstream:
    committed.expire_all()
    row = await committed.get(Upstream, uuid.UUID(upstream_id))
    assert row is not None
    return row


class FakeRegistry:
    """The probe seam the API awaits; no gRPC stream in these tests."""

    def __init__(
        self,
        result: cpb.ProbeResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple] = []

    async def probe(
        self,
        scope: str,
        plugin: str,
        upstream: str,
        model: str = "",
        *,
        timeout_seconds: float = 15.0,
    ) -> cpb.ProbeResult:
        self.calls.append((scope, plugin, upstream, model, timeout_seconds))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
async def gateway(committed: AsyncSession, api_scope: str) -> AsyncIterator:
    """A connected gateway reporting a WASM upstream-auth plugin; gone after the test."""

    async def report(gateway: str = "one", plugins=None, **kwargs):
        return await report_plugins(
            committed,
            api_scope,
            f"{PREFIX}{gateway}",
            plugins if plugins is not None else {("upstream-auth", PLUGIN): (AUTH_SCHEMA, None)},
            connected=True,
            **kwargs,
        )

    yield report
    await committed.rollback()
    await committed.execute(delete(GatewayToken).where(GatewayToken.name.like(f"{PREFIX}%")))
    await committed.execute(
        delete(GatewaySession).where(GatewaySession.gateway_id.like(f"{PREFIX}%"))
    )
    await committed.commit()


async def test_create_update_and_out_shape(client, committed, gateway) -> None:
    await gateway()
    response = _create(client, auth=AUTH)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["auth"] == AUTH
    assert body["api_key_set"] is False and body["api_key_ref"] is None
    row = await _row(committed, body["id"])
    assert (row.auth_plugin, row.auth_params, row.auth_timeout_ms) == (
        PLUGIN,
        {"ttl_ms": 1000},
        2500,
    )

    changed = client.patch(
        f"/api/v1/upstreams/{body['id']}",
        json={"auth": {"plugin": PLUGIN, "params": {"ttl_ms": 2000}, "timeout_ms": 0}},
        headers=H,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["auth"] == {"plugin": PLUGIN, "params": {"ttl_ms": 2000}, "timeout_ms": 0}
    assert (await _row(committed, body["id"])).auth_params == {"ttl_ms": 2000}

    cleared = client.patch(f"/api/v1/upstreams/{body['id']}", json={"auth": None}, headers=H)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["auth"] is None
    row = await _row(committed, body["id"])
    assert row.auth_plugin is None and row.auth_params is None and row.auth_timeout_ms == 0


async def test_unknown_plugin_bad_params_and_builtins_are_422(client) -> None:
    unknown = _create(client, auth={"plugin": "no_such_plugin"})
    assert unknown.status_code == 422, unknown.text
    assert "no gateway reports" in unknown.json()["error"]["message"]

    builtin = _create(client, auth={"plugin": "static_bearer"})
    assert builtin.status_code == 422, builtin.text
    assert "WASM upstream-auth plugin" in builtin.json()["error"]["message"]
    for name in ("none", "passthrough"):
        assert _create(client, auth={"plugin": name}).status_code == 422


async def test_params_must_match_the_reported_schema(client, gateway) -> None:
    await gateway()
    missing = _create(client, auth={"plugin": PLUGIN, "params": {}})
    assert missing.status_code == 422, missing.text
    assert "ttl_ms" in missing.json()["error"]["message"]
    bad = _create(client, auth={"plugin": PLUGIN, "params": {"ttl_ms": 0}})
    assert bad.status_code == 422, bad.text
    assert "at least 1" in bad.json()["error"]["message"]


async def test_a_plugin_and_an_api_key_are_mutually_exclusive(client, gateway) -> None:
    await gateway()
    for body in (
        {"auth": AUTH, "api_key": "sk-x"},
        {"auth": AUTH, "api_key_ref": "OPENAI_API_KEY"},
    ):
        response = _create(client, **body)
        assert response.status_code == 422, response.text
        assert "not both" in response.json()["error"]["message"]

    keyed = _create(client, api_key="sk-stored").json()
    saved_plugin = client.patch(f"/api/v1/upstreams/{keyed['id']}", json={"auth": AUTH}, headers=H)
    assert saved_plugin.status_code == 422, saved_plugin.text
    assert "mutually exclusive" in saved_plugin.json()["error"]["message"]
    # Clearing the key in the same request switches the credential source.
    switch_in = client.patch(
        f"/api/v1/upstreams/{keyed['id']}", json={"auth": AUTH, "api_key": None}, headers=H
    )
    assert switch_in.status_code == 200, switch_in.text
    assert switch_in.json()["auth"] == AUTH and switch_in.json()["api_key_set"] is False

    signed = _create(client, auth=AUTH).json()
    saved_key = client.patch(
        f"/api/v1/upstreams/{signed['id']}", json={"api_key": "sk-other"}, headers=H
    )
    assert saved_key.status_code == 422, saved_key.text
    # Clearing the plugin in the same request makes the key valid.
    switch = client.patch(
        f"/api/v1/upstreams/{signed['id']}",
        json={"auth": None, "api_key": "sk-other"},
        headers=H,
    )
    assert switch.status_code == 200, switch.text
    assert switch.json()["auth"] is None and switch.json()["api_key_set"] is True


async def test_force_saves_past_a_gateway_without_the_plugin(client, committed, gateway) -> None:
    await gateway("with-plugin")
    await gateway("without-plugin", plugins={})
    blocked = _create(client, auth=AUTH)
    assert blocked.status_code == 422, blocked.text
    assert "force" in blocked.json()["error"]["message"]

    saved = _create(client, auth=AUTH, force=True)
    assert saved.status_code == 201, saved.text
    rows = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == saved.json()["id"],
                AuditLog.action == "upstream.create",
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["auth_plugin"] == AUTH["plugin"]
    forced = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == saved.json()["id"],
                AuditLog.action == "upstream.force_save",
            )
        )
    ).all()
    assert len(forced) == 1
    assert forced[0].details == {
        "name": saved.json()["name"],
        "plugin": AUTH["plugin"],
        "missing_gateways": forced[0].details["missing_gateways"],
    }
    assert len(forced[0].details["missing_gateways"]) == 1


async def test_compile_emits_auth_and_its_capability(client, committed, api_settings, gateway):
    await gateway()
    body = _create(client, auth=AUTH).json()
    committed.expire_all()
    compiled = await compile_zone(committed, api_settings.zone, require_local_membership=True)
    upstream = next(u for u in compiled.snapshot.upstreams if u.name == body["name"])
    assert upstream.auth.plugin.plugin == PLUGIN
    assert upstream.auth.plugin.params_json == '{"ttl_ms":1000}'
    assert upstream.auth.plugin.timeout_ms == 2500
    assert f"auth:{PLUGIN}" in compiled.snapshot.required_capabilities


class FakeGrpcServer(SimpleNamespace):
    """What stop_background expects of a server, plus the registry the API reads."""

    async def stop(self, grace: float) -> None:
        return None


def _use_registry(client, registry: FakeRegistry) -> None:
    """Attach a fake to the gRPC server slot the API reads (background is off in tests)."""
    client.app.state.runtime.grpc_server = FakeGrpcServer(session_registry=registry)


def _saved_test(client, upstream_id: str):
    return client.post(f"/api/v1/upstreams/{upstream_id}/test", headers=H)


def _saved_model_test(client, model_id: str):
    return client.post(f"/api/v1/models/{model_id}/deployments/{app_zone(client)}/test", headers=H)


async def test_saved_upstream_test_uses_a_capable_gateway(client, api_scope, gateway) -> None:
    await gateway()
    body = _create(client, auth=AUTH, timeout_ms=5000).json()
    registry = FakeRegistry(cpb.ProbeResult(request_id="r", ok=True, status=204, latency_ms=12))
    _use_registry(client, registry)
    response = _saved_test(client, body["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "credential": "plugin",
        "probe": {
            "url": "http://vllm.test/v1/models",
            "ok": True,
            "status": 204,
            "latency_ms": 12,
            "error": None,
        },
    }
    assert registry.calls == [(api_scope, PLUGIN, body["name"], "", 5.0)]
    # The result is kept on the saved upstream.
    stored = client.get(f"/api/v1/upstreams/{body['id']}", headers=H).json()
    assert stored["probe"]["ok"] is True


async def test_a_probe_of_an_unapplied_version_is_not_stored(client, gateway) -> None:
    """`not_found` describes the gateway's snapshot, not the upstream: shown, never kept."""
    await gateway()
    body = _create(client, auth=AUTH, timeout_ms=5000).json()
    _use_registry(
        client,
        FakeRegistry(
            cpb.ProbeResult(request_id="r", ok=False, latency_ms=1, error_class="not_found")
        ),
    )
    response = _saved_test(client, body["id"])
    assert response.status_code == 200, response.text
    assert response.json()["probe"]["error"] == "not_found"
    stored = client.get(f"/api/v1/upstreams/{body['id']}", headers=H).json()
    assert stored["probe"] is None


async def test_saved_model_test_uses_a_capable_gateway(client, api_scope, gateway) -> None:
    await gateway()
    upstream = _create(client, auth=AUTH, timeout_ms=60000).json()
    model = _model(client, upstream["id"]).json()
    registry = FakeRegistry(cpb.ProbeResult(request_id="r", ok=False, status=503, latency_ms=7))
    _use_registry(client, registry)
    response = _saved_model_test(client, model["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "credential": "plugin",
        "probe": {
            "url": "http://vllm.test/v1/chat/completions",
            "ok": False,
            "status": 503,
            "latency_ms": 7,
            "error": None,
        },
        "reply": None,
    }
    # The upstream timeout is capped at 15 s.
    assert registry.calls == [(api_scope, PLUGIN, upstream["name"], model["name"], 15.0)]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ProbeUnavailable("no gateway"), 409, "no_gateway_for_plugin"),
        (ProbeTimeout("late"), 504, "probe_timeout"),
        (ProbeGatewayGone("gone"), 502, "probe_gateway_gone"),
    ],
)
async def test_saved_test_maps_probe_failures(
    client, gateway, error: Exception, status: int, code: str
) -> None:
    await gateway()
    body = _create(client, auth=AUTH).json()
    _use_registry(client, FakeRegistry(error=error))
    response = _saved_test(client, body["id"])
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


async def test_no_gateway_connected_is_409(client, gateway) -> None:
    await gateway()
    body = _create(client, auth=AUTH).json()
    # background_enabled=False leaves no gRPC server (and so no process-local registry).
    assert client.app.state.runtime.grpc_server is None
    response = _saved_test(client, body["id"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "no_gateway_for_plugin"


async def test_saved_upstream_without_a_plugin_still_tests_directly(client, monkeypatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(upstream_client, "transport", httpx.MockTransport(handler))
    body = _create(client).json()
    response = _saved_test(client, body["id"])
    assert response.status_code == 200, response.text
    assert response.json()["credential"] == "none"
    assert response.json()["probe"]["url"] == "http://vllm.test/v1/models"
    assert len(calls) == 1


async def test_the_unsaved_tests_refuse_a_plugin(client, gateway) -> None:
    await gateway()
    body = _create(client, auth=AUTH).json()
    named = client.post(
        "/api/v1/upstreams/test",
        json={
            "base_url": "http://vllm.test/v1",
            "auth": {"plugin": PLUGIN, "params": {"ttl_ms": 1000}},
        },
        headers=H,
    )
    assert named.status_code == 422, named.text
    assert "save the upstream first" in named.json()["error"]["message"]

    saved = client.post(
        "/api/v1/upstreams/test",
        json={"base_url": "http://vllm.test/v1", "upstream_id": body["id"]},
        headers=H,
    )
    assert saved.status_code == 422, saved.text
    assert "WASM auth plugin" in saved.json()["error"]["message"]

    model = _model(client, body["id"]).json()
    tested = client.post(
        "/api/v1/models/test",
        json={
            "model_type": "llm",
            "upstream_id": body["id"],
            "upstream_model": "name",
            "model_id": model["id"],
        },
        headers=H,
    )
    assert tested.status_code == 422, tested.text
    assert "save the model first" in tested.json()["error"]["message"]


async def test_unknown_ids_are_404(client) -> None:
    missing = "00000000-0000-7000-8000-000000000000"
    assert _saved_test(client, missing).status_code == 404
    assert _saved_model_test(client, missing).status_code == 404

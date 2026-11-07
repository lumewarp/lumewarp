"""Upstream health in the API: aggregated gateway reports and the on-demand test."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete

from lumewarp_control_plane import upstream_client
from lumewarp_control_plane.models import GatewaySession
from tests.api.helpers import ADMIN_AUTH, suffix

HEALTH = {"interval_ms": 5000, "timeout_ms": 1000}
KEY = "sk-health-test-secret-4411"


def _test(client, up: dict, **extra):
    """Test a saved upstream as its form stands (no key typed)."""
    body = {
        "kind": up["kind"],
        "base_url": up["base_url"],
        "upstream_id": up["id"],
        "health_check_url": (up["health_check"] or {}).get("url"),
        **extra,
    }
    return client.post("/api/v1/upstreams/test", json=body, headers=ADMIN_AUTH)


def _upstream(client, **body) -> dict:
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://vllm.test/v1", **body}
    response = client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 201, response.text
    return response.json()


async def _report(committed, scope, gateway_id, health, *, connected=True) -> None:
    now = datetime.now(UTC)
    committed.add(
        GatewaySession(
            scope=scope,
            gateway_id=gateway_id,
            session_id=uuid.uuid4(),
            connected=connected,
            upstream_health=health,
            connected_at=now,
            last_seen_at=now,
        )
    )
    await committed.commit()


@pytest.fixture
async def clean_sessions(committed, api_settings):
    yield
    await committed.execute(
        delete(GatewaySession).where(GatewaySession.scope == api_settings.scope)
    )
    await committed.commit()


async def test_health_aggregates_connected_gateways(
    client, committed, api_settings, clean_sessions
) -> None:
    probed = _upstream(client, health_check=HEALTH)
    other = _upstream(client, health_check=HEALTH)
    unprobed = _upstream(client)
    get = lambda up: client.get(f"/api/v1/upstreams/{up['id']}", headers=ADMIN_AUTH).json()  # noqa: E731
    assert get(probed)["health"] == "unknown"
    assert get(unprobed)["health"] == "not_probed"

    scope = api_settings.scope
    await _report(committed, scope, "gw-a", {probed["name"]: True, other["name"]: True})
    await _report(committed, scope, "gw-b", {probed["name"]: False})
    # A disconnected gateway's last report does not count.
    await _report(committed, scope, "gw-c", {other["name"]: False}, connected=False)
    assert get(probed)["health"] == "unhealthy"
    assert get(other)["health"] == "healthy"
    assert get(unprobed)["health"] == "not_probed"
    listed = {
        u["id"]: u["health"]
        for u in client.get("/api/v1/upstreams?limit=500", headers=ADMIN_AUTH).json()["items"]
    }
    assert listed[probed["id"]] == "unhealthy"


@pytest.fixture
def replies(monkeypatch):
    calls: list[httpx.Request] = []
    table: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) not in table:
            raise httpx.ConnectError("refused", request=request)
        return table[str(request.url)]

    monkeypatch.setattr(upstream_client, "transport", httpx.MockTransport(handler))
    return calls, table


def test_on_demand_test_probes_health_and_models(client, replies) -> None:
    calls, table = replies
    table["http://vllm.test/health"] = httpx.Response(200)
    table["http://vllm.test/v1/models"] = httpx.Response(401, text=KEY)
    up = _upstream(client, api_key=KEY)
    response = _test(client, up)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health"]["url"] == "http://vllm.test/health"
    assert body["health"]["ok"] is True and body["health"]["status"] == 200
    assert body["models"]["ok"] is False and body["models"]["status"] == 401
    assert KEY not in response.text
    assert all(c.headers["authorization"] == f"Bearer {KEY}" for c in calls)


def test_explicit_health_url_on_another_host_gets_no_token(client, replies) -> None:
    calls, table = replies
    table["http://probe.test/ready"] = httpx.Response(204)
    up = _upstream(
        client,
        kind="openai",
        api_key=KEY,
        health_check={**HEALTH, "url": "http://probe.test/ready"},
    )
    body = _test(client, up).json()
    assert body["health"] == {
        "url": "http://probe.test/ready",
        "ok": True,
        "status": 204,
        "latency_ms": body["health"]["latency_ms"],
        "error": None,
    }
    assert body["models"]["url"] == "http://vllm.test/v1/models"
    assert body["models"]["error"] == "connection failed"
    by_url = {str(c.url): c for c in calls}
    assert "authorization" not in by_url["http://probe.test/ready"].headers
    assert by_url["http://vllm.test/v1/models"].headers["authorization"] == f"Bearer {KEY}"


def test_default_health_url_for_non_vllm_is_models(client, replies) -> None:
    _, table = replies
    table["http://vllm.test/v1/models"] = httpx.Response(200, json={"data": []})
    up = _upstream(client, kind="openai_compatible")
    body = _test(client, up).json()
    assert body["health"]["url"] == "http://vllm.test/v1/models"
    assert body["health"]["ok"] is True


def test_unsaved_form_is_tested_with_the_typed_key(client, replies) -> None:
    calls, table = replies
    table["http://new.test/v1/models"] = httpx.Response(200, json={"data": []})
    body = {"kind": "openai", "base_url": "http://new.test/v1", "api_key": KEY}
    response = client.post("/api/v1/upstreams/test", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["credential"] == "entered"
    assert response.json()["models"]["ok"] is True
    assert KEY not in response.text
    assert all(c.headers["authorization"] == f"Bearer {KEY}" for c in calls)


def test_credential_sources(client, replies, monkeypatch) -> None:
    calls, table = replies
    table["http://vllm.test/v1/models"] = httpx.Response(200, json={"data": []})
    up = _upstream(client, kind="openai", api_key=KEY)
    assert _test(client, up).json()["credential"] == "stored"
    assert calls[-1].headers["authorization"] == f"Bearer {KEY}"
    # A key typed over a stored one wins, without replacing it.
    assert _test(client, up, api_key="sk-typed").json()["credential"] == "entered"
    assert calls[-1].headers["authorization"] == "Bearer sk-typed"
    monkeypatch.delenv("LW_TEST_UNSET_KEY", raising=False)
    body = _test(client, up, api_key_ref="LW_TEST_UNSET_KEY").json()
    assert body["credential"] == "env_unset"
    assert "authorization" not in calls[-1].headers
    monkeypatch.setenv("LW_TEST_SET_KEY", "env-123")
    assert _test(client, up, api_key_ref="LW_TEST_SET_KEY").json()["credential"] == "env"
    assert calls[-1].headers["authorization"] == "Bearer env-123"
    plain = _upstream(client, kind="openai")
    assert _test(client, plain).json()["credential"] == "none"
    assert _test(client, {**up, "id": str(uuid.uuid4())}).status_code == 404
    bad = {"kind": "openai", "base_url": "ftp://x"}
    assert client.post("/api/v1/upstreams/test", json=bad, headers=ADMIN_AUTH).status_code == 422

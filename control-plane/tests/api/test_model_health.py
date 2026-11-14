"""Model chat check in the API: the periodic setting, aggregated reports and Test now."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete

from lumewarp_control_plane import upstream_client
from lumewarp_control_plane.models import GatewaySession, GatewayToken
from lumewarp_control_plane.security import SecretKind, generate_secret
from tests.api.helpers import ADMIN_AUTH, suffix

KEY = "sk-chat-check-secret-7719"
CHAT_URL = "http://chat.test/v1/chat/completions"
CHECK = {"interval_ms": 30000, "timeout_ms": 5000}


def _upstream(client, **body) -> dict:
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://chat.test/v1", **body}
    response = client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _model(client, upstream: dict, **body) -> dict:
    body = {
        "name": f"m-{suffix()}",
        "upstream_id": upstream["id"],
        "upstream_model": "qwen",
        "model_type": "llm",
        **body,
    }
    response = client.post("/api/v1/models", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def test_chat_check_round_trips_and_clears(client) -> None:
    up = _upstream(client)
    model = _model(client, up, chat_check=CHECK)
    assert model["chat_check"] == {**CHECK, "unhealthy_threshold": None, "healthy_threshold": None}
    assert model["health"] == "unknown"
    url = f"/api/v1/models/{model['id']}"
    patched = client.patch(
        url, json={"chat_check": {**CHECK, "unhealthy_threshold": 2}}, headers=ADMIN_AUTH
    ).json()
    assert patched["chat_check"]["unhealthy_threshold"] == 2
    cleared = client.patch(url, json={"chat_check": None}, headers=ADMIN_AUTH).json()
    assert cleared["chat_check"] is None
    assert cleared["health"] == "not_probed"
    assert _model(client, up)["chat_check"] is None


@pytest.mark.parametrize(
    "check",
    [
        {"interval_ms": 999, "timeout_ms": 100},
        {"interval_ms": 5000, "timeout_ms": 5000},
        {"interval_ms": 5000, "timeout_ms": 0},
        {**CHECK, "unhealthy_threshold": 0},
    ],
)
def test_bad_chat_check_is_rejected(client, check) -> None:
    up = _upstream(client)
    body = {
        "name": f"m-{suffix()}",
        "upstream_id": up["id"],
        "upstream_model": "q",
        "model_type": "llm",
    }
    response = client.post("/api/v1/models", json={**body, "chat_check": check}, headers=ADMIN_AUTH)
    assert response.status_code == 422


@pytest.fixture
async def clean_sessions(committed, api_settings):
    yield
    await committed.execute(
        delete(GatewaySession).where(GatewaySession.scope == api_settings.scope)
    )
    await committed.commit()


async def _report(committed, scope, gateway_id, health, *, connected=True) -> None:
    now = datetime.now(UTC)
    secret = generate_secret(SecretKind.GATEWAY_TOKEN)
    token = GatewayToken(
        scope=scope,
        name=gateway_id,
        token_hash=secret.hash,
        display_prefix=secret.display_prefix,
    )
    committed.add(token)
    await committed.flush()
    committed.add(
        GatewaySession(
            scope=scope,
            gateway_id=gateway_id,
            token_id=token.id,
            session_id=uuid.uuid4(),
            connected=connected,
            model_health=health,
            connected_at=now,
            last_seen_at=now,
        )
    )
    await committed.commit()


async def test_model_health_aggregates_connected_gateways(
    client, committed, api_settings, clean_sessions
) -> None:
    up = _upstream(client)
    checked = _model(client, up, chat_check=CHECK)
    other = _model(client, up, chat_check=CHECK)
    plain = _model(client, up)
    scope = api_settings.scope
    await _report(committed, scope, "gw-a", {checked["name"]: True, other["name"]: True})
    await _report(committed, scope, "gw-b", {checked["name"]: False})
    await _report(committed, scope, "gw-c", {other["name"]: False}, connected=False)
    get = lambda m: client.get(f"/api/v1/models/{m['id']}", headers=ADMIN_AUTH).json()  # noqa: E731
    assert get(checked)["health"] == "unhealthy"
    assert get(other)["health"] == "healthy"
    assert get(plain)["health"] == "not_probed"
    listed = {
        m["id"]: m["health"]
        for m in client.get("/api/v1/models?limit=500", headers=ADMIN_AUTH).json()["items"]
    }
    assert listed[checked["id"]] == "unhealthy"


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


def _test(client, up: dict, **extra):
    body = {"upstream_id": up["id"], "upstream_model": "qwen", "model_type": "llm", **extra}
    return client.post("/api/v1/models/test", json=body, headers=ADMIN_AUTH)


def test_test_now_sends_one_chat_completion_with_the_default_body(client, replies) -> None:
    calls, table = replies
    table[CHAT_URL] = httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": " 2\n"}}]}
    )
    up = _upstream(client, api_key=KEY)
    response = _test(client, up, default_body={"top_k": 20, "max_tokens": 999})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["credential"] == "stored"
    assert out["probe"]["url"] == CHAT_URL and out["probe"]["ok"] is True
    assert out["reply"] == "2"
    assert KEY not in response.text
    sent = json.loads(calls[-1].content)
    # The probe's own fields win over the default body.
    assert sent == {
        "top_k": 20,
        "model": "qwen",
        "messages": [{"role": "user", "content": "1 + 1 = ?"}],
        "max_tokens": 16,
        "stream": False,
    }
    assert calls[-1].headers["authorization"] == f"Bearer {KEY}"


def test_test_now_reports_failure_without_the_body(client, replies, monkeypatch) -> None:
    calls, table = replies
    table[CHAT_URL] = httpx.Response(500, text=f"boom {KEY}")
    up = _upstream(client, api_key=KEY)
    out = _test(client, up).json()
    assert out["probe"]["ok"] is False and out["probe"]["status"] == 500
    assert out["reply"] is None
    assert KEY not in json.dumps(out)
    # Credential states.
    monkeypatch.delenv("LW_CHAT_UNSET", raising=False)
    unset = _upstream(client, api_key_ref="LW_CHAT_UNSET")
    assert _test(client, unset).json()["credential"] == "env_unset"
    assert "authorization" not in calls[-1].headers
    monkeypatch.setenv("LW_CHAT_SET", "env-456")
    env = _upstream(client, api_key_ref="LW_CHAT_SET")
    assert _test(client, env).json()["credential"] == "env"
    assert calls[-1].headers["authorization"] == "Bearer env-456"
    assert _test(client, _upstream(client)).json()["credential"] == "none"
    # Connection failure.
    other = _upstream(client, base_url="http://down.test/v1")
    down = _test(client, other).json()
    assert down["probe"]["ok"] is False and down["probe"]["error"] == "connection failed"
    assert _test(client, {"id": str(uuid.uuid4())}).status_code == 422


def test_test_now_result_is_kept_on_the_saved_row_until_its_settings_change(
    client, replies
) -> None:
    _, table = replies
    up = _upstream(client)
    model = _model(client, up)
    assert model["probe"] is None

    def probe() -> dict | None:
        return client.get(f"/api/v1/models/{model['id']}", headers=ADMIN_AUTH).json()["probe"]

    # Without model_id (an unsaved form) nothing is kept.
    table[CHAT_URL] = httpx.Response(200, json={"choices": [{"message": {"content": "2"}}]})
    assert _test(client, up).json()["probe"]["ok"] is True
    assert probe() is None
    # A form that differs from the saved model is not kept on it either.
    _test(client, up, model_id=model["id"], upstream_model="other")
    assert probe() is None

    _test(client, up, model_id=model["id"])
    ok = probe()
    assert ok is not None and ok["ok"] is True and ok["ok_at"] == ok["at"]
    unchanged = client.get(f"/api/v1/models/{model['id']}", headers=ADMIN_AUTH).json()
    assert unchanged["updated_at"] == model["updated_at"]

    table[CHAT_URL] = httpx.Response(500)
    _test(client, up, model_id=model["id"])
    failed = probe()
    assert failed is not None and failed["ok"] is False and failed["ok_at"] == ok["ok_at"]

    # Renaming keeps it; changing what was probed clears it.
    patch = client.patch(
        f"/api/v1/models/{model['id']}", json={"name": f"m-{suffix()}"}, headers=ADMIN_AUTH
    )
    assert patch.status_code == 200 and patch.json()["probe"] == failed
    patch = client.patch(
        f"/api/v1/models/{model['id']}", json={"upstream_model": "qwen2"}, headers=ADMIN_AUTH
    )
    assert patch.status_code == 200 and patch.json()["probe"] is None

    # An upstream's own result, and a new address clears its models' results too.
    table[CHAT_URL] = httpx.Response(200, json={"choices": [{"message": {"content": "2"}}]})
    _test(client, up, model_id=model["id"], upstream_model="qwen2")
    assert probe() is not None
    table["http://chat.test/v1/models"] = httpx.Response(200, json={"data": []})
    test = client.post(
        "/api/v1/upstreams/test",
        json={"base_url": up["base_url"], "upstream_id": up["id"]},
        headers=ADMIN_AUTH,
    )
    assert test.json()["probe"]["ok"] is True
    got = client.get(f"/api/v1/upstreams/{up['id']}", headers=ADMIN_AUTH).json()
    assert got["probe"]["ok"] is True
    patch = client.patch(
        f"/api/v1/upstreams/{up['id']}",
        json={"base_url": "http://moved.test/v1"},
        headers=ADMIN_AUTH,
    )
    assert patch.status_code == 200 and patch.json()["probe"] is None
    assert probe() is None

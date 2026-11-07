"""GET /upstreams/{id}/models calls the upstream's OpenAI `/models` with its credential."""

from __future__ import annotations

import httpx
import pytest

from lumewarp_control_plane import upstream_client
from tests.api.helpers import ADMIN_AUTH, suffix

KEY = "sk-fetch-models-secret-9a1b"


@pytest.fixture
def upstream_calls(monkeypatch):
    calls: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return replies.get(str(request.url), httpx.Response(404))

    monkeypatch.setattr(upstream_client, "transport", httpx.MockTransport(handler))
    return calls, replies


def _upstream(client, **body) -> dict:
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://vllm.test/v1", **body}
    response = client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def test_lists_models_with_the_stored_key(client, upstream_calls) -> None:
    calls, replies = upstream_calls
    replies["http://vllm.test/v1/models"] = httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"id": "qwen3", "object": "model", "max_model_len": 32768},
                {"id": "gpt-4o", "object": "model"},
                {"id": "qwen3", "max_model_len": 1},
                {"id": "", "max_model_len": 5},
                {"id": "bool", "max_model_len": True},
                {
                    "id": "router/llama",
                    "context_length": 131072,
                    "top_provider": {"max_completion_tokens": 8192},
                },
                {"id": "groq", "context_window": 8192, "max_completion_tokens": 4096},
                "garbage",
            ],
        },
    )
    up = _upstream(client, api_key=KEY)
    response = client.get(f"/api/v1/upstreams/{up['id']}/models", headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "models": [
            {"id": "bool", "max_input_tokens": None, "max_output_tokens": None},
            {"id": "gpt-4o", "max_input_tokens": None, "max_output_tokens": None},
            {"id": "groq", "max_input_tokens": 8192, "max_output_tokens": 4096},
            # vLLM reports no output limit: the context window bounds it.
            {"id": "qwen3", "max_input_tokens": 32768, "max_output_tokens": 32768},
            {"id": "router/llama", "max_input_tokens": 131072, "max_output_tokens": 8192},
        ]
    }
    assert calls[-1].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in response.text


def test_env_reference_and_no_credential(client, upstream_calls, monkeypatch) -> None:
    calls, replies = upstream_calls
    replies["http://vllm.test/v1/models"] = httpx.Response(200, json={"data": []})
    monkeypatch.setenv("LW_TEST_FETCH_KEY", "env-value-123")
    up = _upstream(client, api_key_ref="LW_TEST_FETCH_KEY")
    assert client.get(f"/api/v1/upstreams/{up['id']}/models", headers=ADMIN_AUTH).json() == {
        "models": []
    }
    assert calls[-1].headers["authorization"] == "Bearer env-value-123"
    up = _upstream(client)
    client.get(f"/api/v1/upstreams/{up['id']}/models", headers=ADMIN_AUTH)
    assert "authorization" not in calls[-1].headers


@pytest.mark.parametrize(
    ("reply", "needle"),
    [
        (httpx.Response(401, text=f"bad key {KEY}"), "HTTP 401"),
        (httpx.Response(200, text="not json"), "OpenAI model list"),
        (httpx.Response(200, json={"data": {"id": "x"}}), "OpenAI model list"),
        (httpx.Response(200, json=[1]), "OpenAI model list"),
    ],
)
def test_bad_upstream_answers_are_502_without_the_body(client, upstream_calls, reply, needle):
    _, replies = upstream_calls
    replies["http://vllm.test/v1/models"] = reply
    up = _upstream(client, api_key=KEY)
    response = client.get(f"/api/v1/upstreams/{up['id']}/models", headers=ADMIN_AUTH)
    assert response.status_code == 502
    assert needle in response.json()["error"]["message"]
    assert KEY not in response.text


def test_unreachable_upstream(client, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(upstream_client, "transport", httpx.MockTransport(handler))
    up = _upstream(client)
    response = client.get(f"/api/v1/upstreams/{up['id']}/models", headers=ADMIN_AUTH)
    assert response.status_code == 502
    assert "connection failed" in response.json()["error"]["message"]


def test_unknown_upstream_is_404(client) -> None:
    response = client.get(
        "/api/v1/upstreams/00000000-0000-7000-8000-000000000000/models", headers=ADMIN_AUTH
    )
    assert response.status_code == 404

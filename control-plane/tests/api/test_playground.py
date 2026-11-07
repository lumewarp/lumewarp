"""Playground ownership, session/CSRF and streaming delegation contract."""

import base64
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient
from sqlalchemy import select

from lumewarp_control_plane.api import playground
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.models import AuditLog, KeyLifecycle, MemberPolicy, ModelAccess
from tests.api.conftest import TEST_ENCRYPTION_KEY
from tests.api.helpers import auth, user_with_token
from tests.factories import make_key, make_member, make_model, make_service_account, make_team


def mock_gateway(client, monkeypatch, handler):
    async def send(request, **kwargs):
        return handler(request)

    monkeypatch.setattr(client.app.state.playground_client, "send", send)


def test_shared_client_lifecycle(api_settings):
    app = create_app(api_settings)
    with TestClient(app):
        shared = app.state.playground_client
        assert not shared.is_closed
        assert shared.timeout.connect == 5
        assert shared.timeout.read == 300
        assert not shared.follow_redirects
    assert shared.is_closed


def test_fixed_token_vector():
    assert playground.mint_token(
        TEST_ENCRYPTION_KEY, uuid.UUID("00000000-0000-0000-0000-000000000001"), 4102444800000
    ) == (
        "lwpg.00000000-0000-0000-0000-000000000001.4102444800000."
        "9q0Juo2V6Fupxe8pQezrHCPUq0zIgfqz-fmT_azFrrE"
    )


async def setup_keys(committed):
    team = await make_team(committed)
    user, pat = await user_with_token(committed)
    other, _ = await user_with_token(committed)
    await make_member(committed, team, user)
    await make_member(committed, team, other)
    cookie = await sessions.create_session(committed, user.id, 3600)
    own = await make_key(committed, team, user)
    foreign = await make_key(committed, team, other)
    account = await make_service_account(committed, team)
    service = await make_key(committed, team, account)
    revoked = await make_key(
        committed, team, user, lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC)
    )
    suspended = await make_key(
        committed, team, user, lifecycle=KeyLifecycle.SUSPENDED, suspended_reason="owner"
    )
    expired = await make_key(
        committed, team, user, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    await committed.commit()
    return own, [foreign, service, revoked, suspended, expired], auth("session:" + cookie), pat


async def test_own_keys_only_and_disabled(client, committed):
    own, excluded, headers, pat = await setup_keys(committed)
    response = client.get("/api/v1/playground", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert [key["id"] for key in response.json()["keys"]] == [str(own.id)]
    assert client.get("/api/v1/playground", headers=auth(pat)).status_code == 401
    assert (
        client.post(
            "/api/v1/playground/v1/chat/completions",
            headers={**auth(pat), "x-lumewarp-act-as": str(own.id)},
            json={"model": "mock"},
        ).status_code
        == 401
    )
    for key in excluded:
        response = client.post(
            "/api/v1/playground/v1/chat/completions",
            headers={**headers, "x-lumewarp-act-as": str(key.id)},
            json={"model": "mock"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "key not found"
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={"model": "mock"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "playground_disabled"


@pytest.mark.parametrize("model", ["private prompt", "x" * 257, None, 123, {"prompt": "secret"}])
async def test_audit_rejects_untrusted_model(client, committed, monkeypatch, model):
    own, _, headers, _ = await setup_keys(committed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    # Even a real catalog name over 256 chars must not enter the audit.
    if isinstance(model, str) and len(model) > 256:
        await make_model(committed, name=model)
        await committed.commit()
    mock_gateway(client, monkeypatch, lambda request: httpx.Response(200, stream=SSEStream()))
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={"model": model},
    )
    assert response.status_code == 200
    row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
        )
    )
    assert row.details == {"key_id": str(own.id), "model": None}


@pytest.mark.parametrize("allowed", [True, False])
async def test_audit_checks_effective_policy_and_length_boundary(
    client, committed, monkeypatch, allowed
):
    own, _, headers, _ = await setup_keys(committed)
    model = await make_model(committed, name="m" * 255 + str(allowed)[0])
    if not allowed:
        committed.add(
            MemberPolicy(team_id=own.team_id, user_id=own.user_id, model_access=ModelAccess.LIST)
        )
    await committed.commit()
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    mock_gateway(client, monkeypatch, lambda request: httpx.Response(200, stream=SSEStream()))
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={"model": model.name},
    )
    assert response.status_code == 200
    row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
        )
    )
    assert row.details["model"] == (model.name if allowed else None)


@pytest.mark.parametrize("code", ["invalid_api_key", "other_auth_error"])
async def test_gateway_auth_error(client, committed, monkeypatch, caplog, code):
    own, _, headers, _ = await setup_keys(committed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    responses = []

    def gateway(request):
        response = httpx.Response(
            401,
            stream=httpx.ByteStream(('{"error":{"code":"' + code + '"}}').encode()),
            headers={"content-type": "application/json"},
        )
        responses.append(response)
        return response

    mock_gateway(client, monkeypatch, gateway)
    for _ in range(2):
        response = client.post(
            "/api/v1/playground/v1/chat/completions",
            headers={**headers, "x-lumewarp-act-as": str(own.id)},
            json={"model": "mock"},
        )
        if code == "invalid_api_key":
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "playground_misconfigured"
        else:
            assert response.status_code == 401
            assert response.json() == {"error": {"code": code}}
    assert all(response.is_closed for response in responses)
    warnings = [record for record in caplog.records if record.name == playground.__name__]
    assert len(warnings) == (1 if code == "invalid_api_key" else 0)
    assert not client.app.state.playground_client.is_closed


async def test_csrf_missing(client, committed):
    own, _, headers, _ = await setup_keys(committed)
    headers.pop("X-Lumewarp-CSRF")
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


class SSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices": []}\n\n'
        yield b"data: [DONE]\n\n"


async def test_proxy_streams_and_audits(client, committed, monkeypatch):
    own, _, headers, _ = await setup_keys(committed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    model = await make_model(committed)
    await committed.commit()
    body = (
        b'{"model":"mock", "messages":[{"role":"user","content":"private prompt"}],"stream":true}'
    )

    body = body.replace(b'"model":"mock"', ('"model":"' + model.name + '"').encode())

    def gateway(request):
        assert str(request.url) == "http://gateway:4000/v1/chat/completions"
        assert request.content == body
        token = request.headers["authorization"].removeprefix("Bearer ")
        prefix, key_id, exp, mac = token.split(".")
        assert prefix == "lwpg" and key_id == str(own.id)
        assert 59_000 < int(exp) - time.time_ns() // 1_000_000 <= 60_000
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"lumewarp-playground-v1"
        ).derive(TEST_ENCRYPTION_KEY.encode())
        expected = (
            base64.urlsafe_b64encode(
                hmac.digest(key, f"{prefix}.{key_id}.{exp}".encode(), hashlib.sha256)
            )
            .decode()
            .rstrip("=")
        )
        assert hmac.compare_digest(mac, expected)
        return httpx.Response(
            201,
            headers={
                "content-type": "text/event-stream",
                "x-lumewarp-request-id": "request-123",
            },
            stream=SSEStream(),
        )

    mock_gateway(client, monkeypatch, gateway)
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        content=body,
    )
    assert response.status_code == 201
    assert response.content == b'data: {"choices": []}\n\ndata: [DONE]\n\n'
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["x-lumewarp-request-id"] == "request-123"
    row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
        )
    )
    assert row.details == {"key_id": str(own.id), "model": model.name}
    assert not client.app.state.playground_client.is_closed


async def test_body_limit_and_unreachable(client, committed, monkeypatch):
    own, _, headers, _ = await setup_keys(committed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    headers["x-lumewarp-act-as"] = str(own.id)
    path = "/api/v1/playground/v1/chat/completions"
    assert client.post(path, headers=headers, content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert (
        await committed.scalar(
            select(AuditLog).where(
                AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
            )
        )
        is None
    )

    def unavailable(request):
        raise httpx.ConnectError("private connection details", request=request)

    mock_gateway(client, monkeypatch, unavailable)
    response = client.post(path, headers=headers, json={"model": None})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "playground_gateway_unreachable"
    row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
        )
    )
    assert row.details == {"key_id": str(own.id), "model": None}
    assert not client.app.state.playground_client.is_closed

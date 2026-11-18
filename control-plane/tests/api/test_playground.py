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
from lumewarp_control_plane.models import (
    AuditLog,
    CatalogName,
    KeyLifecycle,
    PlaygroundRequest,
    ServiceAccount,
    TeamRole,
)
from tests.api.conftest import TEST_ENCRYPTION_KEY
from tests.api.helpers import auth, set_playground_url, user_with_token
from tests.factories import (
    make_alias,
    make_key,
    make_member,
    make_model,
    make_org,
    make_org_admin,
    make_service_account,
    make_team,
)


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


async def setup_keys(committed, *, role: TeamRole = TeamRole.MEMBER):
    team = await make_team(committed)
    user, pat = await user_with_token(committed)
    other, _ = await user_with_token(committed)
    await make_member(committed, team, user, role=role)
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


async def test_team_admin_sees_and_uses_service_account_key(client, committed, monkeypatch):
    own, excluded, headers, _ = await setup_keys(committed, role=TeamRole.ADMIN)
    foreign, service, *_ = excluded
    account = await committed.get(ServiceAccount, service.service_account_id)
    assert account is not None

    body = client.get("/api/v1/playground", headers=headers).json()
    by_id = {key["id"]: key for key in body["keys"]}
    assert set(by_id) == {str(own.id), str(service.id)}
    assert by_id[str(own.id)]["owner_kind"] == "user"
    assert by_id[str(own.id)]["service_account_name"] is None
    assert by_id[str(service.id)]["owner_kind"] == "service_account"
    assert by_id[str(service.id)]["service_account_name"] == account.name

    # A team admin still cannot act as another member's user key.
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(foreign.id)},
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "key not found"

    await set_playground_url(client, committed, "http://gateway:4000")
    request_id = uuid.uuid4()

    def gateway(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-lumewarp-request-id": str(request_id),
            },
            stream=SSEStream(),
        )

    mock_gateway(client, monkeypatch, gateway)
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(service.id)},
        json={"model": "mock"},
    )
    assert response.status_code == 200
    row = await committed.scalar(
        select(PlaygroundRequest).where(PlaygroundRequest.request_id == request_id)
    )
    assert row.user_id == own.user_id
    assert row.key_id == service.id


async def test_org_admin_inherits_service_account_key(client, committed, monkeypatch):
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    foreign_team = await make_team(committed)
    admin, _ = await user_with_token(committed)
    await make_org_admin(committed, org, admin)
    account = await make_service_account(committed, team, name="org-worker")
    key = await make_key(committed, team, account)
    foreign_account = await make_service_account(committed, foreign_team)
    foreign_key = await make_key(committed, foreign_team, foreign_account)
    cookie = await sessions.create_session(committed, admin.id, 3600)
    await committed.commit()
    headers = auth("session:" + cookie)

    body = client.get("/api/v1/playground", headers=headers).json()
    by_id = {entry["id"]: entry for entry in body["keys"]}
    assert set(by_id) == {str(key.id)}
    assert by_id[str(key.id)]["owner_kind"] == "service_account"
    assert by_id[str(key.id)]["service_account_name"] == "org-worker"
    assert by_id[str(key.id)]["org_id"] == str(org.id)
    assert by_id[str(key.id)]["org_name"] == org.name
    assert str(foreign_key.id) not in by_id

    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(foreign_key.id)},
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "key not found"

    await set_playground_url(client, committed, "http://gateway:4000")
    request_id = uuid.uuid4()

    def gateway(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-lumewarp-request-id": str(request_id),
            },
            stream=SSEStream(),
        )

    mock_gateway(client, monkeypatch, gateway)
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(key.id)},
        json={"model": "mock"},
    )
    assert response.status_code == 200
    row = await committed.scalar(
        select(PlaygroundRequest).where(PlaygroundRequest.request_id == request_id)
    )
    assert row.user_id == admin.id
    assert row.key_id == key.id


async def test_inactive_service_account_keys_are_hidden_and_refused(client, committed):
    team = await make_team(committed)
    archived = await make_team(committed)
    admin, _ = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    await make_member(committed, archived, admin, role=TeamRole.ADMIN)

    visible_account = await make_service_account(committed, team, name="visible-worker")
    visible = await make_key(committed, team, visible_account)
    deactivated_account = await make_service_account(committed, team, name="deactivated-worker")
    deactivated = await make_key(committed, team, deactivated_account)
    deactivated_account.deactivated_at = datetime.now(UTC)
    active_account = await make_service_account(committed, team, name="active-worker")
    now = datetime.now(UTC)
    revoked = await make_key(
        committed, team, active_account, lifecycle=KeyLifecycle.REVOKED, revoked_at=now
    )
    expired = await make_key(committed, team, active_account, expires_at=now - timedelta(seconds=1))
    archived_account = await make_service_account(committed, archived, name="archived-worker")
    archived_key = await make_key(committed, archived, archived_account)
    archived.deleted_at = now
    cookie = await sessions.create_session(committed, admin.id, 3600)
    await committed.commit()
    headers = auth("session:" + cookie)

    body = client.get("/api/v1/playground", headers=headers).json()
    by_id = {entry["id"]: entry for entry in body["keys"]}
    assert set(by_id) == {str(visible.id)}
    assert by_id[str(visible.id)]["service_account_name"] == "visible-worker"

    for key in (deactivated, revoked, expired, archived_key):
        response = client.post(
            "/api/v1/playground/v1/chat/completions",
            headers={**headers, "x-lumewarp-act-as": str(key.id)},
            json={},
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["message"] == "key not found"


@pytest.mark.parametrize("request_id", [None, "not-a-request-id"])
async def test_missing_or_invalid_request_id_still_succeeds(
    client, committed, monkeypatch, request_id
):
    own, _, headers, _ = await setup_keys(committed)
    await set_playground_url(client, committed, "http://gateway:4000")
    upstream_headers = {"content-type": "text/event-stream"}
    if request_id is not None:
        upstream_headers["x-lumewarp-request-id"] = request_id
    mock_gateway(
        client,
        monkeypatch,
        lambda request: httpx.Response(200, headers=upstream_headers, stream=SSEStream()),
    )
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={"model": "mock"},
    )
    assert response.status_code == 200
    assert response.content == b'data: {"choices": []}\n\ndata: [DONE]\n\n'
    assert (
        await committed.scalar(select(PlaygroundRequest).where(PlaygroundRequest.key_id == own.id))
        is None
    )


@pytest.mark.parametrize("code", ["invalid_api_key", "other_auth_error"])
async def test_gateway_auth_error(client, committed, monkeypatch, caplog, code):
    own, _, headers, _ = await setup_keys(committed)
    await set_playground_url(client, committed, "http://gateway:4000")
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


async def test_proxy_streams_and_records_request(client, committed, monkeypatch):
    own, _, headers, _ = await setup_keys(committed)
    await set_playground_url(client, committed, "http://gateway:4000")
    model = await make_model(committed)
    await committed.commit()
    body = (
        b'{"model":"mock", "messages":[{"role":"user","content":"private prompt"}],"stream":true}'
    )

    body = body.replace(b'"model":"mock"', ('"model":"' + model.name + '"').encode())
    request_id = uuid.uuid4()

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
                "x-lumewarp-request-id": str(request_id),
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
    assert response.headers["x-lumewarp-request-id"] == str(request_id)
    row = await committed.scalar(
        select(PlaygroundRequest).where(PlaygroundRequest.request_id == request_id)
    )
    assert row.user_id == own.user_id
    assert row.key_id == own.id
    audit_row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "playground.request", AuditLog.resource_id == str(own.id)
        )
    )
    assert audit_row is None
    assert not client.app.state.playground_client.is_closed


async def test_body_limit_and_unreachable(client, committed, monkeypatch):
    own, _, headers, _ = await setup_keys(committed)
    await set_playground_url(client, committed, "http://gateway:4000")
    headers["x-lumewarp-act-as"] = str(own.id)
    path = "/api/v1/playground/v1/chat/completions"
    assert client.post(path, headers=headers, content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert (
        await committed.scalar(select(PlaygroundRequest).where(PlaygroundRequest.key_id == own.id))
        is None
    )

    def unavailable(request):
        raise httpx.ConnectError("private connection details", request=request)

    mock_gateway(client, monkeypatch, unavailable)
    response = client.post(path, headers=headers, json={"model": None})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "playground_gateway_unreachable"
    row = await committed.scalar(
        select(PlaygroundRequest).where(PlaygroundRequest.key_id == own.id)
    )
    assert row is None
    assert not client.app.state.playground_client.is_closed


async def test_reasoning_levels_for_mapped_models_and_their_aliases(client, committed):
    own, _excluded, headers, _pat = await setup_keys(committed)
    fragments = {"low": {"reasoning_effort": "low"}, "xhigh": {"reasoning_effort": "xhigh"}}
    mapped = await make_model(
        committed,
        f"mapped-{uuid.uuid4().hex[:6]}",
        reasoning_mode="mapped",
        reasoning_fragments=fragments,
        reasoning_default_level="xhigh",
    )
    plain = await make_model(committed, f"plain-{uuid.uuid4().hex[:6]}")
    alias = await make_alias(committed, f"alias-{uuid.uuid4().hex[:6]}", mapped)
    await committed.commit()
    body = client.get("/api/v1/playground", headers=headers).json()
    names = set(body["keys"][0]["models"])
    reasoning = body["reasoning"]
    expected = {"levels": ["low", "xhigh"], "default_level": "xhigh"}
    name = lambda row: committed.get(CatalogName, row.name_id)  # noqa: E731
    mapped_name, alias_name, plain_name = [(await name(r)).name for r in (mapped, alias, plain)]
    assert {mapped_name, alias_name, plain_name} <= names
    assert sorted(reasoning[mapped_name]["levels"]) == expected["levels"]
    assert reasoning[mapped_name]["default_level"] == "xhigh"
    assert reasoning[alias_name] == reasoning[mapped_name]
    assert plain_name not in reasoning
    assert set(reasoning) <= names

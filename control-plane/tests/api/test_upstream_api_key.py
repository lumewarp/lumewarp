"""Upstream API keys are stored sealed, never returned, never audited, and reach the
snapshot only as ciphertext the gateway opens with LUMEWARP_ENCRYPTION_KEY.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lumewarp_control_plane import sealing
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.compiler.compile import compile_scope
from lumewarp_control_plane.models import AuditLog, Upstream
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import ADMIN_AUTH, suffix

BASE = "http://127.0.0.1:9/v1"
PLAINTEXT = "sk-private-upstream-value-7f3a"


def _create(client, **body):
    body = {"name": f"up-{suffix()}", "kind": "openai", "base_url": BASE, **body}
    return client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)


async def _row(committed, upstream_id: str) -> Upstream:
    committed.expire_all()
    return await committed.get(Upstream, uuid.UUID(upstream_id))


async def test_api_key_is_sealed_and_write_only(client, committed, api_settings) -> None:
    response = _create(client, api_key=PLAINTEXT)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["api_key_set"] is True
    assert body["api_key_ref"] is None
    assert PLAINTEXT not in response.text
    assert PLAINTEXT not in client.get(f"/api/v1/upstreams/{body['id']}", headers=ADMIN_AUTH).text

    row = await _row(committed, body["id"])
    assert row.api_key_ciphertext is not None
    assert PLAINTEXT.encode() not in row.api_key_ciphertext
    assert sealing.unseal(api_settings, row.api_key_ciphertext) == PLAINTEXT

    # Rotate: a new value, a fresh nonce.
    first = row.api_key_ciphertext
    response = client.patch(
        f"/api/v1/upstreams/{body['id']}", json={"api_key": "sk-other"}, headers=ADMIN_AUTH
    )
    assert response.status_code == 200, response.text
    row = await _row(committed, body["id"])
    assert row.api_key_ciphertext != first
    assert sealing.unseal(api_settings, row.api_key_ciphertext) == "sk-other"

    # A name update leaves the key alone.
    response = client.patch(
        f"/api/v1/upstreams/{body['id']}", json={"timeout_ms": 5}, headers=ADMIN_AUTH
    )
    assert response.json()["api_key_set"] is True

    # Switching to an env reference drops the stored key; null removes it.
    response = client.patch(
        f"/api/v1/upstreams/{body['id']}", json={"api_key_ref": "OPENAI_KEY"}, headers=ADMIN_AUTH
    )
    assert response.json()["api_key_set"] is False
    assert (await _row(committed, body["id"])).api_key_ciphertext is None
    client.patch(f"/api/v1/upstreams/{body['id']}", json={"api_key": "x1"}, headers=ADMIN_AUTH)
    row = await _row(committed, body["id"])
    assert row.api_key_ref is None and row.api_key_ciphertext is not None
    response = client.patch(
        f"/api/v1/upstreams/{body['id']}", json={"api_key": None}, headers=ADMIN_AUTH
    )
    assert response.json()["api_key_set"] is False

    audit = (
        await committed.scalars(select(AuditLog).where(AuditLog.resource_id == body["id"]))
    ).all()
    assert audit
    for entry in audit:
        assert PLAINTEXT not in str(entry.details)
        assert "sk-other" not in str(entry.details)
    assert any(e.details.get("credential_changed") for e in audit)


def test_both_sources_and_multiline_are_rejected(client) -> None:
    assert _create(client, api_key="a", api_key_ref="ENV").status_code == 422
    assert _create(client, api_key="a\nb").status_code == 422
    assert _create(client, api_key="").status_code == 422
    for bad in ("sk-\u0001x", "sk x", "sk-\u00e9", "sk-\tx"):
        response = _create(client, api_key=bad)
        assert response.status_code == 422, bad
        assert bad not in response.text


def test_api_key_needs_an_encryption_key(api_settings) -> None:
    settings = api_settings.model_copy(update={"encryption_key": None})
    with TestClient(create_app(settings)) as client:
        response = _create(client, api_key=PLAINTEXT)
    assert response.status_code == 422
    assert "LUMEWARP_ENCRYPTION_KEY" in response.text
    assert PLAINTEXT not in response.text


@pytest.mark.parametrize("sealed", [True, False])
async def test_snapshot_carries_ciphertext_only(client, committed, api_settings, sealed) -> None:
    body = _create(client, **({"api_key": PLAINTEXT} if sealed else {})).json()
    committed.expire_all()
    compiled = await compile_scope(committed, api_settings.scope, require_local_membership=True)
    (up,) = [u for u in compiled.snapshot.upstreams if u.name == body["name"]]
    assert PLAINTEXT.encode() not in compiled.snapshot.SerializeToString()
    if sealed:
        assert sealing.unseal(api_settings, up.api_key_ciphertext) == PLAINTEXT
        assert "encrypted_api_key" in compiled.snapshot.required_capabilities
    else:
        assert up.api_key_ciphertext == b""


def test_unseal_rejects_another_key(api_settings) -> None:
    sealed = sealing.seal(api_settings, PLAINTEXT)
    other = Settings(encryption_key="another-key-0123456789abcdef0123456")
    with pytest.raises(sealing.SealedInvalid):
        sealing.unseal(other, sealed)
    with pytest.raises(sealing.SealedInvalid):
        sealing.unseal(api_settings, b"\x02" + sealed[1:])

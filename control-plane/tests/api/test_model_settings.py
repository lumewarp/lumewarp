"""Per-model settings: token limits, the default request body and reasoning-effort handling.
They round-trip through the API, are validated on write, compile into the snapshot with
their capabilities only when set, and are audited as flags, never as values.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.compile import compile_scope
from lumewarp_control_plane.models import AuditLog
from tests.api.helpers import ADMIN_AUTH as H
from tests.api.helpers import suffix

API = "/api/v1"


def _nest(levels: int) -> dict:
    value: object = 1
    for _ in range(levels):
        value = {"a": value}
    return value  # type: ignore[return-value]


DEEP = _nest(33)
QWEN = {
    "none": {"chat_template_kwargs": {"enable_thinking": False}},
    "high": {"chat_template_kwargs": {"enable_thinking": True}},
}


def _upstream(client) -> str:
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"}
    response = client.post(f"{API}/upstreams", json=body, headers=H)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create(client, **settings):
    body = {
        "name": f"m-{suffix()}",
        "upstream_id": _upstream(client),
        "upstream_model": "Qwen/Qwen3-8B",
        **settings,
    }
    return client.post(f"{API}/models", json=body, headers=H)


async def _wire(committed, api_settings, name: str):
    committed.expire_all()
    compiled = await compile_scope(committed, api_settings.scope, require_local_membership=True)
    (model,) = [m for m in compiled.snapshot.models if m.name == name]
    return compiled.snapshot, model


async def test_settings_round_trip_and_compile(client, committed, api_settings) -> None:
    response = _create(
        client,
        max_input_tokens=32768,
        max_output_tokens=4096,
        default_body={"top_k": 20, "extra_body": {"repetition_penalty": 1.05}},
        reasoning={"mode": "mapped", "fragments": QWEN, "default_level": "none"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["max_input_tokens"] == 32768
    assert body["max_output_tokens"] == 4096
    assert body["default_body"] == {"top_k": 20, "extra_body": {"repetition_penalty": 1.05}}
    assert body["reasoning"] == {"mode": "mapped", "fragments": QWEN, "default_level": "none"}
    assert (
        client.get(f"{API}/models/{body['id']}", headers=H).json()["reasoning"] == body["reasoning"]
    )

    snapshot, model = await _wire(committed, api_settings, body["name"])
    assert model.max_output_tokens == 4096
    assert json.loads(model.default_body_json) == body["default_body"]
    assert model.reasoning_effort.mode == pb.REASONING_EFFORT_MODE_MAPPED
    assert model.reasoning_effort.default_level == "none"
    assert {k: json.loads(v) for k, v in model.reasoning_effort.fragments_json.items()} == QWEN
    for capability in ("model_limits", "default_body", "reasoning_effort"):
        assert capability in snapshot.required_capabilities

    # PATCH: null removes each setting; omitted fields stay.
    response = client.patch(
        f"{API}/models/{body['id']}",
        json={"default_body": None, "reasoning": None, "max_output_tokens": None},
        headers=H,
    )
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["default_body"] is None and out["max_output_tokens"] is None
    assert out["max_input_tokens"] == 32768
    assert out["reasoning"] == {"mode": "passthrough", "fragments": None, "default_level": None}
    _, model = await _wire(committed, api_settings, body["name"])
    assert not model.HasField("reasoning_effort")
    assert model.default_body_json == "" and model.max_output_tokens == 0


async def test_unset_settings_add_no_capability(client, committed, api_settings) -> None:
    body = _create(client, max_input_tokens=8192).json()
    snapshot, model = await _wire(committed, api_settings, body["name"])
    assert model.max_output_tokens == 0 and not model.HasField("reasoning_effort")
    # max_input_tokens is informational: it is not in the snapshot and needs nothing.
    others = [m for m in snapshot.models if m.name != body["name"]]
    if not any(m.max_output_tokens for m in others):
        assert "model_limits" not in snapshot.required_capabilities


async def test_strip_and_passthrough_default_level(client, committed, api_settings) -> None:
    strip = _create(client, reasoning={"mode": "strip"}).json()
    _, model = await _wire(committed, api_settings, strip["name"])
    assert model.reasoning_effort.mode == pb.REASONING_EFFORT_MODE_STRIP

    passthrough = _create(client, reasoning={"mode": "passthrough", "default_level": "low"}).json()
    _, model = await _wire(committed, api_settings, passthrough["name"])
    assert model.reasoning_effort.mode == pb.REASONING_EFFORT_MODE_PASSTHROUGH
    assert model.reasoning_effort.default_level == "low"


@pytest.mark.parametrize(
    ("settings", "needle"),
    [
        ({"default_body": {"model": "x"}}, "must not set 'model'"),
        ({"default_body": {"stream": True}}, "must not set 'stream'"),
        ({"default_body": {"pad": "x" * 17000}}, "exceeds 16 KiB"),
        ({"default_body": {"reasoning_effort": "high"}}, "must not set 'reasoning_effort'"),
        ({"default_body": DEEP}, "nests deeper than 32 levels"),
        (
            {"reasoning": {"mode": "mapped", "fragments": {"high": DEEP}}},
            "nests deeper than 32 levels",
        ),
        ({"reasoning": {"mode": "mapped"}}, "needs at least one level"),
        ({"reasoning": {"mode": "mapped", "fragments": {"ultra": {}}}}, "ultra"),
        (
            {"reasoning": {"mode": "mapped", "fragments": {"low": {"stream_options": {}}}}},
            "must not set 'stream_options'",
        ),
        (
            {"reasoning": {"mode": "mapped", "fragments": QWEN, "default_level": "low"}},
            "default_level has no mapping",
        ),
        ({"reasoning": {"mode": "strip", "default_level": "low"}}, "takes no default_level"),
        ({"reasoning": {"mode": "strip", "fragments": QWEN}}, "takes no level mapping"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
    ],
)
def test_invalid_settings_are_rejected(client, settings, needle) -> None:
    response = _create(client, **settings)
    assert response.status_code == 422, response.text
    assert needle in response.text


async def test_audit_records_flags_not_values(client, committed) -> None:
    body = _create(client).json()
    response = client.patch(
        f"{API}/models/{body['id']}",
        json={
            "max_output_tokens": 2048,
            "default_body": {"temperature": 0.2, "user": "secret-looking-value"},
            "reasoning": {"mode": "mapped", "fragments": QWEN},
        },
        headers=H,
    )
    assert response.status_code == 200, response.text
    committed.expire_all()
    entry = (
        await committed.scalars(
            select(AuditLog)
            .where(AuditLog.resource_id == body["id"], AuditLog.action == "model.update")
            .order_by(AuditLog.id.desc())
        )
    ).first()
    changes = entry.details["changes"]
    assert changes["max_output_tokens"] == 2048
    assert changes["default_body_changed"] is True
    assert changes["reasoning_changed"] is True
    assert "secret-looking-value" not in json.dumps(entry.details)
    assert "enable_thinking" not in json.dumps(entry.details)


def test_thirty_two_levels_are_accepted(client) -> None:
    assert _create(client, default_body=_nest(32)).status_code == 201

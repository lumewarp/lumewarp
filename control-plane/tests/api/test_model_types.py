"""Model families are explicit, homogeneous in routers and historical in usage."""

import json
import struct

import httpx
import pytest
from sqlalchemy import select

from lumewarp_control_plane import upstream_client
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.validate import (
    SnapshotInvalid,
    capabilities_used,
    validate_snapshot,
)
from lumewarp_control_plane.models import UsageEvent
from lumewarp_control_plane.usage import ingest
from tests.api.helpers import ADMIN_AUTH, suffix
from tests.api.test_model_health import _model, _test, _upstream
from tests.api.test_usage import query
from tests.usage.test_catalog_provenance import TS, key_for, wire


@pytest.mark.parametrize("kind", ["llm", "embedding", "transcription", "decision"])
def test_explicit_type_round_trip(client, kind):
    up = _upstream(client)
    body = {"name": suffix(), "upstream_id": up["id"], "upstream_model": "m"}
    assert client.post("/api/v1/models", json=body, headers=ADMIN_AUTH).status_code == 422
    model = _model(client, up, model_type=kind)
    assert model["model_type"] == kind
    alias = client.post(
        "/api/v1/aliases", json={"name": suffix(), "target": model["id"]}, headers=ADMIN_AUTH
    )
    assert alias.status_code == 201, alias.text
    assert alias.json()["model_type"] == kind
    router = client.post(
        "/api/v1/routers",
        json={"name": suffix(), "members": [{"model_id": model["id"]}]},
        headers=ADMIN_AUTH,
    )
    assert router.status_code == 201, router.text
    assert router.json()["model_type"] == kind
    entries = client.get("/api/v1/catalog?limit=500", headers=ADMIN_AUTH).json()["items"]
    assert next(e for e in entries if e["id"] == model["id"])["model_type"] == kind


@pytest.mark.parametrize(
    ("kind", "settings"),
    [
        ("embedding", {"price_input_nano_usd_per_mtok": 1, "price_output_nano_usd_per_mtok": 1}),
        ("embedding", {"price_output_nano_usd_per_mtok": 0}),
        ("decision", {"price_input_nano_usd_per_mtok": 1}),
        ("transcription", {"price_output_nano_usd_per_mtok": 1}),
        ("llm", {"price_nano_usd_per_audio_second": 0}),
        ("embedding", {"price_nano_usd_per_audio_second": 1}),
        ("transcription", {"default_body": {"language": "en"}}),
        ("transcription", {"max_output_tokens": 10}),
        ("embedding", {"reasoning": {"mode": "strip"}}),
        ("decision", {"max_output_tokens": 10}),
    ],
)
def test_invalid_type_settings_on_create_and_patch(client, kind, settings):
    up = _upstream(client)
    body = {
        "name": suffix(),
        "upstream_id": up["id"],
        "upstream_model": "m",
        "model_type": kind,
        **settings,
    }
    assert client.post("/api/v1/models", json=body, headers=ADMIN_AUTH).status_code == 422
    model = _model(client, up, model_type=kind)
    response = client.patch(f"/api/v1/models/{model['id']}", json=settings, headers=ADMIN_AUTH)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("kind", "settings"),
    [
        ("embedding", {"price_input_nano_usd_per_mtok": 1}),
        ("embedding", {"price_input_nano_usd_per_mtok": 1, "price_output_nano_usd_per_mtok": 0}),
        ("transcription", {"price_nano_usd_per_audio_second": 123}),
        (
            "transcription",
            {"price_input_nano_usd_per_mtok": 1, "price_output_nano_usd_per_mtok": 2},
        ),
        (
            "transcription",
            {
                "price_input_nano_usd_per_mtok": 1,
                "price_output_nano_usd_per_mtok": 2,
                "price_nano_usd_per_audio_second": 3,
            },
        ),
    ],
)
def test_valid_type_prices(client, kind, settings):
    model = _model(client, _upstream(client), model_type=kind, **settings)
    assert all(model[key] == value for key, value in settings.items())


def test_type_change_merges_settings_before_any_autoflush(client):
    up = _upstream(client)
    other = _upstream(client)
    model = _model(client, up, max_output_tokens=10, reasoning={"mode": "strip"})
    alias = client.post(
        "/api/v1/aliases", json={"name": suffix(), "target": model["id"]}, headers=ADMIN_AUTH
    ).json()
    response = client.patch(
        f"/api/v1/models/{model['id']}",
        json={
            "model_type": "embedding",
            "upstream_id": other["id"],
            "max_output_tokens": None,
            "reasoning": None,
            "price_input_nano_usd_per_mtok": 1,
        },
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["model_type"] == "embedding"
    assert (
        client.get(f"/api/v1/aliases/{alias['id']}", headers=ADMIN_AUTH).json()["model_type"]
        == "embedding"
    )
    response = client.patch(
        f"/api/v1/models/{model['id']}",
        json={
            "model_type": "transcription",
            "upstream_id": up["id"],
            "price_input_nano_usd_per_mtok": None,
            "price_nano_usd_per_audio_second": 1,
        },
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("edge", ["members", "split", "fallback"])
def test_router_homogeneity_and_referenced_type_change(client, edge):
    up = _upstream(client)
    a = _model(client, up)
    b = _model(client, up)
    body = {"name": suffix(), "members": [{"model_id": a["id"]}]}
    if edge == "members":
        body["members"].append({"model_id": b["id"]})
    elif edge == "split":
        body["members"] = []
        body["traffic_split"] = [
            {"name": "one", "weight": 1, "members": [{"model_id": a["id"]}]},
            {"name": "two", "weight": 1, "members": [{"model_id": b["id"]}]},
        ]
    else:
        body.update(retry={"max_attempts": 2}, fallback_model_ids=[b["id"]])
    created = client.post("/api/v1/routers", json=body, headers=ADMIN_AUTH)
    assert created.status_code == 201, created.text
    response = client.patch(
        f"/api/v1/models/{b['id']}", json={"model_type": "embedding"}, headers=ADMIN_AUTH
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_type_in_use"
    assert (
        client.delete(f"/api/v1/routers/{created.json()['id']}", headers=ADMIN_AUTH).status_code
        == 204
    )
    assert (
        client.patch(
            f"/api/v1/models/{b['id']}", json={"model_type": "embedding"}, headers=ADMIN_AUTH
        ).status_code
        == 200
    )
    body["name"] = suffix()
    response = client.post("/api/v1/routers", json=body, headers=ADMIN_AUTH)
    assert response.status_code == 422, response.text
    assert "share one model_type" in response.text


@pytest.mark.parametrize("kind", ["embedding", "decision", "transcription"])
def test_test_now_uses_type_probe(client, monkeypatch, kind):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"text": ""})

    monkeypatch.setattr(upstream_client, "transport", httpx.MockTransport(handler))
    response = _test(client, _upstream(client), model_type=kind)
    assert response.status_code == 200, response.text
    request = calls[0]
    if kind == "transcription":
        assert request.url.path == "/v1/audio/transcriptions"
        raw = request.content
        assert b'filename="probe.wav"' in raw and b"audio/wav" in raw
        start = raw.index(b"RIFF")
        wav = raw[start : start + 32044]
        assert len(wav) == 32044 and wav[44:] == bytes(32000)
        assert struct.unpack("<I", wav[24:28])[0] == 16000
        assert b"response_format" in raw and b"json" in raw
    else:
        assert request.url.path == ("/v1/embeddings" if kind == "embedding" else "/v1/decisions")
        body = json.loads(request.content)
        assert body["input"] == "ping" and body["model"] == "qwen"
        if kind == "decision":
            assert body["questions"] == [
                {"type": "predicate", "name": "ok", "instructions": "Is the input a word?"}
            ]


@pytest.mark.parametrize(
    "problem", ["missing_type", "mixed", "audio", "output", "reasoning", "body", "embedding_price"]
)
def test_snapshot_type_validation(problem):
    snap = pb.Snapshot(
        upstreams=[pb.Upstream(name="u", kind=pb.UPSTREAM_KIND_VLLM, base_url="http://u/v1")],
        models=[
            pb.Model(name=name, upstream="u", upstream_model="m", type=pb.MODEL_TYPE_LLM)
            for name in ("a", "b")
        ],
        routers=[
            pb.Router(name="r", strategy="weighted", members=[pb.RouterMember(model="a", weight=1)])
        ],
    )
    if problem == "missing_type":
        snap.models[0].type = pb.MODEL_TYPE_UNSPECIFIED
    elif problem == "mixed":
        snap.models[1].type = pb.MODEL_TYPE_EMBEDDING
        snap.routers[0].fallbacks.append("b")
    elif problem == "audio":
        snap.models[0].price.nano_usd_per_audio_second = 1
    else:
        model = snap.models[0]
        model.type = pb.MODEL_TYPE_TRANSCRIPTION
        if problem == "output":
            model.max_output_tokens = 1
        elif problem == "reasoning":
            model.reasoning_effort.mode = pb.REASONING_EFFORT_MODE_STRIP
        elif problem == "body":
            model.default_body_json = "{}"
        else:
            model.type = pb.MODEL_TYPE_EMBEDDING
            model.price.output_nano_usd_per_mtok = 1
    snap.required_capabilities.extend(capabilities_used(snap))
    with pytest.raises(SnapshotInvalid):
        validate_snapshot(snap, max_bytes=1 << 26, max_keys=100)


async def test_audio_ingest_dimensions_and_filters(client, committed):
    key = await key_for(committed)
    event = wire(key, timestamp=TS, endpoint="audio_transcriptions", audio_ms=1250)
    await ingest(committed, "gw", [event])
    await committed.commit()
    stored = await committed.scalar(
        select(UsageEvent).where(UsageEvent.request_id == event.request_id)
    )
    assert stored.audio_ms == 1250
    for params in (
        {"group_by": "model_type"},
        {"group_by": "endpoint", "model_type": "transcription"},
        {"group_by": "model", "endpoint": "audio_transcriptions"},
    ):
        response = query(client, **params)
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert sum(row["audio_ms"] for row in rows) == 1250
        assert sum(row["cost_nano_usd"] for row in rows) == 123
    assert query(client, model_type="embedding").json()["items"] == []
    assert query(client, group_by="team").json()["items"][0]["audio_ms"] == 1250


async def test_nullable_type_dimension_paginates(client, committed):
    from lumewarp_control_plane.models import TrafficKind
    from tests.api.helpers import usage_event

    key = await key_for(committed)
    for endpoint, kind in [
        ("mcp", TrafficKind.MCP),
        ("embeddings", TrafficKind.LLM),
        ("responses", TrafficKind.LLM),
    ]:
        event = usage_event(key, ts=TS, kind=kind)
        event.endpoint = endpoint
        committed.add(event)
    await committed.commit()
    rows = []
    cursor = None
    while True:
        params = {"team_id": str(key.team_id), "group_by": "model_type", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = query(client, **params)
        assert response.status_code == 200, response.text
        rows.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if not cursor:
            break
    assert {row["model_type"] for row in rows} == {None, "llm", "embedding"}
    assert sum(row["requests"] for row in rows) == 3

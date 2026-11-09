"""Typed model endpoints, probes and billing through the real API/config stream."""

import io
import json
import uuid
import wave
from collections import defaultdict

import httpx
from stack import pg, wait_for

TYPES = ("llm", "embedding", "transcription", "decision")
ENDPOINTS = (
    ("/v1/chat/completions", "chat_completions", "llm"),
    ("/v1/completions", "completions", "llm"),
    ("/v1/responses", "responses", "llm"),
    ("/v1/embeddings", "embeddings", "embedding"),
    ("/v1/audio/transcriptions", "audio_transcriptions", "transcription"),
    ("/v1/audio/translations", "audio_translations", "transcription"),
    ("/v1/decisions", "decisions", "decision"),
    ("/v1/systemone", "systemone", "decision"),
)


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(32000))
    return out.getvalue()


def _call(client, gw, path, model, *, stream=False):
    if path.startswith("/v1/audio/"):
        return client.post(
            gw.url(path),
            data={"model": model, "stream": str(stream).lower()},
            files={"file": ("sample.wav", _wav(), "audio/wav")},
        )
    return client.post(
        gw.url(path),
        json={
            "model": model,
            "stream": stream,
            "messages": [{"role": "user", "content": "ping"}],
            "prompt": "ping",
            "input": "ping",
            "previous_response_id": "unchanged-response-id",
            "questions": [
                {"type": "predicate", "name": "ok", "id": "ok", "instructions": "Is this a word?"}
            ],
        },
    )


def _events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]


def test_model_types_endpoints_usage_and_probes(stack):
    seeded = stack.seed()
    upstream = next(u for u in stack.ok("GET", "/upstreams")["items"] if u["name"] == "mock")
    models = {}
    for kind in TYPES:
        body = {
            "name": f"typed-{kind}",
            "upstream_id": upstream["id"],
            "upstream_model": f"upstream-{kind}",
            "model_type": kind,
            "price_input_nano_usd_per_mtok": 1_000_000_000,
            "price_output_nano_usd_per_mtok": 0 if kind == "embedding" else 2_000_000_000,
        }
        if kind == "transcription":
            body["price_nano_usd_per_audio_second"] = 6_000_000
        models[kind] = stack.ok("POST", "/models", json=body)
        router = stack.ok(
            "POST",
            "/routers",
            json={"name": f"pool-{kind}", "members": [{"model_id": models[kind]["id"]}]},
        )
        assert router["model_type"] == kind
        alias = stack.ok("POST", "/aliases", json={"name": f"alias-{kind}", "target": router["id"]})
        assert alias["model_type"] == kind

    # The same transcription type can report either duration or tokens.
    token_model = stack.ok(
        "POST",
        "/models",
        json={
            "name": "audio-tokens",
            "upstream_id": upstream["id"],
            "upstream_model": "transcription-tokens",
            "model_type": "transcription",
            "price_input_nano_usd_per_mtok": 1_000_000_000,
            "price_output_nano_usd_per_mtok": 2_000_000_000,
            "price_nano_usd_per_audio_second": 6_000_000,
        },
    )
    assert token_model["model_type"] == "transcription"
    mixed = stack.api(
        "POST",
        "/routers",
        json={
            "name": "mixed",
            "members": [{"model_id": models[k]["id"]} for k in ("llm", "embedding")],
        },
    )
    assert mixed.status_code == 422, mixed.text

    gw = stack.start_gateway()
    headers = {"authorization": f"Bearer {seeded.a.secret}"}
    with httpx.Client(headers=headers, timeout=15) as client:
        expected_names = {
            **{f"typed-{k}": k for k in TYPES},
            **{f"pool-{k}": k for k in TYPES},
            **{f"alias-{k}": k for k in TYPES},
            "audio-tokens": "transcription",
        }

        def listed():
            response = client.get(gw.url("/v1/models"))
            if response.status_code != 200:
                return False
            entries = {m["id"]: m["type"] for m in response.json()["data"]}
            return all(entries.get(name) == kind for name, kind in expected_names.items())

        wait_for(listed, 15, "all typed models, routers and aliases in /v1/models")
        expected = {}

        def account(response, endpoint, kind, prompt, completion, audio_ms=0):
            assert response.status_code == 200, response.text
            request_id = uuid.UUID(response.headers["x-lumewarp-request-id"])
            cost = prompt * 1000 + completion * 2000 + audio_ms * 6000
            expected[request_id] = (endpoint, kind, prompt, completion, audio_ms, cost)

        for path, endpoint, kind in ENDPOINTS:
            name = f"alias-{kind}" if endpoint == "responses" else f"pool-{kind}"
            response = _call(client, gw, path, name)
            assert response.status_code == 200, response.text
            body = response.json()
            if kind != "transcription":
                assert body["model"] == name
            if kind == "transcription":
                assert body["usage"] == {"type": "duration", "seconds": 1.5}
                account(response, endpoint, kind, 0, 0, 1500)
            else:
                completion = 0 if kind == "embedding" else 2 if kind == "decision" else 4
                account(response, endpoint, kind, 8, completion)

        response = _call(client, gw, "/v1/responses", "alias-llm", stream=True)
        events = _events(response)
        assert events[0]["type"] == "response.created"
        assert events[-1]["type"] == "response.completed"
        nested = [event["response"] for event in events if "response" in event]
        assert len(nested) >= 2 and all(body["model"] == "alias-llm" for body in nested)
        assert nested[-1]["usage"] == {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}
        account(response, "responses", "llm", 8, 4)

        response = _call(client, gw, "/v1/audio/transcriptions", "audio-tokens")
        assert response.json()["usage"]["type"] == "tokens"
        account(response, "audio_transcriptions", "transcription", 8, 4)
        response = _call(client, gw, "/v1/audio/transcriptions", "pool-transcription", stream=True)
        events = _events(response)
        assert events[-1]["type"] == "transcript.text.done"
        assert events[-1]["usage"]["input_tokens"] == 8
        account(response, "audio_transcriptions", "transcription", 8, 4)

        # Test now calls the upstream directly, without adding metered gateway traffic.
        for kind in TYPES:
            result = stack.ok(
                "POST",
                "/models/test",
                json={
                    "upstream_id": upstream["id"],
                    "upstream_model": models[kind]["upstream_model"],
                    "model_type": kind,
                },
            )
            assert result["probe"]["ok"] and result["probe"]["status"] == 200, result
            suffix = {
                "llm": "/chat/completions",
                "embedding": "/embeddings",
                "transcription": "/audio/transcriptions",
                "decision": "/decisions",
            }[kind]
            assert result["probe"]["url"].endswith(suffix)

        def ingested():
            rows = pg(
                stack.dsn, "SELECT * FROM usage_events WHERE key_id = $1", uuid.UUID(seeded.a.id)
            )
            return rows if {row["request_id"] for row in rows} >= expected.keys() else None

        rows = wait_for(ingested, 20, "typed usage events ingested")
        assert len(rows) == len(expected), "probes must not be metered"
        for row in rows:
            endpoint, kind, prompt, completion, audio_ms, cost = expected[row["request_id"]]
            assert row["endpoint"] == endpoint
            assert (row["prompt_tokens"], row["completion_tokens"], row["audio_ms"]) == (
                prompt,
                completion,
                audio_ms,
            )
            assert row["cost_known"] and row["cost_nano_usd"] == cost
            assert row["usage_state"] == "reported"

        for dimensions in ("model_type", "endpoint", "model_type,endpoint"):
            dimensions = dimensions.split(",")
            totals = defaultdict(lambda: [0, 0, 0, 0, 0])
            for endpoint, kind, prompt, completion, audio_ms, cost in expected.values():
                key = tuple({"endpoint": endpoint, "model_type": kind}[d] for d in dimensions)
                for i, value in enumerate((1, prompt, completion, audio_ms, cost)):
                    totals[key][i] += value
            items = stack.ok(
                "GET",
                "/usage",
                params={"team_id": seeded.team_id, "group_by": ",".join(dimensions)},
            )["items"]
            actual = {
                tuple(item[d] for d in dimensions): [
                    item[field]
                    for field in (
                        "requests",
                        "prompt_tokens",
                        "completion_tokens",
                        "audio_ms",
                        "cost_nano_usd",
                    )
                ]
                for item in items
            }
            assert actual == dict(totals)
        filtered = stack.ok(
            "GET",
            "/usage",
            params={
                "team_id": seeded.team_id,
                "model_type": "transcription",
                "endpoint": "audio_translations",
            },
        )["items"]
        assert (
            len(filtered) == 1
            and filtered[0]["audio_ms"] == 1500
            and filtered[0]["cost_nano_usd"] == 9_000_000
        )

        # Check mismatch after the successful aggregation so rejected requests cannot
        # obscure exact token, duration and cost totals.
        rejected = {}
        for path, endpoint, kind in ENDPOINTS:
            wrong = "embedding" if kind == "llm" else "llm"
            for prefix in ("typed", "pool", "alias"):
                name = f"{prefix}-{wrong}"
                response = _call(client, gw, path, name)
                assert response.status_code == 400, response.text
                assert response.json()["error"]["code"] == "model_type_mismatch"
                assert (
                    response.json()["error"]["message"] == f'Model "{name}" does not serve {path}'
                )
                rejected[uuid.UUID(response.headers["x-lumewarp-request-id"])] = (name, endpoint)

        def rejections_ingested():
            rows = pg(
                stack.dsn, "SELECT * FROM usage_events WHERE key_id = $1", uuid.UUID(seeded.a.id)
            )
            return rows if {row["request_id"] for row in rows} >= rejected.keys() else None

        for row in wait_for(rejections_ingested, 20, "typed endpoint rejection events"):
            if row["request_id"] not in rejected:
                continue
            assert (row["requested"], row["endpoint"]) == rejected[row["request_id"]]
            assert row["outcome"] == "rejected" and row["status"] == 400
            assert row["audio_ms"] == row["prompt_tokens"] == row["completion_tokens"] == 0
        rejected_metric = sum(
            float(line.rsplit(" ", 1)[1])
            for line in gw.metrics().splitlines()
            if line.startswith("lumewarp_requests_rejected_total{")
            and 'reason="model_type_mismatch"' in line
        )
        assert rejected_metric == len(rejected) == 24
        for method in ("GET", "DELETE"):
            assert (
                client.request(method, gw.url("/v1/responses/unchanged-response-id")).status_code
                == 404
            )

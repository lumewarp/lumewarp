"""P2 routing (W5) through the control plane: retries, fallbacks and circuit breakers
configured over the admin API, compiled into the snapshot and served by a real gateway
against real mock upstreams. No IdP needed; runs under scripts/e2e-p1.sh.

The "bad" mock fails every inference request with 503 (`--fail-every 1`); the stack's
default mock is healthy. The gateway's retry budget starts with three retries, so each
test keeps its retried requests at or below that.
"""

import json

import httpx
from p2_events import DatabaseEvents
from stack import free_port, pg, wait_for

BAD_WEIGHT = 1000  # first attempts land on the bad deployment ~always


def test_unregistered_custom_strategy_nacks_and_keeps_last_snapshot(stack):
    seeded = stack.seed()
    with DatabaseEvents(stack.dsn) as events:
        gw = stack.start_gateway()
        events.wait(
            lambda: (stack.gateway_status() or {}).get("acked_version"),
            what="initial snapshot ACK",
        )
        previous = stack.gateway_status()["acked_version"]
        assert not any(
            c.startswith("strategy:") for c in stack.gateway_status()["capabilities"]
        )
        upstream = _upstream_id(stack, "mock")
        dep = _deployment(stack, "custom-deployment", upstream)
        stack.ok(
            "POST",
            "/routes",
            json={
                "model": "custom-test",
                "deployment_ids": [dep],
                "strategy": "custom",
                "custom_strategy": "header_pin",
                "strategy_params": {"headers": ["x-pin"]},
            },
        )
        revision = pg(
            stack.dsn, "SELECT source_revision FROM scopes WHERE name = 'e2e'"
        )[0][0]
        events.wait(
            lambda: pg(
                stack.dsn,
                "SELECT version FROM config_versions WHERE source_revision >= $1",
                revision,
            ),
            what="custom strategy compilation",
        )
        version = stack.current_version()
        events.wait(
            lambda: (stack.gateway_status() or {}).get("last_nack_version") == version,
            what="unregistered custom strategy NACK",
        )
        status = stack.gateway_status()
        assert status["last_nack_code"] == "NACK_CODE_UNSUPPORTED_FEATURE"
        assert previous <= status["acked_version"] < version
    assert gw.chat(seeded.a.secret).status_code == 200


def _upstream_id(stack, name: str) -> str:
    return next(
        u["id"] for u in stack.ok("GET", "/upstreams")["items"] if u["name"] == name
    )


def _bad_upstream(stack, **settings) -> str:
    port = stack.start_mock("mock-bad", "--fail-every", "1", "--fail-status", "503")
    return stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "bad",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{port}/v1",
            **settings,
        },
    )["id"]


def _deployment(stack, name: str, upstream_id: str, weight: int = 1) -> str:
    return stack.ok(
        "POST",
        "/deployments",
        json={
            "name": name,
            "upstream_id": upstream_id,
            "upstream_model": "mock",
            "weight": weight,
            "price_input_nano_usd_per_mtok": 1_000_000_000,
            "price_output_nano_usd_per_mtok": 2_000_000_000,
        },
    )["id"]


def _wait_served(gw, key: str, *models: str) -> None:
    """Wait until the gateway's snapshot has the routes (no inference traffic)."""

    def listed():
        r = httpx.get(
            gw.url("/v1/models"), headers={"authorization": f"Bearer {key}"}, timeout=2
        )
        return r.status_code == 200 and set(models) <= {
            m["id"] for m in r.json()["data"]
        }

    wait_for(listed, 15, f"gateway serving {models}")


def _series(gw, name: str) -> dict[str, float]:
    out = {}
    for line in gw.metrics().splitlines():
        if line.startswith(name + "{"):
            labels, value = line.rsplit(" ", 1)
            out[labels.removeprefix(name)] = float(value)
    return out


def _attempts(gw, deployment: str, outcome: str | None = None) -> float:
    """Requests actually sent to `deployment` (or with exactly `outcome`). An open
    breaker skips a candidate with outcome="circuit_open", which sends nothing.
    """
    return sum(
        v
        for labels, v in _series(gw, "lumewarp_upstream_attempts_total").items()
        if f'deployment="{deployment}"' in labels
        and (
            f'outcome="{outcome}"' in labels
            if outcome
            else 'outcome="circuit_open"' not in labels
        )
    )


def _failed_attempts(gw, deployment: str) -> float:
    return sum(
        v
        for labels, v in _series(gw, "lumewarp_upstream_attempts_total").items()
        if f'deployment="{deployment}"' in labels
        and not any(f'outcome="{o}"' in labels for o in ("success", "circuit_open"))
    )


def _settled_usage(stack, gw, count: int) -> list:
    gw.drain_usage()
    rows = stack.usage_rows()
    assert len(rows) == count, "exactly one ingested usage event per completed request"
    assert len({row["request_id"] for row in rows}) == count
    return rows


def test_retry_to_second_deployment_records_one_usage_event(stack):
    seeded = stack.seed()
    bad = _bad_upstream(stack)
    good = _upstream_id(stack, "mock")
    ids = [
        _deployment(stack, "bad-a", bad, BAD_WEIGHT),
        _deployment(stack, "good-a", good),
    ]
    stack.ok(
        "POST",
        "/routes",
        json={"model": "retry", "deployment_ids": ids, "retry": {"max_attempts": 2}},
    )
    gw = stack.start_gateway()
    _wait_served(gw, seeded.a.secret, "retry")

    for _ in range(3):
        r = gw.chat(seeded.a.secret, model="retry")
        assert r.status_code == 200, r.text
    assert _failed_attempts(gw, "bad-a") >= 1, "the bad deployment was tried first"
    assert _attempts(gw, "good-a") == 3
    rows = _settled_usage(stack, gw, 3)
    assert len(rows) == 3, "exactly one usage event per client request"
    for row in rows:
        assert (row["status"], row["model"], row["deployment"]) == (
            200,
            "retry",
            "good-a",
        )
        assert row["cost_known"] and row["prompt_tokens"] > 0


def test_fallback_route_serves_with_original_model(stack):
    seeded = stack.seed()
    bad = _bad_upstream(stack)
    good = _upstream_id(stack, "mock")
    # Nothing listens there: connect errors, which streams may retry too (a 503 with
    # headers ends a stream's attempts, see gateway/README.md "Retry and health").
    down = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "down",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{free_port()}/v1",
        },
    )["id"]
    backup = stack.ok(
        "POST",
        "/routes",
        json={
            "model": "backup",
            "deployment_ids": [_deployment(stack, "good-a", good)],
        },
    )
    primaries = {}
    for model, upstream in (("primary", bad), ("primary-down", down)):
        primaries[model] = stack.ok(
            "POST",
            "/routes",
            json={
                "model": model,
                "deployment_ids": [_deployment(stack, f"{model}-a", upstream)],
                # Fallbacks share the route's attempt budget: primary + fallback.
                "retry": {"max_attempts": 2},
                "fallback_route_ids": [backup["id"]],
            },
        )
    # The backup cannot be deleted while it is a fallback.
    assert stack.api("DELETE", f"/routes/{backup['id']}").status_code == 409
    gw = stack.start_gateway()
    _wait_served(gw, seeded.a.secret, "primary", "primary-down", "backup")

    for model in ("primary", "primary-down"):
        r = gw.chat(seeded.a.secret, model=model)
        assert r.status_code == 200, r.text
        assert r.json()["model"] == model, "client sees the model it asked for"
    s = gw.chat(seeded.a.secret, model="primary-down", stream=True)
    assert s.status_code == 200 and "data: [DONE]" in s.text, s.text
    events = [
        json.loads(line.removeprefix("data: "))
        for line in s.text.splitlines()
        if line.startswith("data: {")
    ]
    assert events and all(e["model"] == "primary-down" for e in events)

    fallbacks = _series(gw, "lumewarp_fallbacks_total")
    assert fallbacks.get('{model="primary",fallback_model="backup"}') == 1, fallbacks
    assert fallbacks.get('{model="primary-down",fallback_model="backup"}') == 2, (
        fallbacks
    )
    assert _attempts(gw, "primary-a", "retryable_status") == 1
    assert _attempts(gw, "primary-down-a", "connect_error") == 2
    rows = _settled_usage(stack, gw, 3)
    assert len(rows) == 3, "one usage event per client request"
    assert sorted((r["model"], r["deployment"], r["status"]) for r in rows) == [
        ("primary", "good-a", 200),
        ("primary-down", "good-a", 200),
        ("primary-down", "good-a", 200),
    ]
    assert primaries["primary"]["fallback_route_ids"] == [backup["id"]]


def test_uri_contract_snapshot_is_accepted_by_gateway(stack):
    stack.seed()
    # No inference or scrape is sent to these hosts: all URLs are compiled eagerly.
    urls = [
        "http://localhost/metrics",
        "https://example.com:65535/v1",
        "http://127.0.0.1:0/v1",
        "http://[::1]:8000/v1",
        "https://[2001:db8::1]/v1",
        "http://mock_upstream/v1",
        "http://example.com/a%20b;v=1",
    ]
    for i, url in enumerate(urls):
        stack.ok(
            "POST",
            "/upstreams",
            json={
                "name": f"uri-{i}",
                "kind": "openai_compatible",
                "base_url": url,
                "metrics_url": url + "?model=a%2Fb&v=1",
            },
        )
    for field in ("base_url", "metrics_url"):
        for i, url in enumerate(
            (
                "http://exam%2fple.com/v1",
                "http://example.com:abc/v1",
                "http://u/bad<path",
            )
        ):
            response = stack.api(
                "POST",
                "/upstreams",
                json={
                    "name": f"invalid-{field}-{i}",
                    "kind": "vllm",
                    "base_url": "http://127.0.0.1:9/v1",
                    field: url,
                },
            )
            assert response.status_code == 422, response.text
    upstream = _upstream_id(stack, "mock")
    with DatabaseEvents(stack.dsn) as events:
        stack.ok(
            "PATCH",
            f"/upstreams/{upstream}",
            json={
                "health_check": {
                    "path": "/a%20b;v=1",
                    "interval_ms": 1000,
                    "timeout_ms": 100,
                }
            },
        )
        revision = pg(
            stack.dsn, "SELECT source_revision FROM scopes WHERE name = 'e2e'"
        )[0][0]
        events.wait(
            lambda: (
                stack.ok("GET", "/config/versions", params={"limit": 1})["items"][0][
                    "source_revision"
                ]
                >= revision
            ),
            what="URI contract compiled revision",
        )
        version = stack.current_version()
        stack.start_gateway()
        events.wait(
            lambda: (stack.gateway_status() or {}).get("acked_version", 0) >= version,
            what="URI contract snapshot ACK",
        )
    assert stack.gateway_status()["last_nack_code"] is None


def test_circuit_breaker_opens_and_stops_traffic(stack):
    seeded = stack.seed()
    threshold = 3
    bad = _bad_upstream(
        stack, circuit_breaker={"failure_threshold": threshold, "open_ms": 60000}
    )
    good = _upstream_id(stack, "mock")
    ids = [
        _deployment(stack, "bad-a", bad, BAD_WEIGHT),
        _deployment(stack, "good-a", good),
    ]
    stack.ok("POST", "/routes", json={"model": "cb", "deployment_ids": ids})
    gw = stack.start_gateway()
    _wait_served(gw, seeded.a.secret, "cb")
    state = 'lumewarp_upstream_circuit_state{deployment="bad-a"}'

    statuses = []
    for _ in range(threshold * 10):
        statuses.append(gw.chat(seeded.a.secret, model="cb").status_code)
        if gw.metric(state) == 2:
            break
    assert gw.metric(state) == 2, f"breaker did not open: {statuses}"
    assert _failed_attempts(gw, "bad-a") == threshold
    assert statuses.count(503) == threshold, statuses

    before_bad, before_good = _attempts(gw, "bad-a"), _attempts(gw, "good-a")
    for _ in range(20):
        r = gw.chat(seeded.a.secret, model="cb")
        assert r.status_code == 200, r.text
    assert _attempts(gw, "bad-a") == before_bad, "an open breaker receives no traffic"
    assert _attempts(gw, "good-a") == before_good + 20
    assert _attempts(gw, "bad-a", "circuit_open") >= 20
    assert gw.metric(state) == 2

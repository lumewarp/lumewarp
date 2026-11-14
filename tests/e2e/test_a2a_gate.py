"""A2A v1.0 through a real control plane and gateway (plan acceptance 5).

Everything is created over the admin API — the A2A agent and its middleware, the teams,
the service accounts and the keys — then exercised through the gateway's `/a2a` endpoints
against the real `mcp-a2a-mock` agent:

* the gateway fetches the card with no A2A request and the control plane's
  `last_card_json` carries the rewritten card (gateway URL, gateway auth, no signatures);
* a key without an agent grant is refused, and `GET /a2a` lists only granted agents;
* `SendStreamingMessage` through the `strip-artifacts` middleware removes the configured
  media type; the middleware is loaded from `LUMEWARP_A2A_MIDDLEWARE_DIR` and reported in
  Hello (visible through `GET /plugins`);
* another granted key cannot read the first key's task, and a tampered MAC is refused;
  push configuration is an error;
* a usage row (`kind=a2a`, target, method, completed outcome for a stream) lands in
  Postgres, the A2A metric moves, and the run's pg_dump scan finds no plaintext credential;
* a middleware with a 50 ms timeout set through the API answers 504.

Runs under `scripts/e2e-a2a.sh` (control-plane mode), which reuses
`scripts/e2e-core.sh` for the database/secret-scan run context.
"""

import base64
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import stack as stack_module
from stack import ROOT, Proc, free_port, wait_for

# `scripts/e2e-a2a.sh` builds the A2A mock and the middleware fixtures and sets this
# flag. A plain `scripts/e2e-core.sh` run collects this module but must not stand up a
# database/control plane for it: the skip is evaluated before fixtures, not in the body.
pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_A2A_CP") != "1",
    reason="control-plane A2A gate: run scripts/e2e-a2a.sh",
)

MOCK_BIN = Path(
    os.environ.get(
        "E2E_A2A_MOCK_BIN", str(ROOT / "bench/mock-upstream/target/release/mcp-a2a-mock")
    )
)
STRIP_WASM = ROOT / "gateway/target/plugins/a2a-middleware/strip-artifacts.wasm"
MISBEHAVE_WASM = ROOT / "gateway/target/plugins-test/a2a-middleware/misbehave.wasm"
HELPER = "a2a-helper"
SLOW = "a2a-slow"
SECRET_MEDIA = "application/x-mock-secret"


# --------------------------------------------------------------------------- helpers


def message(text: str) -> dict:
    return {
        "messageId": "msg-e2e",
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }


def rpc(
    gw,
    secret: str,
    agent: str,
    method: str,
    params: dict,
    *,
    version: str | None = "1.0",
    accept_sse: bool = False,
    bearer: str | None = None,
) -> httpx.Response:
    headers = {"x-lumewarp-key": secret}
    if version is not None:
        headers["a2a-version"] = version
    if accept_sse:
        headers["accept"] = "text/event-stream"
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return gw.http.post(
        gw.url(f"/a2a/{agent}"),
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def agent_list(gw, secret: str) -> httpx.Response:
    return gw.http.get(gw.url("/a2a"), headers={"x-lumewarp-key": secret})


def sse_events(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def metric_sum(gw, prefix: str) -> float:
    total = 0.0
    for line in gw.metrics().splitlines():
        if line.startswith(prefix):
            total += float(line.rsplit(" ", 1)[1])
    return total


def plugin_connected(stack, name: str) -> bool:
    items = stack.ok("GET", "/plugins", params={"kind": "a2a-middleware"})["items"]
    for item in items:
        if item["name"] == name:
            return any(support["connected"] for support in item["gateways"])
    return False


def create_key(stack, team_id: str, tag: str) -> tuple[str, str]:
    """A service account and one of its keys: (key id, secret)."""
    account = stack.ok("POST", f"/teams/{team_id}/service-accounts", json={"name": tag})["id"]
    created = stack.ok(
        "POST",
        f"/teams/{team_id}/keys",
        json={"name": tag, "service_account_id": account},
    )
    return created["key"]["id"], created["secret"]


def start_gateway_with_public_url(stack, monkeypatch, middleware_dir: Path):
    """`Gateway` picks its own ports; the A2A card rewrite needs the port first."""
    port = free_port()
    admin = free_port()
    real_free_port = stack_module.free_port
    ports = iter([port, admin])
    monkeypatch.setattr(stack_module, "free_port", lambda: next(ports, real_free_port()))
    return stack.start_gateway(
        extra_env={
            "LUMEWARP_GATEWAY_ID_KEY": base64.b64encode(os.urandom(32)).decode(),
            "LUMEWARP_GATEWAY_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "LUMEWARP_A2A_MIDDLEWARE_DIR": str(middleware_dir),
        }
    )


# ----------------------------------------------------------------------------- test


def test_a2a_through_the_control_plane(stack, monkeypatch):
    if not MOCK_BIN.exists():
        pytest.skip(f"missing {MOCK_BIN}; build mcp-a2a-mock first")
    if not STRIP_WASM.exists() or not MISBEHAVE_WASM.exists():
        pytest.skip("build the A2A middleware components with build.sh --tests first")

    # The mock agent and a middleware directory with the example and the timeout fixture.
    mock_port = free_port()
    mock = Proc(
        "a2a-mock",
        [
            str(MOCK_BIN),
            "a2a",
            "--listen",
            f"127.0.0.1:{mock_port}",
            "--auth",
            "none",
            "--stream-step-ms",
            "20",
        ],
        stack.base_env,
        stack.dir,
    )
    stack.extra_mocks.append(mock)
    mock.start()
    wait_for(
        lambda: httpx.get(f"http://127.0.0.1:{mock_port}/health", timeout=1).status_code == 200,
        10,
        "a2a mock",
    )
    middleware = stack.dir / "a2a-middleware"
    middleware.mkdir()
    shutil.copy(STRIP_WASM, middleware / "strip-artifacts.wasm")
    shutil.copy(MISBEHAVE_WASM, middleware / "misbehave.wasm")

    gateway = start_gateway_with_public_url(stack, monkeypatch, middleware)
    # Hello is what makes the plugin nameable by the API; it is not an A2A request.
    wait_for(
        lambda: (
            plugin_connected(stack, "strip_artifacts") and plugin_connected(stack, "misbehave_test")
        ),
        30,
        "A2A middleware plugins reported in Hello",
    )

    agent_url = f"http://127.0.0.1:{mock_port}/a2a"
    helper = stack.ok(
        "POST",
        "/a2a-agents",
        json={
            "name": HELPER,
            "url": agent_url,
            "auth": {"plugin": "none"},
            "middleware": {
                "plugin": "strip_artifacts",
                "params": {"media_types": [SECRET_MEDIA]},
            },
        },
    )
    slow = stack.ok(
        "POST",
        "/a2a-agents",
        json={
            "name": SLOW,
            "url": agent_url,
            "auth": {"plugin": "none"},
            "middleware": {
                "plugin": "misbehave_test",
                "params": {"mode": "slow", "spin": 4_000_000_000},
                "timeout_ms": 50,
            },
        },
    )
    assert slow["middleware"]["timeout_ms"] == 50
    assert slow["middleware"]["plugin"] == "misbehave_test"
    assert helper["middleware"]["plugin"] == "strip_artifacts"
    assert helper["middleware"]["params"] == {"media_types": [SECRET_MEDIA]}

    granted = stack.ok(
        "POST",
        "/teams",
        json={
            "name": "a2a-granted",
            "org_id": stack.org_id,
            "a2a_access": "list",
            "a2a_agents": [HELPER, SLOW],
        },
    )
    denied = stack.ok(
        "POST",
        "/teams",
        json={
            "name": "a2a-denied",
            "org_id": stack.org_id,
            "a2a_access": "list",
            "a2a_agents": [],
        },
    )
    key_a_id, key_a = create_key(stack, granted["id"], "a2a-key-a")
    _, key_b = create_key(stack, granted["id"], "a2a-key-b")
    _, key_c = create_key(stack, denied["id"], "a2a-key-c")
    stack.wait_key_count(3)
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == stack.current_version(),
        20,
        "gateway ACKed the A2A config",
    )

    # --- the card is fetched and reported without an A2A request -----------------
    def fetched_card():
        out = stack.ok("GET", f"/a2a-agents/{helper['id']}")
        return out if out["last_card_json"] else None

    agent = wait_for(fetched_card, 20, "agent card reported through CatalogReport")
    assert agent["last_card_error"] == ""
    card = json.loads(agent["last_card_json"])
    assert card["supportedInterfaces"][0]["url"] == (
        f"http://127.0.0.1:{gateway.port}/a2a/{HELPER}"
    ), card
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert "signatures" not in card
    assert card["capabilities"]["pushNotifications"] is False
    assert card["securitySchemes"]["lumewarp"]["apiKeySecurityScheme"]["name"] == "X-Lumewarp-Key"

    # --- grants and the per-key listing ------------------------------------------
    listed = agent_list(gateway, key_a)
    assert listed.status_code == 200, listed.text
    assert {entry["name"] for entry in listed.json()} == {HELPER, SLOW}
    assert agent_list(gateway, key_b).json() == listed.json()
    denied_list = agent_list(gateway, key_c)
    assert denied_list.status_code == 200
    assert denied_list.json() == []
    refused = rpc(gateway, key_c, HELPER, "SendMessage", {"message": message("no")})
    assert refused.status_code == 404, refused.text

    # --- one task, two keys and a tampered MAC -----------------------------------
    sent = rpc(gateway, key_a, HELPER, "SendMessage", {"message": message("hello")})
    assert sent.status_code == 200, sent.text
    task = sent.json()["result"]["task"]
    task_id = task["id"]
    assert task_id.startswith(f"lwt.{key_a_id}."), task_id
    assert task["contextId"].startswith(f"lwt.{key_a_id}."), task["contextId"]
    assert SECRET_MEDIA not in sent.text, "the blocking response was not stripped"

    foreign = rpc(gateway, key_b, HELPER, "GetTask", {"id": task_id})
    assert foreign.status_code == 200
    assert foreign.json()["error"]["code"] == -32001, foreign.text
    mac_at = task_id.rfind(".") + 1
    tampered = task_id[:mac_at] + ("A" if task_id[mac_at] != "A" else "B") + task_id[mac_at + 1 :]
    bad_mac = rpc(gateway, key_a, HELPER, "GetTask", {"id": tampered})
    assert bad_mac.json()["error"]["code"] == -32001, bad_mac.text

    pushed = rpc(
        gateway,
        key_a,
        HELPER,
        "SendMessage",
        {
            "message": message("push"),
            "configuration": {
                "taskPushNotificationConfig": {"url": "https://callback.example.test"}
            },
        },
    )
    assert pushed.json()["error"]["code"] == -32003, pushed.text

    # --- streaming through strip-artifacts ---------------------------------------
    before = metric_sum(
        gateway,
        'lumewarp_a2a_requests_total{agent="a2a-helper",method="message_stream",status="200"}',
    )
    streamed = rpc(
        gateway,
        key_a,
        HELPER,
        "SendStreamingMessage",
        {"message": message("stream")},
        accept_sse=True,
    )
    assert streamed.status_code == 200, streamed.text
    assert "text/event-stream" in streamed.headers.get("content-type", "")
    events = sse_events(streamed.text)
    assert events and SECRET_MEDIA not in streamed.text, streamed.text[:500]
    text_parts = [
        part
        for event in events
        for part in event.get("result", {})
        .get("artifactUpdate", {})
        .get("artifact", {})
        .get("parts", [])
    ]
    assert any(part.get("text") for part in text_parts), events
    assert events[-1]["result"]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    after = metric_sum(
        gateway,
        'lumewarp_a2a_requests_total{agent="a2a-helper",method="message_stream",status="200"}',
    )
    assert after == before + 1, (before, after)

    # --- middleware timeout selected through the API -----------------------------
    timeout_agent = stack.ok("GET", f"/a2a-agents/{slow['id']}")
    assert timeout_agent["middleware"]["timeout_ms"] == 50
    timed_out = rpc(gateway, key_a, SLOW, "SendMessage", {"message": message("slow")})
    assert timed_out.status_code == 504, timed_out.text
    assert timed_out.json()["error"]["code"] == -32603
    assert (
        metric_sum(
            gateway,
            'lumewarp_plugin_calls_total{kind="a2a-middleware",plugin="misbehave_test",result="timeout"}',
        )
        >= 1
    )

    # --- usage in Postgres, then a graceful shutdown -----------------------------
    def stream_rows():
        rows = [
            row
            for row in stack.usage_rows()
            if row["kind"] == "a2a" and row["method"] == "SendStreamingMessage"
        ]
        return rows or None

    wait_for(stream_rows, 20, "A2A streaming usage row")
    gateway.drain_usage()
    rows = stack.usage_rows()
    stream_row = next(
        row
        for row in rows
        if row["kind"] == "a2a"
        and row["target"] == HELPER
        and row["method"] == "SendStreamingMessage"
    )
    assert stream_row["status"] == 200
    assert stream_row["stream"] is True
    assert stream_row["outcome"] == "completed"
    assert stream_row["tool"] == ""
    blocking = next(
        row
        for row in rows
        if row["kind"] == "a2a"
        and row["target"] == HELPER
        and row["method"] == "SendMessage"
        and row["status"] == 200
    )
    assert blocking["stream"] is False
    assert blocking["outcome"] == "completed"

    # The stack fixture's teardown scans this database for the credentials issued above
    # and drops it; `scripts/e2e-a2a.sh` re-checks the scan report.

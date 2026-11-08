#!/usr/bin/env bash
# A2A v1.0 end-to-end gate (plan acceptance 5).
#
# Two modes:
#   standalone — a YAML config, two keys and the built `strip-artifacts`
#                middleware against the real `mcp-a2a-mock` agent.
#   cp         — Postgres + control plane + gateway over the gRPC config
#                stream: the agent, middleware, teams, keys and grants are
#                created through the admin API; the card, ownership, streaming,
#                usage row, metrics and middleware timeout are asserted.
#                Runs the pytest gate `tests/e2e/test_a2a_gate.py` through
#                `scripts/e2e-core.sh` (database reaping + plaintext-secret scan).
#
#   scripts/e2e-a2a.sh                 # both modes
#   E2E_A2A_MODE=standalone ...        # or cp
#
# Environment:
#   E2E_A2A_MODE        both (default) | standalone | cp
#   E2E_A2A_MOCK_BIN   path to mcp-a2a-mock (default bench/mock-upstream release)
#   E2E_A2A_GATEWAY_BIN path to the gateway binary (default gateway debug)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
run="$(mktemp -d "${TMPDIR:-/tmp}/lumewarp-a2a-e2e.XXXXXX")"
pids=()
cleanup() {
  status=$?
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  if [[ "${KEEP_RUN:-0}" == 1 ]]; then
    echo "kept run directory: $run" >&2
  else
    rm -rf "$run"
  fi
  exit "$status"
}
trap cleanup EXIT
trap "exit 130" INT
trap "exit 143" TERM

free_port() {
  python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

key_hash() {
  printf %s "$1" | sha256sum | cut -d' ' -f1
}

echo "== build middleware components, gateway and mock agent"
bash "$root/gateway/plugins/build.sh" --tests
(cd "$root/gateway" && cargo build -p lumewarp-gateway)
gateway_bin="${E2E_A2A_GATEWAY_BIN:-$root/gateway/target/debug/lumewarp}"
(cd "$root/bench/mock-upstream" && cargo build --release --bin mcp-a2a-mock)
mock_bin="${E2E_A2A_MOCK_BIN:-$root/bench/mock-upstream/target/release/mcp-a2a-mock}"
for file in "$gateway_bin" "$mock_bin" \
    "$root/gateway/target/plugins/a2a-middleware/strip-artifacts.wasm" \
    "$root/gateway/target/plugins-test/a2a-middleware/misbehave.wasm"; do
  [[ -e "$file" ]] || { echo "error: missing $file" >&2; exit 1; }
done

mode="${E2E_A2A_MODE:-both}"
case "$mode" in
  standalone|cp|both) ;;
  *) echo "error: E2E_A2A_MODE must be standalone, cp or both" >&2; exit 2 ;;
esac
stop_bg() {
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]:-}"; do wait "$pid" 2>/dev/null || true; done
  pids=()
}

if [[ "$mode" != cp ]]; then
helper_port="$(free_port)"
pt_port="$(free_port)"
gw_port="$(free_port)"
admin_port="$(free_port)"
gw2_port="$(free_port)"
admin2_port="$(free_port)"
public_url="http://127.0.0.1:$gw_port"
id_key="$(python3 -c 'import base64; print(base64.b64encode(b"lumewarp-a2a-e2e-id-key-32byte!!").decode())')"

"$mock_bin" a2a --listen "127.0.0.1:$helper_port" --auth none --stream-step-ms 20 \
  >"$run/helper.log" 2>&1 &
pids+=($!)
"$mock_bin" a2a --listen "127.0.0.1:$pt_port" --auth any-bearer --stream-step-ms 20 \
  >"$run/pt.log" 2>&1 &
pids+=($!)

cat >"$run/config.yaml" <<YAML
server:
  listen: '127.0.0.1:$gw_port'
  admin_listen: '127.0.0.1:$admin_port'
a2a_agents:
  - name: helper
    url: 'http://127.0.0.1:$helper_port/a2a'
    auth: {plugin: none}
    middleware: {plugin: strip_artifacts, params: {media_types: ['application/x-mock-secret']}}
  - name: pt
    url: 'http://127.0.0.1:$pt_port/a2a'
    auth: {plugin: passthrough}
keys:
  - key_id: key-a
    key_sha256: '$(key_hash e2e-key-a)'
    allow_all_models: true
    allowed_agents: [helper, pt]
  - key_id: key-b
    key_sha256: '$(key_hash e2e-key-b)'
    allow_all_models: true
    allowed_agents: [helper]
YAML

env LUMEWARP_A2A_MIDDLEWARE_DIR="$root/gateway/target/plugins/a2a-middleware" \
    LUMEWARP_GATEWAY_ID_KEY="$id_key" \
    LUMEWARP_GATEWAY_PUBLIC_URL="$public_url" \
    "$gateway_bin" --config "$run/config.yaml" >"$run/gateway.log" 2>&1 &
pids+=($!)

# Same snapshot, no LUMEWARP_GATEWAY_ID_KEY: A2A must answer 503 and nothing else.
cat >"$run/config-503.yaml" <<YAML
server:
  listen: '127.0.0.1:$gw2_port'
  admin_listen: '127.0.0.1:$admin2_port'
a2a_agents:
  - name: helper
    url: 'http://127.0.0.1:$helper_port/a2a'
    auth: {plugin: none}
keys:
  - key_id: key-a
    key_sha256: '$(key_hash e2e-key-a)'
    allow_all_models: true
    allowed_agents: [helper]
YAML
env -u LUMEWARP_GATEWAY_ID_KEY LUMEWARP_GATEWAY_PUBLIC_URL="http://127.0.0.1:$gw2_port" \
    "$gateway_bin" --config "$run/config-503.yaml" >"$run/gateway-503.log" 2>&1 &
pids+=($!)

ready() {
  curl -fsS -o /dev/null "$1" 2>/dev/null
}
for _ in $(seq 1 300); do
  if ready "http://127.0.0.1:$admin_port/readyz" &&
     ready "http://127.0.0.1:$admin2_port/readyz" &&
     ready "http://127.0.0.1:$helper_port/health" &&
     ready "http://127.0.0.1:$pt_port/health"; then
    break
  fi
  sleep 0.1
done
for pair in "gateway:$admin_port/readyz" "gateway-503:$admin2_port/readyz" \
    "helper:$helper_port/health" "pt:$pt_port/health"; do
  name="${pair%%:*}"
  url="http://127.0.0.1:${pair#*:}"
  ready "$url" || {
    echo "error: $name did not become ready ($url)" >&2
    tail -50 "$run/$name.log" >&2 || true
    exit 1
  }
done

echo "== assertions"
GW="$gw_port" GW2="$gw2_port" ADMIN="$admin_port" \
  python3 - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

GW = int(os.environ["GW"])
GW2 = int(os.environ["GW2"])
ADMIN = int(os.environ["ADMIN"])
KEY_A = "e2e-key-a"
KEY_B = "e2e-key-b"
SECRET_MEDIA = "application/x-mock-secret"
PUBLIC = f"http://127.0.0.1:{GW}"


def http(url, data=None, headers=None, method=None):
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def rpc(agent, key, method, params, version="1.0", bearer=None, port=GW):
    headers = {"content-type": "application/json", "x-lumewarp-key": key}
    if version is not None:
        headers["a2a-version"] = version
    if bearer is not None:
        headers["authorization"] = "Bearer " + bearer
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    status, _, raw = http(f"http://127.0.0.1:{port}/a2a/{agent}", body, headers, "POST")
    return status, json.loads(raw)


def message(text):
    return {
        "messageId": "msg-e2e",
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }


def check(condition, label, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"ok - {label}")


# 1. The Agent Card points at the gateway and describes the gateway's auth.
deadline = time.time() + 20
card = None
last = None
while time.time() < deadline:
    status, _, raw = http(
        f"http://127.0.0.1:{GW}/a2a/helper/.well-known/agent-card.json",
        headers={"x-lumewarp-key": KEY_A},
    )
    if status == 200:
        card = json.loads(raw)
        break
    last = (status, raw[:300])
    time.sleep(0.1)
check(card is not None, "the gateway serves the fetched agent card", last)
check(len(card["supportedInterfaces"]) == 1, "the card has one interface")
check(
    card["supportedInterfaces"][0]["url"] == f"{PUBLIC}/a2a/helper",
    "the card URL points at the gateway",
    card["supportedInterfaces"][0]["url"],
)
check("signatures" not in card, "upstream signatures are removed")
check(card["capabilities"]["pushNotifications"] is False, "push notifications are off")
check(
    card["securitySchemes"]["lumewarp"]["apiKeySecurityScheme"]["name"] == "X-Lumewarp-Key",
    "the card advertises the gateway API key",
)

status, _, raw = http(f"http://127.0.0.1:{GW}/a2a", headers={"x-lumewarp-key": KEY_A})
agents = {entry["name"] for entry in json.loads(raw)}
check(status == 200 and {"helper", "pt"} <= agents, "GET /a2a lists the granted agents")

# 2. Task ownership: key B and a tampered MAC are both task-not-found.
status, response = rpc("helper", KEY_A, "SendMessage", {"message": message("hello")})
check(status == 200, "SendMessage succeeds", response)
task = response["result"]["task"]
task_id = task["id"]
context_id = task["contextId"]
check(task_id.startswith(f"lwt.key-a."), "the task id is wrapped for key A", task_id)
check(context_id.startswith(f"lwt.key-a."), "the context id is wrapped for key A")
status, response = rpc("helper", KEY_A, "GetTask", {"id": task_id})
check(status == 200 and response["result"]["id"] == task_id, "key A can read its task")
status, response = rpc("helper", KEY_B, "GetTask", {"id": task_id})
check(status == 200 and response["error"]["code"] == -32001, "key B cannot read key A's task", response)
tampered = task_id[:-1] + ("B" if task_id[-1] == "A" else "A")
status, response = rpc("helper", KEY_A, "GetTask", {"id": tampered})
check(status == 200 and response["error"]["code"] == -32001, "a tampered MAC is refused", response)

# 3. Push notification configuration is refused.
status, response = rpc(
    "helper",
    KEY_A,
    "SendMessage",
    {
        "message": message("push"),
        "configuration": {"taskPushNotificationConfig": {"url": "https://callback.example.test"}},
    },
)
check(status == 200 and response["error"]["code"] == -32003, "SendMessage with push config is refused")
status, response = rpc(
    "helper", KEY_A, "CreateTaskPushNotificationConfig", {"url": "https://callback.example.test"}
)
check(response["error"]["code"] == -32003, "push config methods are refused")

# 4. A2A-Version, ListTasks and the streaming middleware.
status, response = rpc("helper", KEY_A, "SendMessage", {"message": message("x")}, version=None)
check(response["error"]["code"] == -32009, "an absent A2A-Version is refused")
status, response = rpc("helper", KEY_A, "SendMessage", {"message": message("x")}, version="0.3")
check(response["error"]["code"] == -32009, "A2A-Version 0.3 is refused")
status, response = rpc("helper", KEY_A, "ListTasks", {})
check(response["error"]["code"] == -32004, "ListTasks is unsupported without passthrough")

status, response = rpc(
    "pt", KEY_A, "SendMessage", {"message": message("pt hello")}, bearer="user-token"
)
check(status == 200, "passthrough SendMessage succeeds", response)
status, response = rpc("pt", KEY_A, "ListTasks", {}, bearer="user-token")
listed = response["result"]["tasks"]
check(len(listed) == 1 and listed[0]["id"].startswith("lwt.key-a."), "ListTasks wraps ids", response)

status, _, raw = http(
    f"http://127.0.0.1:{GW}/a2a/helper",
    json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "SendStreamingMessage", "params": {"message": message("stream")}}
    ).encode(),
    {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "a2a-version": "1.0",
        "x-lumewarp-key": KEY_A,
    },
    "POST",
)
events = []
for block in raw.decode().split("\n\n"):
    data = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
    if data:
        events.append(json.loads("\n".join(data)))
check(status == 200 and events, "SendStreamingMessage streams SSE", raw[:200])
check(json.dumps(events).find(SECRET_MEDIA) == -1, "strip-artifacts removes the secret media type")
check(
    any(
        part.get("text") is not None
        for event in events
        for artifact in [event.get("result", {}).get("artifactUpdate", {}).get("artifact", {})]
        for part in artifact.get("parts", [])
    ),
    "the text artifact survives",
)
check(
    events[-1]["result"]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED",
    "the stream ends with the terminal status",
    events[-1],
)

# 5. Usage-adjacent metrics and the 503 without the id key.
status, _, raw = http(f"http://127.0.0.1:{ADMIN}/metrics")
metrics = raw.decode()
check(
    'lumewarp_a2a_requests_total{agent="helper",method="message_stream",status="200"}' in metrics,
    "the A2A request metric is exported",
)
check(
    'lumewarp_plugin_calls_total{kind="a2a-middleware",plugin="strip_artifacts",result="ok"}' in metrics,
    "the middleware call metric is exported",
)
status, _, raw = http(
    f"http://127.0.0.1:{GW2}/a2a/helper",
    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "SendMessage", "params": {"message": message("x")}}).encode(),
    {"content-type": "application/json", "a2a-version": "1.0", "x-lumewarp-key": KEY_A},
    "POST",
)
check(
    status == 503 and json.loads(raw)["error"]["code"] == "a2a_not_configured",
    "A2A answers 503 without LUMEWARP_GATEWAY_ID_KEY",
    raw[:200],
)
print("all A2A end-to-end assertions passed")
PY
stop_bg
fi

if [[ "$mode" != standalone ]]; then
  echo "== control-plane A2A gate (Postgres + control plane + gateway)"
  export E2E_A2A_MOCK_BIN="$mock_bin"
  # The gate is skipped when a plain `scripts/e2e-core.sh` run collects it.
  export E2E_A2A_CP=1
  ignores=()
  for file in "$root"/tests/e2e/test_*.py; do
    [[ "$(basename "$file")" == "test_a2a_gate.py" ]] && continue
    ignores+=("--ignore=$file")
  done
  # e2e-core owns the run directory, the database reaping and the plaintext-secret
  # scan; it builds the gateway and the mock-upstream library, while this script
  # already built the A2A agent and the middleware fixtures.
  "$root/scripts/e2e-core.sh" "${ignores[@]}"
fi

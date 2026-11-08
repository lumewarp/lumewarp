"""A router strategy is a WASM component: discovered through Hello, configured through the API.

scripts/e2e-core.sh builds gateway/plugins/router/*.wasm with gateway/plugins/build.sh. Only
the tests using one set LUMEWARP_ROUTER_PLUGIN_DIR on the gateway they start.
"""

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import stack as stack_module
from stack import free_port, wait_for
from test_routing_resilience import _model, _upstream_id

STRATEGY = "priority_failover"
ROOT = Path(__file__).resolve().parents[2]
# Also run through the component by the plugin crate's own tests.
BOUNDARIES = json.loads(
    (ROOT / "gateway/plugins/router/priority-failover/testdata/boundaries.json").read_text()
)


def test_nothing_outside_the_plugin_names_the_strategy():
    found = subprocess.run(
        ["git", "grep", "-l", STRATEGY, "--", "proto", "control-plane/src", "ui/src"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert found.returncode == 1, found.stdout or found.stderr


def _strategy(stack) -> dict | None:
    items = stack.ok("GET", "/plugins?kind=router-strategy")["items"]
    return next((item for item in items if item["name"] == STRATEGY), None)


def test_plugin_appears_through_hello_and_a_router_using_it_is_served(
    stack, router_plugin_dir
):
    seeded = stack.seed()
    upstream = _upstream_id(stack, "mock")
    first, second = _model(stack, "first", upstream), _model(stack, "second", upstream)
    body = {
        "name": "failover",
        "strategy": STRATEGY,
        "params": {"max_in_flight": 100},
        "members": [
            {"model_id": first, "params": {"priority": 2}},
            {"model_id": second, "params": {"priority": 1}},
        ],
    }
    assert _strategy(stack) is None
    assert "no gateway reports" in stack.api("POST", "/routers", json=body).text

    gw = stack.start_gateway(
        extra_env={"LUMEWARP_ROUTER_PLUGIN_DIR": str(router_plugin_dir)}
    )
    wait_for(
        lambda: f"strategy:{STRATEGY}" in stack.gateway_status()["capabilities"],
        15,
        "strategy capability in Hello",
    )
    item = wait_for(lambda: _strategy(stack), 15, "strategy reported")
    assert item["title"] == "Priority failover"
    assert item["uses_weight"] is False
    assert item["builtin"] is False
    assert item["params_schema"]["properties"]["max_in_flight"]["minimum"] == 1
    assert item["member_schema"]["properties"]["priority"]["title"] == "Priority"
    assert item["member_schema"]["required"] == ["priority"]
    assert [g["connected"] for g in item["gateways"]] == [True]
    assert item["gateways"][0]["digest"]

    # The schema the component derives is what the API enforces.
    bad = {**body, "members": [{"model_id": first, "params": {"priority": "high"}}]}
    refused = stack.api("POST", "/routers", json=bad)
    assert refused.status_code == 422, refused.text
    assert "members[0].params.priority" in refused.text

    router = stack.ok("POST", "/routers", json=body)
    assert router["config_invalid"] is False
    wait_for(
        lambda: gw.chat(seeded.a.secret, model="failover").status_code == 200,
        15,
        "router using the strategy served",
    )
    response = gw.chat(seeded.a.secret, model="failover")
    assert response.status_code == 200
    request_id = response.headers["x-lumewarp-request-id"]

    def row():
        return next((r for r in stack.usage_rows() if str(r["request_id"]) == request_id), None)

    wait_for(row, 10, "usage row of the call")
    found = row()
    # The member's params reached the strategy: priority 1 wins whatever the order.
    assert (found["requested"], found["model"]) == ("failover", "second")


def _standalone_accepts(
    binary: Path, directory: Path, params: dict, member: dict, env: dict[str, str]
) -> bool:
    """Whether the gateway starts from YAML with these settings. JSON is YAML."""
    listen, admin = free_port(), free_port()
    config = directory / f"standalone-{listen}.yaml"
    config.write_text(
        json.dumps(
            {
                "server": {"listen": f"127.0.0.1:{listen}", "admin_listen": f"127.0.0.1:{admin}"},
                "upstreams": [{"name": "u", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"}],
                "models": [{"name": "m", "upstream": "u", "upstream_model": "m"}],
                "routers": [
                    {
                        "name": "r",
                        "strategy": STRATEGY,
                        "params": params,
                        "members": [{"model": "m", "params": member}],
                    }
                ],
            }
        )
    )
    proc = subprocess.Popen(
        [binary, "--config", str(config)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", listen), 0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        raise AssertionError(f"gateway neither started nor exited: {params} {member}")
    finally:
        proc.terminate()
        proc.wait(15)


def test_the_same_boundary_payloads_agree_in_control_plane_and_standalone(
    stack, router_plugin_dir, tmp_path
):
    binary = Path(stack_module.GATEWAY_BIN)
    stack.seed()
    model = _model(stack, "boundary", _upstream_id(stack, "mock"))
    # One component in the standalone directory: wasmtime compiles it on every start, and
    # this test starts a process per boundary case.
    standalone_plugins = tmp_path / "router-plugins"
    standalone_plugins.mkdir()
    shutil.copy(
        router_plugin_dir / f"{STRATEGY.replace('_', '-')}.wasm", standalone_plugins
    )
    plugin_env = {
        **stack.base_env,
        "LUMEWARP_ROUTER_PLUGIN_DIR": str(standalone_plugins),
    }
    stack.start_gateway(extra_env={"LUMEWARP_ROUTER_PLUGIN_DIR": str(router_plugin_dir)})
    wait_for(lambda: _strategy(stack), 15, "strategy reported")
    for i, case in enumerate(BOUNDARIES):
        body = {
            "name": f"boundary-{i}",
            "strategy": STRATEGY,
            "params": case["params"],
            "members": [{"model_id": model, "params": case["member"]}],
        }
        response = stack.api("POST", "/routers", json=body)
        assert response.status_code == (201 if case["ok"] else 422), (case, response.text)
        assert (
            _standalone_accepts(binary, tmp_path, case["params"], case["member"], plugin_env)
            is case["ok"]
        ), case

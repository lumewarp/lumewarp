"""A router component is discovered through Hello and configured through the API.

scripts/e2e-core.sh builds gateway/plugins/router/priority-failover separately from
its native gateway. Only the tests using it set LUMEWARP_ROUTER_PLUGIN_DIR.
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import stack as stack_module
from stack import free_port, wait_for
from test_routing_resilience import _model, _upstream_id

STRATEGY = "priority_failover"
ROOT = Path(__file__).resolve().parents[2]
# Also run through the factory by the crate's own tests.
BOUNDARIES = json.loads(
    (ROOT / "gateway/plugins/router/priority-failover/testdata/boundaries.json").read_text()
)


def test_nothing_outside_the_crate_names_the_strategy():
    found = subprocess.run(
        ["git", "grep", "-l", STRATEGY, "--", "proto", "control-plane/src", "ui/src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert found.returncode == 1, found.stdout or found.stderr


def _strategy(stack) -> dict | None:
    items = stack.ok("GET", "/plugins?kind=router-strategy")["items"]
    return next((item for item in items if item["name"] == STRATEGY), None)


def test_crate_appears_through_hello_and_a_router_using_it_is_served(stack, monkeypatch):
    binary = os.environ.get("E2E_STRATEGY_GATEWAY_BIN")
    if not binary:
        pytest.skip("only scripts/e2e-core.sh builds the gateway with the strategy crate")
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

    monkeypatch.setattr(stack_module, "GATEWAY_BIN", Path(binary))
    monkeypatch.setenv("LUMEWARP_ROUTER_PLUGIN_DIR", str(ROOT / "gateway/target/plugins/router"))
    gw = stack.start_gateway()

    item = _strategy(stack)
    assert item["title"] == "Priority failover"
    assert item["uses_weight"] is False
    assert item["builtin"] is False
    assert item["member_schema"]["properties"]["priority"]["title"] == "Priority"
    assert item["member_schema"]["required"] == ["priority"]
    assert item["params_schema"]["properties"]["max_in_flight"]["minimum"] == 1
    assert [g["connected"] for g in item["gateways"]] == [True]
    assert item["gateways"][0]["digest"]

    # The schema the crate derives is what the API enforces.
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


def _standalone_accepts(binary: str, directory: Path, params: dict, member: dict) -> bool:
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
        [binary, "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 15
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
    stack, monkeypatch, tmp_path
):
    binary = os.environ.get("E2E_STRATEGY_GATEWAY_BIN")
    if not binary:
        pytest.skip("only scripts/e2e-core.sh builds the gateway with the strategy crate")
    stack.seed()
    model = _model(stack, "boundary", _upstream_id(stack, "mock"))
    monkeypatch.setattr(stack_module, "GATEWAY_BIN", Path(binary))
    monkeypatch.setenv("LUMEWARP_ROUTER_PLUGIN_DIR", str(ROOT / "gateway/target/plugins/router"))
    stack.start_gateway()
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
        assert _standalone_accepts(binary, tmp_path, case["params"], case["member"]) is case["ok"], (
            case
        )

"""A new strategy needs only a Rust crate and a gateway rebuild.

The gateway here is built with `gateway/strategies/priority-failover` (scripts/e2e-p1.sh).
The strategy reaches the control plane only through Hello: nothing in the proto, the control
plane or the UI names it, its form comes from `GET /strategies`, and a router using it is
created and called through the admin API.
"""

import os
import subprocess
from pathlib import Path

import pytest
import stack as stack_module
from stack import wait_for
from test_p2_routing import _model, _upstream_id

STRATEGY = "priority_failover"
ROOT = Path(__file__).resolve().parents[2]


def test_nothing_outside_the_crate_names_the_strategy():
    found = subprocess.run(
        ["git", "grep", "-l", STRATEGY, "--", "proto", "control-plane/src", "ui/src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert found.returncode == 1, found.stdout or found.stderr


def _strategy(stack) -> dict | None:
    items = stack.ok("GET", "/strategies")["items"]
    return next((item for item in items if item["name"] == STRATEGY), None)


def test_crate_appears_through_hello_and_a_router_using_it_is_served(stack, monkeypatch):
    binary = os.environ.get("E2E_STRATEGY_GATEWAY_BIN")
    if not binary:
        pytest.skip("only scripts/e2e-p1.sh builds the gateway with the strategy crate")
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

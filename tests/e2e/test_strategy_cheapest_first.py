"""The worked example of `.claude/skills/port-router-to-rust`, end to end.

`gateway/strategies/cheapest-first` reaches the control plane only through
`Hello`: nothing in the proto, the control plane or the UI names it. A router
using it is saved, compiled into the snapshot and served, and the prices in its
member params decide which model answers.

The gateway is built with `--features strategy-cheapest-first` into its own
`gateway/target/e2e-cheapest-first` directory, so the default binary the other
e2e tests start is never overwritten. Set `E2E_CHEAPEST_FIRST_GATEWAY_BIN` to
use a binary built elsewhere.
"""

import os
import subprocess
from pathlib import Path

import pytest
import stack as stack_module
from stack import wait_for
from test_p2_routing import _model, _upstream_id

STRATEGY = "cheapest_first"
FEATURE = "strategy-cheapest-first"
TITLE = "Cheapest first"
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


@pytest.fixture(scope="module")
def strategy_gateway() -> Path:
    """A gateway binary with the crate compiled in.

    It builds into its own target directory: the default
    gateway/target/debug/lumewarp must keep the built-ins only, because
    test_p2_routing.py asserts its exact strategy capabilities.
    """
    if binary := os.environ.get("E2E_CHEAPEST_FIRST_GATEWAY_BIN"):
        return Path(binary)
    target_dir = ROOT / "gateway/target/e2e-cheapest-first"
    result = subprocess.run(
        ["cargo", "build", "-p", "lumewarp-gateway", "--features", FEATURE],
        cwd=ROOT / "gateway",
        env={**os.environ, "CARGO_TARGET_DIR": str(target_dir)},
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    binary = target_dir / "debug/lumewarp"
    assert binary.exists(), f"cargo build did not produce {binary}"
    return binary


def test_the_snapshot_carries_the_crate_and_the_cheapest_member_serves(
    stack, monkeypatch, strategy_gateway
):
    seeded = stack.seed()
    upstream = _upstream_id(stack, "mock")
    expensive = _model(stack, "expensive", upstream)
    unpriced = _model(stack, "unpriced", upstream)
    cheap = _model(stack, "cheap", upstream)
    body = {
        "name": "cost-aware",
        "strategy": STRATEGY,
        "params": {},
        "members": [
            {"model_id": expensive, "params": {"input_price_nano_usd_per_mtok": 300}},
            # No price in the member params, whatever the catalog says: the
            # strategy can only see its members' settings.
            {"model_id": unpriced, "params": {}},
            {"model_id": cheap, "params": {"input_price_nano_usd_per_mtok": 100}},
        ],
    }

    # Until a gateway says Hello, the control plane has never heard of it.
    assert _strategy(stack) is None
    assert "no gateway reports" in stack.api("POST", "/routers", json=body).text

    monkeypatch.setattr(stack_module, "GATEWAY_BIN", strategy_gateway)
    gw = stack.start_gateway()
    assert f"strategy:{STRATEGY}" in stack.gateway_status()["capabilities"]

    item = _strategy(stack)
    assert item["title"] == TITLE
    assert item["description"].startswith("Use the eligible member")
    assert item["uses_weight"] is False
    assert item["builtin"] is False
    assert item["params_schema"]["properties"]["max_input_price_nano_usd_per_mtok"][
        "minimum"
    ] == 1
    assert (
        item["member_schema"]["properties"]["input_price_nano_usd_per_mtok"]["title"]
        == "Input price (nano-USD per Mtok)"
    )
    assert "required" not in item["member_schema"]
    assert [g["connected"] for g in item["gateways"]] == [True]
    assert item["gateways"][0]["digest"]

    # The schema the crate derives is what the API enforces, for member params
    # and for params (annotations alone create no runtime check).
    bad_member = {
        **body,
        "members": [
            {"model_id": expensive, "params": {"input_price_nano_usd_per_mtok": "300"}},
            {"model_id": unpriced, "params": {}},
            {"model_id": cheap, "params": {"input_price_nano_usd_per_mtok": 100}},
        ],
    }
    refused = stack.api("POST", "/routers", json=bad_member)
    assert refused.status_code == 422, refused.text
    assert "members[0].params.input_price_nano_usd_per_mtok" in refused.text

    bad_params = {**body, "params": {"max_input_price_nano_usd_per_mtok": 0}}
    refused = stack.api("POST", "/routers", json=bad_params)
    assert refused.status_code == 422, refused.text
    assert "params.max_input_price_nano_usd_per_mtok" in refused.text

    router = stack.ok("POST", "/routers", json=body)
    assert router["config_invalid"] is False
    wait_for(
        lambda: gw.chat(seeded.a.secret, model="cost-aware").status_code == 200,
        15,
        "router using the strategy served",
    )
    response = gw.chat(seeded.a.secret, model="cost-aware")
    assert response.status_code == 200
    assert response.json()["model"] == "cost-aware"
    request_id = response.headers["x-lumewarp-request-id"]

    def row():
        return next(
            (r for r in stack.usage_rows() if str(r["request_id"]) == request_id), None
        )

    wait_for(row, 10, "usage row of the call")
    found = row()
    # The unpriced member comes last and the expensive one is not the cheapest:
    # the third member's member params win, not the declaration order.
    assert (found["requested"], found["model"]) == ("cost-aware", "cheap")

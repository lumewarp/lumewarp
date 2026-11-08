"""The worked example of `.claude/skills/port-router-to-rust`, end to end.

`gateway/plugins/router/cheapest-first` reaches the control plane only through
`Hello`: nothing in the proto, the control plane or the UI names it. A router
using it is saved, compiled into the snapshot and served, and the prices in its
member params decide which model answers.

scripts/e2e-core.sh builds the component with gateway/plugins/build.sh; the test
starts a gateway with LUMEWARP_ROUTER_PLUGIN_DIR pointing at that directory.
"""

import subprocess
from pathlib import Path

from stack import wait_for
from test_routing_resilience import _model, _upstream_id

STRATEGY = "cheapest_first"
TITLE = "Cheapest first"
ROOT = Path(__file__).resolve().parents[2]


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


def test_the_snapshot_carries_the_plugin_and_the_cheapest_member_serves(
    stack, router_plugin_dir
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

    plugin_env = {"LUMEWARP_ROUTER_PLUGIN_DIR": str(router_plugin_dir)}
    gw = stack.start_gateway(extra_env=plugin_env)
    wait_for(
        lambda: f"strategy:{STRATEGY}" in stack.gateway_status()["capabilities"],
        15,
        "strategy capability in Hello",
    )

    item = wait_for(lambda: _strategy(stack), 15, "strategy reported")
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

    # The schema the component derives is what the API enforces, for member params
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

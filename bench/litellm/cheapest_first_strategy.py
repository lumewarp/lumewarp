#!/usr/bin/env python3
"""Cheapest-first routing for LiteLLM, the same policy as Lumewarp's plugin.

`bench/scripts/run.sh --policy cheapest-first` (or `BENCH_POLICY=cheapest-first`)
loads `gateway/plugins/router/cheapest-first` into Lumewarp and installs this
strategy into LiteLLM, so both gateways run an identical router over identical
deployments with identical prices and the only difference left is the gateway.

The policy is the plugin's: among the *healthy* deployments, take the one with
the lowest input price; unpriced deployments come last; ties go to the smallest
deployment id (the bench names them `m0..m4` in declaration order, and uses
distinct prices, so the tie-break never decides a request).

LiteLLM 1.106.2 has no `routing_strategy` value for a custom class (its config
only accepts the built-in names), so the proxy builds its own `Router` and never
calls `set_custom_routing_strategy`. Two supported extension points fix that
without patching LiteLLM itself:

  * `LITELLM_WORKER_STARTUP_HOOKS=cheapest_first_strategy:install` runs
    `install()` at the start of the proxy lifespan in every worker, and
  * `install()` wraps `Router.routing_strategy_init`, the single place LiteLLM
    builds a strategy, so every router (the proxy's own included) gets this
    strategy right after its built-in setup.

No database, no Redis: `async_get_healthy_deployments` is the router's own
in-memory healthy set, the same one its built-in strategies use.

Selftest (no proxy, no network):

    uvx --from 'litellm[proxy]==1.106.2' python -I \\
        bench/litellm/cheapest_first_strategy.py --selftest
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from litellm.router import CustomRoutingStrategyBase, Router

# Same price ladder as bench/lumewarp/config-cheapest-first.yaml, in nano-USD
# per million input tokens there and USD per input token here.
PRICES: dict[str, float] = {
    "m0": 1e-15,
    "m1": 2e-15,
    "m2": 3e-15,
    "m3": 4e-15,
    "m4": 5e-15,
}

#: Written once per worker when the strategy runs, so run.sh can prove it is in
#: effect instead of silently measuring LiteLLM's default shuffle.
MARKER_ENV = "LW_BENCH_POLICY_MARKER"

_marked = False


def _mark_used(deployment: object | None = None) -> None:
    """Once per process: record that the strategy ran and what it chose.

    run.sh checks the file exists (the hook installed and the strategy served a
    request) and that it names `m0`, the cheapest deployment of the bench.
    """
    global _marked
    if _marked:
        return
    _marked = True
    marker = os.environ.get(MARKER_ENV)
    if not marker:
        return
    chosen = _deployment_id(deployment) if deployment is not None else "none"
    try:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} deployment={chosen}\n")
    except OSError:
        pass


def _model_info(deployment: object) -> dict:
    if isinstance(deployment, dict):
        info = deployment.get("model_info")
        return info if isinstance(info, dict) else {}
    info = getattr(deployment, "model_info", None)
    if isinstance(info, dict):
        return info
    return info.model_dump() if hasattr(info, "model_dump") else {}


def _price(deployment: object) -> float | None:
    """The deployment's input price, from `model_info` or `litellm_params`."""
    params = (
        deployment.get("litellm_params")
        if isinstance(deployment, dict)
        else getattr(deployment, "litellm_params", None)
    )
    for source in (_model_info(deployment), params):
        if isinstance(source, dict):
            value = source.get("input_cost_per_token")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _deployment_id(deployment: object) -> str:
    value = _model_info(deployment).get("id")
    return value if isinstance(value, str) else ""


def _cheapest(deployments: list) -> object | None:
    """The plugin's order: priced before unpriced, then price, then id."""
    if not deployments:
        return None
    return min(
        deployments,
        key=lambda deployment: (
            _price(deployment) is None,
            _price(deployment) or 0.0,
            _deployment_id(deployment),
        ),
    )


class CheapestFirst(CustomRoutingStrategyBase):
    """`CustomRoutingStrategyBase.async_get_available_deployment`."""

    def __init__(self, router: Router) -> None:
        self._router = router

    async def async_get_available_deployment(
        self,
        model: str,
        messages: list[dict[str, str]] | None = None,
        input: str | list | None = None,
        specific_deployment: bool | None = False,
        request_kwargs: dict | None = None,
    ):
        healthy = await self._router.async_get_healthy_deployments(
            model=model,
            request_kwargs=request_kwargs or {},
            messages=messages,
            input=input,
            specific_deployment=specific_deployment,
        )
        # The router answers a dict for "cannot route" (unknown model, ...).
        if isinstance(healthy, dict):
            _mark_used()
            return healthy
        chosen = _cheapest(list(healthy or []))
        _mark_used(chosen)
        return chosen

    def get_available_deployment(self, *args, **kwargs):
        _mark_used()
        return _cheapest(list(self._router.model_list))


_installed = False


def install() -> None:
    """Give every `Router` LiteLLM builds this strategy (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True
    original = Router.routing_strategy_init

    def routing_strategy_init(self, routing_strategy, routing_strategy_args):
        original(self, routing_strategy, routing_strategy_args)
        self.set_custom_routing_strategy(CheapestFirst(self))

    Router.routing_strategy_init = routing_strategy_init


# --------------------------------------------------------------------------- #
# Selftest: no proxy, no network. Checks the policy and the installation hook.
# --------------------------------------------------------------------------- #


def _deployment(model_id: str, price: float | None) -> dict:
    info: dict = {"id": model_id}
    if price is not None:
        info["input_cost_per_token"] = price
    return {
        "model_name": "mock",
        "litellm_params": {"model": "openai/mock", "api_base": "http://127.0.0.1:1/v1"},
        "model_info": info,
    }


def _deployment_pair() -> list[dict]:
    return [_deployment("m1", PRICES["m1"]), _deployment("m2", PRICES["m2"])]


def _selftest() -> int:
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    deployments = [
        _deployment("m0", PRICES["m0"]),
        _deployment("m1", PRICES["m1"]),
        _deployment("m2", PRICES["m2"]),
    ]
    check("lowest price wins", _deployment_id(_cheapest(deployments)), "m0")
    check(
        "order does not matter",
        _deployment_id(_cheapest(list(reversed(deployments)))),
        "m0",
    )
    check(
        "unpriced comes last",
        _deployment_id(_cheapest([_deployment("m9", None), *_deployment_pair()])),
        "m1",
    )
    check(
        "only unpriced -> first by id",
        _deployment_id(_cheapest([_deployment("m9", None), _deployment("m3", None)])),
        "m3",
    )
    check("empty -> none", _cheapest([]), None)
    # A deployment without model_info is unpriced, not an error.
    check(
        "no model_info",
        _deployment_id(_cheapest([{"model_name": "mock"}, _deployment("m4", 1.0)])),
        "m4",
    )
    # A price only in litellm_params still counts.
    priced_in_params = _deployment("m5", None)
    priced_in_params["litellm_params"]["input_cost_per_token"] = 1e-18
    check(
        "price from litellm_params",
        _deployment_id(_cheapest([_deployment("m0", PRICES["m0"]), priced_in_params])),
        "m5",
    )

    # install() must give a real Router this strategy, and the strategy must run
    # the policy on the router's own (healthy) deployment list.
    install()
    install()  # idempotent
    router = Router(
        model_list=[
            _deployment("m0", PRICES["m0"]),
            _deployment("m2", PRICES["m2"]),
        ],
        routing_strategy="simple-shuffle",
    )
    picked = asyncio.run(
        router.async_get_available_deployment(model="mock", request_kwargs={})
    )
    check("router picks the cheapest", _deployment_id(picked), "m0")

    # The marker is how run.sh proves the strategy served the run.
    marker = os.environ.get(MARKER_ENV)
    if marker:
        check("marker written", Path(marker).exists(), True)

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("cheapest-first strategy selftest: ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    print(__doc__)
    sys.exit(2)

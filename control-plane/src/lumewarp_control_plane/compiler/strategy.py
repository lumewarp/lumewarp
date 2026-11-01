"""Validation and serialization of compiled-in route strategy settings."""

import json
import re
from typing import Any

from lumewarp_control_plane.models import RoutingStrategy

MAX_PARAMS_BYTES = 16 * 1024
STRATEGY_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def params_json(params: dict[str, Any]) -> str:
    return json.dumps(
        params, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def strategy_problem(
    strategy: RoutingStrategy, name: str | None, params: dict[str, Any]
) -> str | None:
    if strategy != RoutingStrategy.CUSTOM:
        return "custom strategy fields require strategy custom" if name or params else None
    if name is None or not STRATEGY_NAME.fullmatch(name):
        return "custom_strategy must match ^[a-z][a-z0-9_]{0,62}$"
    # serde_json's default recursion budget accepts at most 127 nested containers.
    pending = [(params, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > 127:
            return "strategy_params exceeds the gateway JSON nesting limit"
        children = value.values() if isinstance(value, dict) else value
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    try:
        encoded = params_json(params)
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return "strategy_params must be a JSON object with finite values"
    if size > MAX_PARAMS_BYTES:
        return "strategy_params exceeds 16 KiB"
    if "headers" in params:
        headers = params["headers"]
        if not isinstance(headers, list) or any(
            not isinstance(h, str) or not HEADER_NAME.fullmatch(h) for h in headers
        ):
            return "strategy_params.headers must be an array of HTTP header names"
    return None

"""Validation and serialization of router strategy settings.

Full schema validation arrives with the strategy descriptors (c8). Until then: built-in
names take no params, any other well-shaped name is a compiled-in custom strategy.
"""

import json
import re
from typing import Any

BUILTIN_STRATEGIES = frozenset({"weighted", "least_in_flight", "lowest_latency", "vllm_load"})
MAX_PARAMS_BYTES = 16 * 1024
STRATEGY_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def params_json(params: dict[str, Any]) -> str:
    return json.dumps(
        params, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def params_problem(params: dict[str, Any], field: str = "params") -> str | None:
    """Limits every params object must meet, and the platform's `headers` rule."""
    # serde_json's default recursion budget accepts at most 127 nested containers.
    pending = [(params, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > 127:
            return f"{field} exceeds the gateway JSON nesting limit"
        children = value.values() if isinstance(value, dict) else value
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    try:
        encoded = params_json(params)
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return f"{field} must be a JSON object with finite values"
    if size > MAX_PARAMS_BYTES:
        return f"{field} exceeds 16 KiB"
    if "headers" in params:
        headers = params["headers"]
        if not isinstance(headers, list) or any(
            not isinstance(h, str)
            or not HEADER_NAME.fullmatch(h)
            or h.lower()
            in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
            for h in headers
        ):
            return f"{field}.headers must be an array of HTTP header names"
    return None


def strategy_problem(strategy: str, params: dict[str, Any]) -> str | None:
    if not STRATEGY_NAME.fullmatch(strategy):
        return "strategy must match ^[a-z][a-z0-9_]{0,62}$"
    if strategy in BUILTIN_STRATEGIES and params:
        return f"built-in strategy {strategy!r} takes no params"
    return params_problem(params)

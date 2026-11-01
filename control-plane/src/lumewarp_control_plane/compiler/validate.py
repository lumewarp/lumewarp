"""Compile-time mirror of every check the gateway applies to a snapshot.

A published snapshot must never be NACKed INVALID_CONFIG by a healthy gateway: that would
pin the whole fleet on the previous version and block revocations and budget blocks. These
checks follow `gateway/crates/lumewarp-gateway/src/snapshot.rs::validate` and
`router.rs::Router::compile`; keep them in sync with those files.

Not checkable here: `Upstream.api_key_ref` must name an env var that exists on every
gateway (deployment configuration, not snapshot content).
"""

import re
from ipaddress import IPv6Address
from urllib.parse import urlsplit

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.strategy import STRATEGY_NAME, strategy_problem

# Capabilities the P2 gateway supports (snapshot.rs SUPPORTED_CAPABILITIES).
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "retry",
        "fallbacks",
        "health_check",
        "circuit_breaker",
        "traffic_split",
        "lowest_latency",
        "vllm_load",
    }
)
MAX_SPLITS = 16
MAX_SPLIT_NAME = 32
MAX_RETRY_ATTEMPTS = 5
_SPLIT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}")
# hyper HeaderName::from_bytes: an RFC 9110 token.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
UPSTREAM_KINDS = {
    pb.UPSTREAM_KIND_OPENAI,
    pb.UPSTREAM_KIND_VLLM,
    pb.UPSTREAM_KIND_OPENAI_COMPATIBLE,
}
# UNSPECIFIED is documented as INCLUDE_USAGE and accepted.
STREAM_USAGE_MODES = {
    pb.STREAM_USAGE_MODE_UNSPECIFIED,
    pb.STREAM_USAGE_MODE_INCLUDE_USAGE,
    pb.STREAM_USAGE_MODE_PASSTHROUGH,
}
SERVABLE_STRATEGIES = {
    pb.ROUTING_STRATEGY_WEIGHTED,
    pb.ROUTING_STRATEGY_LEAST_IN_FLIGHT,
    pb.ROUTING_STRATEGY_LOWEST_LATENCY,
    pb.ROUTING_STRATEGY_VLLM_LOAD,
    pb.ROUTING_STRATEGY_CUSTOM,
}
STICKY_BY = {pb.STICKY_BY_NONE, pb.STICKY_BY_HEADER, pb.STICKY_BY_TEAM, pb.STICKY_BY_KEY}
KEY_STATUSES = {
    pb.KEY_STATUS_ACTIVE,
    pb.KEY_STATUS_REVOKED,
    pb.KEY_STATUS_BUDGET_EXCEEDED,
    pb.KEY_STATUS_SUSPENDED,
}
RESERVED_ALIASES = frozenset({"unknown"})
DIGEST_LEN = 32
# Conservative subsets of http 1.5's authority and PathAndQuery grammar.
_HOST = re.compile(r"[A-Za-z0-9_.-]+")
_URI_PATH = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_URI_QUERY = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*")
MAX_URI_LEN = 65534


class SnapshotInvalid(ValueError):
    """The snapshot would be rejected by the gateway."""


def _http_url_problem(url: str) -> str | None:
    """Allow only URLs that Rust http::Uri and the gateway can consume unchanged."""
    if not url or any(not ("\x21" <= c <= "\x7e") for c in url):
        return "must be non-empty printable ASCII without spaces"
    if len(url) > MAX_URI_LEN:
        return "is too long"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "is not a valid URL"
    if not url.startswith(("http://", "https://")):
        return "must be an absolute http(s) URL"
    if not parts.netloc:
        return "must have a host"
    if "@" in parts.netloc:
        return "must not contain userinfo"
    if "#" in url:
        return "must not contain a fragment"
    authority = parts.netloc
    if authority.startswith("["):
        host, _, port = authority[1:].partition("]")
        try:
            # Zone identifiers are deliberately excluded from this conservative subset.
            if "%" in host:
                return "has an invalid host"
            IPv6Address(host)
        except ValueError:
            return "has an invalid host"
        if port and not port.startswith(":"):
            return "has an invalid authority"
    else:
        host, colon, port_number = authority.partition(":")
        if not _HOST.fullmatch(host):
            return "has an invalid host"
        port = colon + port_number
    if port and not re.fullmatch(r":[0-9]+", port):
        return "has an invalid port"
    try:
        parts.port  # noqa: B018 - raises on an invalid port
    except ValueError:
        return "has an invalid port"
    if not _URI_PATH.fullmatch(parts.path) or not _URI_QUERY.fullmatch(parts.query):
        return "has an invalid path or query"
    return None


def upstream_url_problem(url: str) -> str | None:
    """Router::compile's base URI, including space for derived endpoint paths."""
    if problem := _http_url_problem(url):
        return problem
    if "?" in url:
        return "must not contain a query"
    # API health paths are at most 400 bytes; reserve that much for every base.
    if len(url.rstrip("/")) + 400 > MAX_URI_LEN:
        return "is too long for derived endpoints"
    return None


def metrics_url_problem(url: str) -> str | None:
    """Why the gateway would reject an explicit `Upstream.metrics_url` (router.rs
    metrics_uri: a plain http(s) URI with an authority and no userinfo), or None.
    """
    return _http_url_problem(url)


def health_path_problem(path: str) -> str | None:
    if path and (not path.startswith("/") or not _URI_PATH.fullmatch(path)):
        return "health path must be an absolute path without query or fragment"
    return None


def health_check_problem(h: pb.HealthCheck, base_url: str = "") -> str | None:
    if h.interval_ms < 1000 or h.timeout_ms == 0 or h.timeout_ms >= h.interval_ms:
        return "health interval must be >= 1000 ms and 0 < timeout < interval"
    if problem := health_path_problem(h.path):
        return problem
    if len(base_url.rstrip("/")) + len(h.path or "/models") > MAX_URI_LEN:
        return "health probe URL is too long"
    return None


def capabilities_used(snap: pb.Snapshot) -> list[str]:
    """Capabilities the snapshot's fields need, exactly as snapshot.rs::validate requires
    them: a capability is listed iff a field that needs it is set. Sorted.
    """
    used: set[str] = set()
    for u in snap.upstreams:
        if u.HasField("health_check"):
            used.add("health_check")
        if u.HasField("circuit_breaker"):
            used.add("circuit_breaker")
        if u.metrics_url:
            used.add("vllm_load")
    for r in snap.routes:
        if r.HasField("retry"):
            used.add("retry")
        if r.fallbacks:
            used.add("fallbacks")
        if r.traffic_split or r.sticky != pb.STICKY_BY_NONE or r.sticky_header:
            used.add("traffic_split")
        if r.strategy == pb.ROUTING_STRATEGY_LOWEST_LATENCY:
            used.add("lowest_latency")
        if r.strategy == pb.ROUTING_STRATEGY_VLLM_LOAD:
            used.add("vllm_load")
        if r.strategy == pb.ROUTING_STRATEGY_CUSTOM:
            used.add(f"strategy:{r.custom_strategy}")
    return sorted(used)


def _validate_split(r: pb.ModelRoute, deployments: set[str], weight: dict[str, int]) -> None:
    """snapshot.rs::validate_split, plus Router::compile's positive weight per pool."""
    if r.sticky == pb.STICKY_BY_HEADER:
        if not r.sticky_header or not _HEADER_NAME.fullmatch(r.sticky_header):
            raise SnapshotInvalid(f"route {r.model!r}: sticky=header needs a valid sticky_header")
    elif r.sticky_header:
        raise SnapshotInvalid(f"route {r.model!r}: sticky_header is only used with sticky=header")
    if not r.traffic_split:
        if r.sticky != pb.STICKY_BY_NONE:
            raise SnapshotInvalid(f"route {r.model!r}: sticky needs a traffic_split")
        return
    if len(r.traffic_split) > MAX_SPLITS:
        raise SnapshotInvalid(f"route {r.model!r}: more than {MAX_SPLITS} traffic splits")
    names: set[str] = set()
    for split in r.traffic_split:
        if not _SPLIT_NAME.fullmatch(split.name):
            raise SnapshotInvalid(
                f"route {r.model!r}: split name {split.name!r} must be 1..={MAX_SPLIT_NAME}"
                " of [A-Za-z0-9_.-]"
            )
        if split.name in names:
            raise SnapshotInvalid(f"route {r.model!r}: duplicate split {split.name!r}")
        names.add(split.name)
        if not split.deployments:
            raise SnapshotInvalid(f"route {r.model!r}: split {split.name!r} has no deployments")
        if len(set(split.deployments)) != len(split.deployments):
            raise SnapshotInvalid(f"route {r.model!r}: split {split.name!r} repeats a deployment")
        missing = [d for d in split.deployments if d not in deployments]
        if missing:
            raise SnapshotInvalid(
                f"route {r.model!r}: split {split.name!r} references unknown deployment"
                f" {missing[0]!r}"
            )
        if r.strategy == pb.ROUTING_STRATEGY_WEIGHTED and not sum(
            weight[d] for d in split.deployments
        ):
            raise SnapshotInvalid(f"route {r.model!r} split {split.name!r} has zero total weight")
    if not sum(s.weight for s in r.traffic_split):
        raise SnapshotInvalid(f"route {r.model!r}: traffic split weights sum to 0")


def _unique(names: list[str], kind: str) -> set[str]:
    seen: set[str] = set()
    for name in names:
        if not name:
            raise SnapshotInvalid(f"{kind} with empty name")
        if name in seen:
            raise SnapshotInvalid(f"duplicate {kind} {name!r}")
        seen.add(name)
    return seen


def validate_snapshot(snap: pb.Snapshot, *, max_bytes: int, max_keys: int) -> None:
    """Raise SnapshotInvalid with the first problem a gateway would reject."""
    unsupported = {
        cap
        for cap in set(snap.required_capabilities) - SUPPORTED_CAPABILITIES
        if not (cap.startswith("strategy:") and STRATEGY_NAME.fullmatch(cap[9:]))
    }
    if unsupported:
        raise SnapshotInvalid(f"unsupported capabilities {sorted(unsupported)}")
    missing_caps = set(capabilities_used(snap)) - set(snap.required_capabilities)
    if missing_caps:
        raise SnapshotInvalid(f"fields require capabilities {sorted(missing_caps)}")
    if len(snap.api_keys) > max_keys:
        raise SnapshotInvalid(f"{len(snap.api_keys)} keys exceed the limit of {max_keys}")
    size = snap.ByteSize()
    if size > max_bytes:
        raise SnapshotInvalid(f"snapshot is {size} bytes, over the limit of {max_bytes}")

    upstreams = _unique([u.name for u in snap.upstreams], "upstream")
    for u in snap.upstreams:
        if u.kind not in UPSTREAM_KINDS:
            raise SnapshotInvalid(f"upstream {u.name!r} has kind {u.kind}")
        if u.stream_usage_mode not in STREAM_USAGE_MODES:
            raise SnapshotInvalid(
                f"upstream {u.name!r} has stream_usage_mode {u.stream_usage_mode}"
            )
        if (problem := upstream_url_problem(u.base_url)) is not None:
            raise SnapshotInvalid(f"upstream {u.name!r} base_url {problem}")
        if u.HasField("health_check") and (
            problem := health_check_problem(u.health_check, u.base_url)
        ):
            raise SnapshotInvalid(f"upstream {u.name!r}: {problem}")
        if u.metrics_url and (problem := metrics_url_problem(u.metrics_url)) is not None:
            raise SnapshotInvalid(f"upstream {u.name!r} metrics_url {problem}")

    deployments = _unique([d.name for d in snap.deployments], "deployment")
    weight = {d.name: d.weight for d in snap.deployments}
    for d in snap.deployments:
        if d.upstream not in upstreams:
            raise SnapshotInvalid(f"deployment {d.name!r} references unknown upstream")
        if not d.upstream_model:
            raise SnapshotInvalid(f"deployment {d.name!r} has an empty upstream_model")

    routes = _unique([r.model for r in snap.routes], "route")
    for r in snap.routes:
        if r.model in RESERVED_ALIASES:
            raise SnapshotInvalid(f"route alias {r.model!r} is reserved")
        if r.strategy not in SERVABLE_STRATEGIES:
            raise SnapshotInvalid(f"route {r.model!r} has unsupported strategy {r.strategy}")
        if r.strategy == pb.ROUTING_STRATEGY_CUSTOM:
            import json

            if len(r.strategy_params_json.encode("utf-8")) > 16 * 1024:
                raise SnapshotInvalid("strategy params exceed 16 KiB")
            try:
                params = json.loads(r.strategy_params_json or "{}")
            except (ValueError, RecursionError) as exc:
                raise SnapshotInvalid("strategy params must be a JSON object") from exc
            if not isinstance(params, dict):
                raise SnapshotInvalid("strategy params must be a JSON object")
            if problem := strategy_problem("custom", r.custom_strategy, params):
                raise SnapshotInvalid(problem)
        elif r.custom_strategy or r.strategy_params_json:
            raise SnapshotInvalid("custom strategy fields require CUSTOM")
        if r.sticky not in STICKY_BY:
            raise SnapshotInvalid(f"route {r.model!r} has sticky {r.sticky}")
        if r.HasField("retry") and (
            r.retry.max_attempts > MAX_RETRY_ATTEMPTS
            or any(not 400 <= code <= 599 for code in r.retry.retry_on_status)
        ):
            raise SnapshotInvalid(
                f"route {r.model!r}: retry max_attempts must be <= {MAX_RETRY_ATTEMPTS}"
                " and statuses in 400..=599"
            )
        for fallback in r.fallbacks:
            if fallback == r.model or fallback not in routes:
                raise SnapshotInvalid(
                    f"route {r.model!r} has a self or unknown fallback {fallback!r}"
                )
        _validate_split(r, deployments, weight)
        # With traffic_split the route's own deployment list is ignored.
        if r.traffic_split:
            continue
        if not r.deployments:
            raise SnapshotInvalid(f"route {r.model!r} has no deployments")
        missing = [d for d in r.deployments if d not in deployments]
        if missing:
            raise SnapshotInvalid(f"route {r.model!r} references unknown deployment {missing[0]!r}")
        if r.strategy == pb.ROUTING_STRATEGY_WEIGHTED and not sum(weight[d] for d in r.deployments):
            raise SnapshotInvalid(f"route {r.model!r} has zero total weight")

    _unique([t.id for t in snap.teams], "team")
    teams = {t.id for t in snap.teams}
    _unique([k.key_id for k in snap.api_keys], "key id")
    hashes: set[bytes] = set()
    limits: dict[str, pb.RateLimit] = {}
    for k in snap.api_keys:
        if len(k.key_hash) != DIGEST_LEN:
            raise SnapshotInvalid(f"key {k.key_id!r} hash must be {DIGEST_LEN} bytes")
        if k.key_hash in hashes:
            raise SnapshotInvalid(f"key {k.key_id!r} has a duplicate hash")
        hashes.add(k.key_hash)
        if k.status not in KEY_STATUSES:
            raise SnapshotInvalid(f"key {k.key_id!r} has status {k.status}")
        if k.expires_at_unix_ms < 0:
            raise SnapshotInvalid(f"key {k.key_id!r} has a negative expiry")
        if k.team_id and k.team_id not in teams:
            raise SnapshotInvalid(f"key {k.key_id!r} references an unknown team")
        if not k.HasField("policy"):
            raise SnapshotInvalid(f"key {k.key_id!r} has no policy")
        if any(not m for m in k.policy.allowed_models):
            raise SnapshotInvalid(f"key {k.key_id!r} allows an empty model name")
        own: set[str] = set()
        for scope in k.policy.rate_limit_scopes:
            if not scope.id or not scope.HasField("limit"):
                raise SnapshotInvalid(f"key {k.key_id!r} has a rate limit scope without id/limit")
            if scope.id in own:
                raise SnapshotInvalid(f"key {k.key_id!r} repeats rate limit scope {scope.id!r}")
            own.add(scope.id)
            if limits.setdefault(scope.id, scope.limit) != scope.limit:
                raise SnapshotInvalid(f"rate limit scope {scope.id!r} has conflicting limits")

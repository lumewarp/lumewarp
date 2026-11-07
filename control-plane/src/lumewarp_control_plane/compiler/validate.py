"""Compile-time mirror of every check the gateway applies to a snapshot.

A published snapshot must never be NACKed INVALID_CONFIG by a healthy gateway: that would
pin the whole fleet on the previous version and block revocations and budget blocks. These
checks follow `gateway/crates/lumewarp-gateway/src/snapshot.rs::validate` and
`router.rs::RoutingTable::compile`; keep them in sync with those files.

Not checkable here: `Upstream.api_key_ref` must name an env var that exists on every
gateway, and `Upstream.api_key_ciphertext` must open with each gateway's
LUMEWARP_ENCRYPTION_KEY (deployment environment configuration, not snapshot content).
"""

import json
import re
from ipaddress import IPv6Address
from urllib.parse import urlsplit

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.strategy import (
    BUILTIN_STRATEGIES,
    MAX_PARAMS_BYTES,
    STRATEGY_NAME,
    params_problem,
)

# Capabilities every gateway supports (snapshot.rs SUPPORTED_CAPABILITIES). Strategies are
# `strategy:<name>`; a custom one is supported where its crate is compiled in.
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "retry",
        "fallbacks",
        "health_check",
        "circuit_breaker",
        "traffic_split",
        "vllm_load",
        "rotation_grace",
        "encrypted_api_key",
        "model_limits",
        "default_body",
        "reasoning_effort",
        *(f"strategy:{name}" for name in BUILTIN_STRATEGIES),
    }
)
MAX_SPLITS = 16
MAX_SPLIT_NAME = 32
MAX_RETRY_ATTEMPTS = 5
# org, team, owner (member/service account) and key: the four levels of the hierarchy.
MAX_RATE_LIMIT_SCOPES = 4
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
    """RoutingTable::compile's base URI, including space for derived endpoint paths."""
    if problem := _http_url_problem(url):
        return problem
    if "?" in url:
        return "must not contain a query"
    # API health paths are at most 400 bytes; reserve that much for every base.
    if len(url.rstrip("/")) + 400 > MAX_URI_LEN:
        return "is too long for derived endpoints"
    return None


def metrics_url_problem(url: str) -> str | None:
    """Why the gateway or control plane rejects an explicit `Upstream.metrics_url`.

    router.rs::metrics_uri accepts a plain http(s) URI with an authority and no userinfo.
    The control plane is stricter on purpose: a query string is the remaining way to smuggle
    a credential (`?token=...`) into a column that is returned by the API and copied into
    audit, so it is refused here as `base_url` already refuses it.
    """
    if problem := _http_url_problem(url):
        return problem
    if "?" in url:
        return "must not contain a query"
    return None


def health_url_problem(url: str) -> str | None:
    """An explicit `HealthCheck.url`: the `metrics_url` rules (router.rs::health_uri
    accepts plain http(s) without userinfo; the query is refused here for the same
    credential-in-a-public-column reason).
    """
    return metrics_url_problem(url)


def health_check_problem(h: pb.HealthCheck, base_url: str = "") -> str | None:
    if h.interval_ms < 1000 or h.timeout_ms == 0 or h.timeout_ms >= h.interval_ms:
        return "health interval must be >= 1000 ms and 0 < timeout < interval"
    if h.url and (problem := health_url_problem(h.url)):
        return f"health url {problem}"
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
        if u.api_key_ciphertext:
            used.add("encrypted_api_key")
    for d in snap.models:
        if d.max_output_tokens:
            used.add("model_limits")
        if d.default_body_json:
            used.add("default_body")
        if d.HasField("reasoning_effort"):
            used.add("reasoning_effort")
    for r in snap.routers:
        if r.HasField("retry"):
            used.add("retry")
        if r.fallbacks:
            used.add("fallbacks")
        if r.traffic_split or r.sticky != pb.STICKY_BY_NONE or r.sticky_header:
            used.add("traffic_split")
        used.add(f"strategy:{r.strategy}")
    if any(k.previous_key_hash for k in snap.api_keys):
        used.add("rotation_grace")
    return sorted(used)


def _params_object(text: str, field: str) -> dict:
    """A wire params field: empty or a JSON object within the size limit."""
    if len(text.encode("utf-8")) > MAX_PARAMS_BYTES:
        raise SnapshotInvalid(f"{field} exceed 16 KiB")
    try:
        params = json.loads(text or "{}")
    except (ValueError, RecursionError) as exc:
        raise SnapshotInvalid(f"{field} must be a JSON object") from exc
    if not isinstance(params, dict):
        raise SnapshotInvalid(f"{field} must be a JSON object")
    return params


def _validate_members(r: pb.Router, where: str, members, models: set[str]) -> None:
    """snapshot.rs::validate_members, plus RoutingTable::compile's positive weight per pool."""
    if not members:
        raise SnapshotInvalid(f"router {r.name!r}{where} has no models")
    seen: set[str] = set()
    for m in members:
        if m.model not in models:
            raise SnapshotInvalid(f"router {r.name!r}{where} references unknown model {m.model!r}")
        if m.model in seen:
            raise SnapshotInvalid(f"router {r.name!r}{where} repeats model {m.model!r}")
        seen.add(m.model)
        _params_object(m.params_json, f"router {r.name!r}{where} member params")
    if r.strategy == "weighted" and not sum(m.weight for m in members):
        raise SnapshotInvalid(f"router {r.name!r}{where} has zero total weight")


def _validate_split(r: pb.Router, models: set[str]) -> None:
    """snapshot.rs::validate_split."""
    if r.sticky == pb.STICKY_BY_HEADER:
        if not r.sticky_header or not _HEADER_NAME.fullmatch(r.sticky_header):
            raise SnapshotInvalid(f"router {r.name!r}: sticky=header needs a valid sticky_header")
    elif r.sticky_header:
        raise SnapshotInvalid(f"router {r.name!r}: sticky_header is only used with sticky=header")
    if not r.traffic_split:
        if r.sticky != pb.STICKY_BY_NONE:
            raise SnapshotInvalid(f"router {r.name!r}: sticky needs a traffic_split")
        return
    if len(r.traffic_split) > MAX_SPLITS:
        raise SnapshotInvalid(f"router {r.name!r}: more than {MAX_SPLITS} traffic splits")
    names: set[str] = set()
    for split in r.traffic_split:
        if not _SPLIT_NAME.fullmatch(split.name):
            raise SnapshotInvalid(
                f"router {r.name!r}: split name {split.name!r} must be 1..={MAX_SPLIT_NAME}"
                " of [A-Za-z0-9_.-]"
            )
        if split.name in names:
            raise SnapshotInvalid(f"router {r.name!r}: duplicate split {split.name!r}")
        names.add(split.name)
        _validate_members(r, f" split {split.name!r}", split.members, models)
    if not sum(s.weight for s in r.traffic_split):
        raise SnapshotInvalid(f"router {r.name!r}: traffic split weights sum to 0")


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
        if u.api_key_ref and u.api_key_ciphertext:
            raise SnapshotInvalid(f"upstream {u.name!r} sets both api_key_ref and a sealed key")

    models = _unique([d.name for d in snap.models], "model")
    for d in snap.models:
        if d.default_body_json and (
            problem := _wire_object(d.default_body_json, "default_body", DEFAULT_BODY_RESERVED_KEYS)
        ):
            raise SnapshotInvalid(f"model {d.name!r}: {problem}")
        if d.HasField("reasoning_effort"):
            r = d.reasoning_effort
            mode = {
                pb.REASONING_EFFORT_MODE_UNSPECIFIED: "passthrough",
                pb.REASONING_EFFORT_MODE_PASSTHROUGH: "passthrough",
                pb.REASONING_EFFORT_MODE_STRIP: "strip",
                pb.REASONING_EFFORT_MODE_MAPPED: "mapped",
            }.get(r.mode)
            if mode is None:
                raise SnapshotInvalid(f"model {d.name!r}: unknown reasoning mode")
            fragments = {}
            for level, text in r.fragments_json.items():
                if problem := _wire_object(text, f"reasoning fragment {level!r}"):
                    raise SnapshotInvalid(f"model {d.name!r}: {problem}")
                fragments[level] = json.loads(text)
            if problem := reasoning_problem(mode, fragments, r.default_level or None):
                raise SnapshotInvalid(f"model {d.name!r}: {problem}")
        if d.upstream not in upstreams:
            raise SnapshotInvalid(f"model {d.name!r} references unknown upstream")
        if not d.upstream_model:
            raise SnapshotInvalid(f"model {d.name!r} has an empty upstream_model")

    # Models, aliases and routers share one name space.
    names = _unique(
        [
            *(d.name for d in snap.models),
            *(a.name for a in snap.aliases),
            *(r.name for r in snap.routers),
        ],
        "name",
    )
    reserved = names & RESERVED_ALIASES
    if reserved:
        raise SnapshotInvalid(f"name {sorted(reserved)[0]!r} is reserved")
    routers = {r.name for r in snap.routers}
    for a in snap.aliases:
        if a.target not in models and a.target not in routers:
            raise SnapshotInvalid(f"alias {a.name!r} targets {a.target!r}, not a model or router")
    for r in snap.routers:
        if not STRATEGY_NAME.fullmatch(r.strategy):
            raise SnapshotInvalid(
                f"router {r.name!r}: strategy must match ^[a-z][a-z0-9_]{{0,62}}$"
            )
        params = _params_object(r.params_json, f"router {r.name!r} params")
        if r.strategy in BUILTIN_STRATEGIES and params:
            raise SnapshotInvalid(f"router {r.name!r}: built-in {r.strategy!r} takes no params")
        if problem := params_problem(params, f"router {r.name!r} params"):
            raise SnapshotInvalid(problem)
        if r.sticky not in STICKY_BY:
            raise SnapshotInvalid(f"router {r.name!r} has sticky {r.sticky}")
        if r.HasField("retry") and (
            r.retry.max_attempts > MAX_RETRY_ATTEMPTS
            or any(not 400 <= code <= 599 for code in r.retry.retry_on_status)
        ):
            raise SnapshotInvalid(
                f"router {r.name!r}: retry max_attempts must be <= {MAX_RETRY_ATTEMPTS}"
                " and statuses in 400..=599"
            )
        for fallback in r.fallbacks:
            if fallback not in models:
                raise SnapshotInvalid(f"router {r.name!r} fallback {fallback!r} is not a model")
        _validate_split(r, models)
        # With traffic_split the router's own member list is ignored.
        if not r.traffic_split:
            _validate_members(r, "", r.members, models)

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
        if k.previous_key_hash:
            if len(k.previous_key_hash) != DIGEST_LEN:
                raise SnapshotInvalid(f"key {k.key_id!r} previous hash must be {DIGEST_LEN} bytes")
            if k.previous_key_expires_at_unix_ms <= 0:
                raise SnapshotInvalid(f"key {k.key_id!r} previous hash needs a positive expiry")
            if k.previous_key_hash in hashes:
                raise SnapshotInvalid(f"key {k.key_id!r} has a duplicate previous hash")
            hashes.add(k.previous_key_hash)
        elif k.previous_key_expires_at_unix_ms:
            raise SnapshotInvalid(f"key {k.key_id!r} has a previous expiry without a hash")
        if k.team_id and k.team_id not in teams:
            raise SnapshotInvalid(f"key {k.key_id!r} references an unknown team")
        if not k.HasField("policy"):
            raise SnapshotInvalid(f"key {k.key_id!r} has no policy")
        if any(not m for m in k.policy.allowed_models):
            raise SnapshotInvalid(f"key {k.key_id!r} allows an empty model name")
        if len(k.policy.rate_limit_scopes) > MAX_RATE_LIMIT_SCOPES:
            raise SnapshotInvalid(
                f"key {k.key_id!r} has {len(k.policy.rate_limit_scopes)} rate limit scopes,"
                f" over the limit of {MAX_RATE_LIMIT_SCOPES}"
            )
        own: set[str] = set()
        for scope in k.policy.rate_limit_scopes:
            if not scope.id or not scope.HasField("limit"):
                raise SnapshotInvalid(f"key {k.key_id!r} has a rate limit scope without id/limit")
            if scope.id in own:
                raise SnapshotInvalid(f"key {k.key_id!r} repeats rate limit scope {scope.id!r}")
            own.add(scope.id)
            if limits.setdefault(scope.id, scope.limit) != scope.limit:
                raise SnapshotInvalid(f"rate limit scope {scope.id!r} has conflicting limits")


# Model body rules: gateway/crates/lumewarp-gateway/src/transform.rs.
RESERVED_BODY_KEYS = ("model", "stream", "stream_options")
# A default body cannot set reasoning_effort: the model's reasoning default_level does,
# in every mode (transform.rs).
DEFAULT_BODY_RESERVED_KEYS = (*RESERVED_BODY_KEYS, "reasoning_effort")
# Object/array nesting of a default body or fragment; the gateway checks the same bound.
MAX_BODY_DEPTH = 32
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
MAX_BODY_JSON_BYTES = 16 * 1024


def _depth(value: object) -> int:
    """Nesting of objects and arrays, without recursion (the outer object is 1)."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict | list):
            deepest = max(deepest, level)
            if deepest > MAX_BODY_DEPTH:
                break
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, level + 1) for child in children)
    return deepest


def body_object_problem(
    value: object, what: str, reserved: tuple[str, ...] = RESERVED_BODY_KEYS
) -> str | None:
    """A default body or reasoning fragment: a JSON object without gateway-owned keys."""
    if not isinstance(value, dict):
        return f"{what} must be a JSON object"
    if _depth(value) > MAX_BODY_DEPTH:
        return f"{what} nests deeper than {MAX_BODY_DEPTH} levels"
    try:
        text = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return f"{what} must be JSON"
    if len(text.encode()) > MAX_BODY_JSON_BYTES:
        return f"{what} exceeds 16 KiB"
    for key in reserved:
        if key in value:
            return f"{what} must not set {key!r}"
    return None


def reasoning_problem(mode: str, fragments: dict | None, default_level: str | None) -> str | None:
    if default_level is not None and default_level not in REASONING_LEVELS:
        return f"unknown reasoning level {default_level!r}"
    if mode == "mapped":
        if not fragments:
            return "mapped reasoning needs at least one level"
        for level, fragment in fragments.items():
            if level not in REASONING_LEVELS:
                return f"unknown reasoning level {level!r}"
            if problem := body_object_problem(fragment, f"reasoning fragment {level!r}"):
                return problem
        if default_level is not None and default_level not in fragments:
            return "default_level has no mapping"
    elif fragments:
        return f"{mode} reasoning takes no level mapping"
    if mode == "strip" and default_level is not None:
        return "strip reasoning takes no default_level"
    return None


def _wire_object(
    text: str, what: str, reserved: tuple[str, ...] = RESERVED_BODY_KEYS
) -> str | None:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return f"{what} must be a JSON object"
    return body_object_problem(value, what, reserved)

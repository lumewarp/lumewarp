"""Compile-time mirror of every check the gateway applies to a snapshot.

A published snapshot must never be NACKed INVALID_CONFIG by a healthy gateway: that would
pin the whole fleet on the previous version and block revocations and budget blocks. These
checks follow `gateway/crates/lumewarp-gateway/src/snapshot.rs::validate` and
`router.rs::Router::compile`; keep them in sync with those files.

Not checkable here: `Upstream.api_key_ref` must name an env var that exists on every
gateway (deployment configuration, not snapshot content).
"""

from urllib.parse import urlsplit

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb

# Capabilities the P1 gateway supports (snapshot.rs SUPPORTED_CAPABILITIES).
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset()
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
# LOWEST_LATENCY is known but answered UNSUPPORTED_FEATURE by the P1 gateway.
SERVABLE_STRATEGIES = {pb.ROUTING_STRATEGY_WEIGHTED, pb.ROUTING_STRATEGY_LEAST_IN_FLIGHT}
KEY_STATUSES = {
    pb.KEY_STATUS_ACTIVE,
    pb.KEY_STATUS_REVOKED,
    pb.KEY_STATUS_BUDGET_EXCEEDED,
    pb.KEY_STATUS_SUSPENDED,
}
RESERVED_ALIASES = frozenset({"unknown"})
DIGEST_LEN = 32


class SnapshotInvalid(ValueError):
    """The snapshot would be rejected by the gateway."""


def upstream_url_problem(url: str) -> str | None:
    """Why the gateway would reject `url` as an upstream base URL, or None.

    Router::compile requires an http(s) URI with an authority, no userinfo, no query;
    the `http` crate also rejects fragments, spaces and non-ASCII/control characters.
    """
    if not url or any(not ("\x21" <= c <= "\x7e") for c in url):
        return "must be non-empty printable ASCII without spaces"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "is not a valid URL"
    if parts.scheme not in ("http", "https"):
        return "must use http or https"
    if not parts.netloc:
        return "must have a host"
    if "@" in parts.netloc:
        return "must not contain userinfo"
    if parts.query or "?" in url:
        return "must not contain a query"
    if parts.fragment or "#" in url:
        return "must not contain a fragment"
    try:
        parts.port  # noqa: B018 - raises on an invalid port
    except ValueError:
        return "has an invalid port"
    return None


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
    unsupported = set(snap.required_capabilities) - SUPPORTED_CAPABILITIES
    if unsupported:
        raise SnapshotInvalid(f"unsupported capabilities {sorted(unsupported)}")
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

    deployments = _unique([d.name for d in snap.deployments], "deployment")
    weight = {d.name: d.weight for d in snap.deployments}
    for d in snap.deployments:
        if d.upstream not in upstreams:
            raise SnapshotInvalid(f"deployment {d.name!r} references unknown upstream")
        if not d.upstream_model:
            raise SnapshotInvalid(f"deployment {d.name!r} has an empty upstream_model")

    _unique([r.model for r in snap.routes], "route")
    for r in snap.routes:
        if r.model in RESERVED_ALIASES:
            raise SnapshotInvalid(f"route alias {r.model!r} is reserved")
        if r.strategy not in SERVABLE_STRATEGIES:
            raise SnapshotInvalid(f"route {r.model!r} has unsupported strategy {r.strategy}")
        if not r.deployments:
            raise SnapshotInvalid(f"route {r.model!r} has no deployments")
        missing = [d for d in r.deployments if d not in deployments]
        if missing:
            raise SnapshotInvalid(f"route {r.model!r} references unknown deployment {missing[0]!r}")
        if r.strategy == pb.ROUTING_STRATEGY_WEIGHTED and not sum(weight[d] for d in r.deployments):
            raise SnapshotInvalid(f"route {r.model!r} has zero total weight")

    _unique([t.id for t in snap.teams], "team")
    team_org = {t.id: t.org_id for t in snap.teams}
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
        if k.team_id:
            if k.team_id not in team_org:
                raise SnapshotInvalid(f"key {k.key_id!r} references an unknown team")
            if team_org[k.team_id] != k.org_id:
                raise SnapshotInvalid(f"key {k.key_id!r} org does not match its team")
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

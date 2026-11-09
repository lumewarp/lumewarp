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
from lumewarp_control_plane.compiler.plugins import (
    BUILTIN_PLUGINS,
    MAX_PARAMS_BYTES,
    PLUGIN_NAME,
    params_problem,
)
from lumewarp_control_plane.models import PluginKind

# Capabilities every gateway supports (snapshot.rs SUPPORTED_CAPABILITIES). Router strategies
# are `strategy:<name>`, MCP auth `auth:<name>` and A2A middleware `a2a_middleware:<name>`;
# a WASM one is supported where a gateway loaded it (see `_plugin_capability`). `mcp` and
# `a2a` cover the two proxied protocols.
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
        "model_chat_check",
        "model_types",
        "audio_price",
        "mcp",
        "a2a",
        *(f"strategy:{name}" for name in BUILTIN_PLUGINS[PluginKind.ROUTER_STRATEGY]),
    }
)
# A capability whose suffix is a plugin name: any well-formed name is accepted here, because
# whether a gateway loaded it is a deployment fact the control plane cannot check.
PLUGIN_CAPABILITY_PREFIXES = ("strategy:", "auth:", "a2a_middleware:")
# The gateway's bounds for MCP servers and A2A agents (targets.rs).
TARGET_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
BUILTIN_AUTH = frozenset({"none", "static_bearer", "passthrough"})
MAX_TARGET_DEFINITIONS = 10_000
MAX_DEFINITION_NAME = 256
MAX_CARD_BYTES = 64 * 1024
MAX_TARGET_TIMEOUT_MS = 0xFFFFFFFF


def _plugin_capability(cap: str) -> bool:
    """A `strategy:`/`auth:`/`a2a_middleware:` capability naming a well-formed plugin."""
    for prefix in PLUGIN_CAPABILITY_PREFIXES:
        if cap.startswith(prefix) and PLUGIN_NAME.fullmatch(cap[len(prefix) :]):
            return True
    return False


def target_url_problem(url: str) -> str | None:
    """An MCP server or A2A agent URL: `router::plain_http_uri`, absolute http(s) without
    userinfo. Unlike a routing base URL it may carry a path and a query (an MCP endpoint
    often does), but no fragment.
    """
    return _http_url_problem(url)


def target_name_problem(name: str) -> str | None:
    """`targets::valid_name` plus L4: a name may not contain the `__` tool separator."""
    if not TARGET_NAME.fullmatch(name):
        return "must match ^[a-z][a-z0-9_-]{0,62}$"
    if "__" in name:
        return 'must not contain "__" (the tool name separator)'
    return None


def card_url_problem(url: str) -> str | None:
    """`A2aAgent.card_url`: the URL rule, and no derived-path reservation."""
    return target_url_problem(url)


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


def chat_check_problem(c: pb.ChatCheck) -> str | None:
    if c.interval_ms < 1000 or c.timeout_ms == 0 or c.timeout_ms >= c.interval_ms:
        return "chat_check interval must be >= 1000 ms and 0 < timeout < interval"
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
        if d.type != pb.MODEL_TYPE_LLM:
            used.add("model_types")
        if d.price.nano_usd_per_audio_second:
            used.add("audio_price")
        if d.max_output_tokens:
            used.add("model_limits")
        if d.default_body_json:
            used.add("default_body")
        if d.HasField("reasoning_effort"):
            used.add("reasoning_effort")
        if d.HasField("chat_check"):
            used.add("model_chat_check")
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
    if snap.mcp_servers:
        used.add("mcp")
    for server in snap.mcp_servers:
        if server.HasField("auth") and server.auth.HasField("plugin"):
            used.add(f"auth:{server.auth.plugin.plugin}")
    if snap.a2a_agents:
        used.add("a2a")
    for agent in snap.a2a_agents:
        if agent.HasField("auth") and agent.auth.HasField("plugin"):
            used.add(f"auth:{agent.auth.plugin.plugin}")
        if agent.HasField("middleware") and agent.middleware.plugin:
            used.add(f"a2a_middleware:{agent.middleware.plugin}")
    return sorted(used)


def target_plugin_problem(kind: PluginKind, name: str, params: dict) -> str | None:
    """`check_auth`/`compile_plugin`'s shape rule for one plugin reference."""
    if not PLUGIN_NAME.fullmatch(name):
        return f"plugin name {name!r} must match ^[a-z][a-z0-9_]{{0,62}}$"
    if name in BUILTIN_PLUGINS[kind] and params:
        return f"built-in plugin {name!r} takes no params"
    return params_problem(params, kind=kind)


def _auth_problem(auth: pb.UpstreamAuth, what: str) -> str | None:
    """`targets.rs::check_auth`, spelled out: the built-ins' rules and the plugin shape.

    A WASM plugin's settings are validated against the schema a gateway reported by the
    write path; here only the always-checkable shape (name, size, `headers`) is repeated,
    because a rollback restores settings without asking a gateway.
    """
    if not auth.HasField("plugin"):
        return f"{what} auth has no plugin"
    plugin = auth.plugin
    params = _params_object(plugin.params_json, f"{what} auth params")
    if problem := target_plugin_problem(PluginKind.UPSTREAM_AUTH, plugin.plugin, params):
        return f"{what} auth {problem}"
    if auth.secret_ref and auth.secret_ciphertext:
        return f"{what} sets both secret_ref and secret_ciphertext"
    if plugin.plugin == "static_bearer":
        if not auth.secret_ref and not auth.secret_ciphertext:
            return f"{what} static_bearer needs secret_ref or secret_ciphertext"
    elif auth.secret_ref or auth.secret_ciphertext:
        return f"{what} auth {plugin.plugin!r} takes no secret"
    return None


def _definitions_problem(server: pb.McpServer) -> str | None:
    """`targets.rs::compile_approved`: kind, state, name and hash of every definition."""
    what = f"mcp server {server.name!r}"
    if len(server.definitions) > MAX_TARGET_DEFINITIONS:
        return f"{what} has more than {MAX_TARGET_DEFINITIONS} definitions"
    seen: set[tuple[int, str, bytes]] = set()
    for definition in server.definitions:
        if definition.kind not in (pb.MCP_DEFINITION_KIND_TOOL, pb.MCP_DEFINITION_KIND_PROMPT):
            return f"{what} definition has an unknown kind {definition.kind}"
        if definition.state not in (
            pb.MCP_DEFINITION_STATE_UNSPECIFIED,
            pb.MCP_DEFINITION_STATE_APPROVED,
            pb.MCP_DEFINITION_STATE_PENDING,
            pb.MCP_DEFINITION_STATE_REJECTED,
        ):
            return f"{what} definition has an unknown state {definition.state}"
        name = definition.name
        if not name or len(name.encode()) > MAX_DEFINITION_NAME:
            return f"{what} definition has an invalid name"
        if len(definition.hash) != DIGEST_LEN:
            return f"{what} definition {name!r} needs a 32-byte hash"
        key = (definition.kind, name, definition.hash)
        if key in seen:
            return f"{what} repeats the definition {name!r}"
        seen.add(key)
    return None


def validate_targets(snap: pb.Snapshot) -> None:
    """`targets.rs::validate`: MCP servers and A2A agents the gateway would accept."""
    _unique([s.name for s in snap.mcp_servers], "mcp server")
    _unique([a.name for a in snap.a2a_agents], "a2a agent")
    for server in snap.mcp_servers:
        what = f"mcp server {server.name!r}"
        if problem := target_name_problem(server.name):
            raise SnapshotInvalid(f"{what} name {problem}")
        if problem := target_url_problem(server.url):
            raise SnapshotInvalid(f"{what} url {problem}")
        if server.review_mode not in (
            pb.MCP_REVIEW_MODE_UNSPECIFIED,
            pb.MCP_REVIEW_MODE_TOFU,
            pb.MCP_REVIEW_MODE_MANUAL,
            pb.MCP_REVIEW_MODE_OFF,
        ):
            raise SnapshotInvalid(f"{what} has an unknown review_mode {server.review_mode}")
        if server.timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"{what} timeout_ms is too large")
        if server.HasField("auth") and server.auth.plugin.timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"{what} auth plugin_timeout_ms is too large")
        if not server.id:
            raise SnapshotInvalid(f"{what} has no id")
        if problem := _auth_problem(server.auth, what):
            raise SnapshotInvalid(problem)
        if problem := _definitions_problem(server):
            raise SnapshotInvalid(problem)
    for agent in snap.a2a_agents:
        what = f"a2a agent {agent.name!r}"
        if problem := target_name_problem(agent.name):
            raise SnapshotInvalid(f"{what} name {problem}")
        if problem := target_url_problem(agent.url):
            raise SnapshotInvalid(f"{what} url {problem}")
        if agent.card_url and (problem := card_url_problem(agent.card_url)):
            raise SnapshotInvalid(f"{what} card_url {problem}")
        if agent.timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"{what} timeout_ms is too large")
        if agent.HasField("auth") and agent.auth.plugin.timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"{what} auth plugin_timeout_ms is too large")
        if agent.HasField("middleware") and agent.middleware.timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"{what} middleware plugin_timeout_ms is too large")
        if not agent.id:
            raise SnapshotInvalid(f"{what} has no id")
        if problem := _auth_problem(agent.auth, what):
            raise SnapshotInvalid(problem)
        if agent.HasField("middleware") and agent.middleware.plugin:
            params = _params_object(agent.middleware.params_json, f"{what} middleware params")
            if problem := target_plugin_problem(
                PluginKind.A2A_MIDDLEWARE, agent.middleware.plugin, params
            ):
                raise SnapshotInvalid(f"{what} middleware {problem}")
        if agent.card_json:
            if len(agent.card_json.encode()) > MAX_CARD_BYTES:
                raise SnapshotInvalid(f"{what} card_json exceeds 64 KiB")
            try:
                card = json.loads(agent.card_json)
            except (ValueError, RecursionError):
                raise SnapshotInvalid(f"{what} card_json must be a JSON object") from None
            if not isinstance(card, dict):
                raise SnapshotInvalid(f"{what} card_json must be a JSON object")


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
        if not _plugin_capability(cap)
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
        if d.type not in (
            pb.MODEL_TYPE_LLM,
            pb.MODEL_TYPE_EMBEDDING,
            pb.MODEL_TYPE_TRANSCRIPTION,
            pb.MODEL_TYPE_DECISION,
        ):
            raise SnapshotInvalid(f"model {d.name!r}: model type is required")
        if d.type != pb.MODEL_TYPE_LLM and (d.max_output_tokens or d.HasField("reasoning_effort")):
            raise SnapshotInvalid(f"model {d.name!r}: reasoning and output limits require llm")
        if d.type == pb.MODEL_TYPE_TRANSCRIPTION and d.default_body_json:
            raise SnapshotInvalid(f"model {d.name!r}: transcription cannot set default_body")
        if d.type != pb.MODEL_TYPE_TRANSCRIPTION and d.price.nano_usd_per_audio_second:
            raise SnapshotInvalid(f"model {d.name!r}: audio price requires transcription")
        if d.type == pb.MODEL_TYPE_EMBEDDING and d.price.output_nano_usd_per_mtok:
            raise SnapshotInvalid(f"model {d.name!r}: embedding cannot set output price")
        if d.HasField("chat_check") and (problem := chat_check_problem(d.chat_check)):
            raise SnapshotInvalid(f"model {d.name!r}: {problem}")
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
        if not PLUGIN_NAME.fullmatch(r.strategy):
            raise SnapshotInvalid(
                f"router {r.name!r}: strategy must match ^[a-z][a-z0-9_]{{0,62}}$"
            )
        params = _params_object(r.params_json, f"router {r.name!r} params")
        if r.strategy in BUILTIN_PLUGINS[PluginKind.ROUTER_STRATEGY] and params:
            raise SnapshotInvalid(f"router {r.name!r}: built-in {r.strategy!r} takes no params")
        if problem := params_problem(
            params, f"router {r.name!r} params", PluginKind.ROUTER_STRATEGY
        ):
            raise SnapshotInvalid(problem)
        if r.sticky not in STICKY_BY:
            raise SnapshotInvalid(f"router {r.name!r} has sticky {r.sticky}")
        if r.plugin_timeout_ms > MAX_TARGET_TIMEOUT_MS:
            raise SnapshotInvalid(f"router {r.name!r} plugin_timeout_ms is too large")
        if r.HasField("retry") and (
            r.retry.max_attempts > MAX_RETRY_ATTEMPTS
            or any(not 400 <= code <= 599 for code in r.retry.retry_on_status)
        ):
            raise SnapshotInvalid(
                f"router {r.name!r}: retry max_attempts must be <= {MAX_RETRY_ATTEMPTS}"
                " and statuses in 400..=599"
            )
        types_by_name = {d.name: d.type for d in snap.models}
        referenced = (
            [m.model for m in r.members]
            + [m.model for s in r.traffic_split for m in s.members]
            + list(r.fallbacks)
        )
        if len({types_by_name[n] for n in referenced if n in types_by_name}) > 1:
            raise SnapshotInvalid(
                f"router {r.name!r}: members and fallbacks must share one model type"
            )
        for fallback in r.fallbacks:
            if fallback not in models:
                raise SnapshotInvalid(f"router {r.name!r} fallback {fallback!r} is not a model")
        _validate_split(r, models)
        # With traffic_split the router's own member list is ignored.
        if not r.traffic_split:
            _validate_members(r, "", r.members, models)

    validate_targets(snap)

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
        servers = {s.name for s in snap.mcp_servers}
        granted: set[str] = set()
        for grant in k.policy.mcp_grants:
            if not grant.server:
                raise SnapshotInvalid(f"key {k.key_id!r} grants an empty MCP server name")
            if grant.server not in servers:
                raise SnapshotInvalid(
                    f"key {k.key_id!r} grants unknown MCP server {grant.server!r}"
                )
            if grant.server in granted:
                raise SnapshotInvalid(f"key {k.key_id!r} repeats the MCP server {grant.server!r}")
            granted.add(grant.server)
            if grant.all_tools and grant.tools:
                raise SnapshotInvalid(
                    f"key {k.key_id!r}: server {grant.server!r} grants all tools and a list"
                )
            if any(not tool for tool in grant.tools):
                raise SnapshotInvalid(f"key {k.key_id!r} grants an empty tool name")
        agents = {a.name for a in snap.a2a_agents}
        if any(not name for name in k.policy.allowed_agents):
            raise SnapshotInvalid(f"key {k.key_id!r} allows an empty agent name")
        if len(set(k.policy.allowed_agents)) != len(k.policy.allowed_agents):
            raise SnapshotInvalid(f"key {k.key_id!r} repeats an A2A agent")
        if unknown := set(k.policy.allowed_agents) - agents:
            raise SnapshotInvalid(f"key {k.key_id!r} allows unknown A2A agents {sorted(unknown)}")
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
REASONING_LEVEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
MAX_REASONING_LEVELS = 16
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
    if default_level is not None and not REASONING_LEVEL_NAME.fullmatch(default_level):
        return f"invalid reasoning level {default_level!r}"
    if mode == "mapped":
        if not fragments:
            return "mapped reasoning needs at least one level"
        if len(fragments) > MAX_REASONING_LEVELS:
            return "mapped reasoning supports at most 16 levels"
        for level, fragment in fragments.items():
            if not isinstance(level, str) or not REASONING_LEVEL_NAME.fullmatch(level):
                return f"invalid reasoning level {level!r}"
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

"""Gateway plugins: what is known about them and validation of their settings.

A gateway reports every plugin it has — the built-ins and the WASM plugins it loaded from its
own directories at startup — in `Hello.plugins`, each with the JSON Schemas of its settings
(docs/plugins.md). The manifest of the built-ins is committed next to this file as
`builtin_plugins.json`, so they work with no gateway connected. Schemas reported by a
gateway are untrusted: they are capped, restricted to a subset that has no `$ref` and no
`pattern`, and otherwise ignored, in which case only the gateway's `configure` validates
those settings.

One rule covers every write that names a plugin, for all three kinds: `check_plugin` takes a
registry, `validate_plugin_params` loads one and applies the same rule. A router, an MCP server
and an A2A agent all go through it, and so does a rollback, which restores stored settings.
Keep the subset in step with the gateway's `lumewarp-plugin-sdk`; both pass the SDK's
`testdata/schema-subset.json`.
"""

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators
from referencing import Registry
from sqlalchemy import and_, delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane.models import (
    ActorType,
    GatewayPlugin,
    GatewaySession,
    GatewayToken,
    PluginKind,
    Router,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
)
from lumewarp_control_plane.security import Principal
from lumewarp_control_plane.settings import get_settings

PLUGIN_KINDS = (
    PluginKind.ROUTER_STRATEGY,
    PluginKind.UPSTREAM_AUTH,
    PluginKind.A2A_MIDDLEWARE,
)
# The capability a plugin advertises: `<prefix>:<name>`, and the request field a write names
# it in, which is what an error message points at.
CAPABILITY_PREFIX = {
    PluginKind.ROUTER_STRATEGY: "strategy",
    PluginKind.UPSTREAM_AUTH: "auth",
    PluginKind.A2A_MIDDLEWARE: "a2a_middleware",
}
FIELD_NAME = {
    PluginKind.ROUTER_STRATEGY: "strategy",
    PluginKind.UPSTREAM_AUTH: "plugin",
    PluginKind.A2A_MIDDLEWARE: "plugin",
}
# `lumewarp_cp_gateway_plugins_ignored_total{kind}` also counts what belongs to no kind: a
# descriptor whose kind is unusable, or a non-plugin capability over its own limit.
OTHER_KIND = "other"
MAX_PARAMS_BYTES = 16 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
# Per gateway and kind, whether a name comes from a descriptor or a capability.
MAX_GATEWAY_PLUGINS = 128
_MAX_OTHER_CAPABILITIES = 64
_MAX_CAPABILITY_LENGTH = 128
# A gateway unseen for longer no longer makes a plugin available.
GATEWAY_RETENTION = timedelta(days=30)
PLUGIN_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
HEADER_LIST_WIDGET = "header-list"
_DIGEST = re.compile(r"[0-9a-f]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NEED = re.compile(r"[a-z][a-z0-9_]{0,62}")
_MAX_TITLE = 200
_MAX_DESCRIPTION = 2000
_MAX_NEEDS = 16
_PROTO_KIND_NAMES = {
    "PLUGIN_KIND_ROUTER_STRATEGY": PluginKind.ROUTER_STRATEGY,
    "PLUGIN_KIND_UPSTREAM_AUTH": PluginKind.UPSTREAM_AUTH,
    "PLUGIN_KIND_A2A_MIDDLEWARE": PluginKind.A2A_MIDDLEWARE,
}


def params_json(params: dict[str, Any]) -> str:
    return json.dumps(
        params, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _header_problem(headers: Any) -> bool:
    return not isinstance(headers, list) or any(
        not isinstance(h, str) or not HEADER_NAME.fullmatch(h) or h.lower() in CREDENTIAL_HEADERS
        for h in headers
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
    if "headers" in params and _header_problem(params["headers"]):
        return f"{field}.headers must be an array of HTTP header names"
    return None


def shape_problem(
    kind: PluginKind, name: str, params: dict[str, Any], field: str | None = None
) -> tuple[str, str] | None:
    """(request field, message) of the shape every write naming a plugin must have, also when
    no schema can be consulted. None when it is fine.
    """
    field = field or FIELD_NAME[kind]
    if not PLUGIN_NAME.fullmatch(name):
        return field, f"{field} must match ^[a-z][a-z0-9_]{{0,62}}$"
    if name in BUILTIN_PLUGINS[kind] and params:
        return "params", f"built-in {field} {name!r} takes no params"
    if problem := params_problem(params):
        return "params", problem
    return None


def router_problem(strategy: str, params: dict[str, Any]) -> str | None:
    """The shape a router must have, with the router's own field name in the message."""
    problem = shape_problem(PluginKind.ROUTER_STRATEGY, strategy, params, "strategy")
    return problem[1] if problem else None


# --------------------------------------------------------------------------- schema subset

_COMMON_KEYWORDS = frozenset({"type", "title", "description", "default"})
_OBJECT_KEYWORDS = _COMMON_KEYWORDS | {"properties", "required", "additionalProperties"}
_SCALAR_KEYWORDS = {
    "string": _COMMON_KEYWORDS | {"enum"},
    "integer": _COMMON_KEYWORDS | {"enum", "minimum", "maximum"},
    "number": _COMMON_KEYWORDS | {"enum", "minimum", "maximum"},
    "boolean": _COMMON_KEYWORDS,
    "array": _COMMON_KEYWORDS | {"items", "x-lumewarp-widget"},
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text_problem(path: str, schema: dict[str, Any]) -> str | None:
    for keyword in ("title", "description"):
        if keyword in schema and not isinstance(schema[keyword], str):
            return f"{path}: {keyword} must be a string"
    return None


def _scalar_problem(path: str, schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return f"{path}: not an object schema"
    kind = schema.get("type")
    allowed = _SCALAR_KEYWORDS.get(kind) if isinstance(kind, str) else None
    if allowed is None:
        return f"{path}: unsupported type"
    if unknown := next((key for key in schema if key not in allowed), None):
        return f"{path}: keyword {unknown!r} is outside the subset"
    if problem := _text_problem(path, schema):
        return problem
    for bound in ("minimum", "maximum"):
        if bound in schema and not _is_number(schema[bound]):
            return f"{path}: {bound} must be a number"
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        return f"{path}: minimum is above maximum"
    if "enum" in schema:
        match = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": _is_number,
        }[kind]
        values = schema["enum"]
        if not isinstance(values, list) or not values or not all(match(v) for v in values):
            return f"{path}: enum values must match the type"
    if kind == "array":
        if schema.get("items") != {"type": "string"}:
            return f"{path}: only arrays of strings are allowed"
        if schema.get("x-lumewarp-widget", HEADER_LIST_WIDGET) != HEADER_LIST_WIDGET:
            return f"{path}: unknown x-lumewarp-widget"
    return None


def _object_problem(path: str, schema: Any, *, may_nest: bool) -> str | None:
    if not isinstance(schema, dict):
        return f"{path}: not an object schema"
    if schema.get("type") != "object":
        return f"{path}: type must be object"
    if unknown := next((key for key in schema if key not in _OBJECT_KEYWORDS), None):
        return f"{path}: keyword {unknown!r} is outside the subset"
    if problem := _text_problem(path, schema):
        return problem
    if schema.get("additionalProperties", False) is not False:
        return f"{path}: additionalProperties must be false"
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return f"{path}: properties must be an object"
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or name not in properties for name in required
    ):
        return f"{path}: required must name declared properties"
    for name, prop in properties.items():
        prop_path = f"{path}.{name}"
        if isinstance(prop, dict) and prop.get("type") == "object":
            if not may_nest:
                return f"{prop_path}: only one level of nesting is allowed"
            problem = _object_problem(prop_path, prop, may_nest=False)
        else:
            problem = _scalar_problem(prop_path, prop)
        if problem:
            return problem
    return None


def schema_subset_problem(schema: Any) -> str | None:
    """Why `schema` must not be used, or None. Walks at most two object levels."""
    return _object_problem("$", schema, may_nest=True)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def parse_untrusted_schema(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """(schema, None) when `text` is safe to store and validate with, else (None, problem)."""
    if not text:
        return None, "no schema reported"
    if len(text) > MAX_SCHEMA_BYTES or len(text.encode("utf-8", "replace")) > MAX_SCHEMA_BYTES:
        return None, f"schema exceeds {MAX_SCHEMA_BYTES} bytes"
    try:
        schema = json.loads(text, parse_constant=_reject_constant)
        problem = schema_subset_problem(schema)
        # What JSONB cannot store: NUL, lone surrogates, an overflowed float.
        stored = json.dumps(schema, ensure_ascii=False, allow_nan=False)
        stored.encode("utf-8")
        if "\\u0000" in stored:
            problem = problem or "schema contains NUL"
    except (ValueError, RecursionError, TypeError) as exc:
        return None, f"schema is not usable JSON ({type(exc).__name__})"
    return (None, problem) if problem else (schema, None)


def _integers_only(_: Any, value: Any) -> bool:
    # JSON Schema counts 1.0 as an integer; the gateway's serde does not.
    return isinstance(value, int) and not isinstance(value, bool)


_Validator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _integers_only),
)


def _error_text(error: Any) -> str:
    # The instance is left out of the text: params may be sensitive and reach logs via clients.
    match error.validator:
        case "required" | "additionalProperties":
            return error.message
        case "type":
            return f"must be of type {error.validator_value}"
        case "minimum":
            return f"must be at least {error.validator_value}"
        case "maximum":
            return f"must be at most {error.validator_value}"
        case "enum":
            return f"must be one of {error.validator_value}"
    return f"fails {error.validator}"


def schema_errors(schema: dict[str, Any], instance: dict[str, Any], path: str) -> list[str]:
    """Problems of `instance`; none when the schema cannot be evaluated (cannot validate)."""
    try:
        # An empty registry never fetches, should a `$ref` ever get past the subset check.
        errors = list(_Validator(schema, registry=Registry()).iter_errors(instance))
    except Exception:  # noqa: BLE001 -- untrusted input: `configure` remains the authority
        return []
    problems = []
    for error in errors:
        where = "".join(f".{part}" for part in error.absolute_path)
        problems.append(f"{path}{where}: {_error_text(error)}")
    return sorted(problems)


def _header_list_errors(schema: dict[str, Any], instance: Any, path: str) -> list[str]:
    """The platform's rule on fields a schema marks as header lists, whatever else it says."""
    if not isinstance(instance, dict):
        return []
    problems = []
    for name, prop in schema.get("properties", {}).items():
        if name not in instance:
            continue
        if prop.get("x-lumewarp-widget") == HEADER_LIST_WIDGET:
            if _header_problem(instance[name]):
                problems.append(f"{path}.{name}: must be an array of permitted HTTP header names")
        elif prop.get("type") == "object":
            problems.extend(_header_list_errors(prop, instance[name], f"{path}.{name}"))
    return problems


def settings_errors(
    descriptors: Iterable[dict[str, Any]],
    params: dict[str, Any],
    members: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Settings must pass every descriptor. `members` pairs a display path with its params.

    `headers` is reserved: the gateway removes it before `configure` and it has its own rule.
    """
    settings = {key: value for key, value in params.items() if key != "headers"}
    problems: list[str] = []
    seen: set[str] = set()
    for descriptor in descriptors:
        params_schema = descriptor.get("params_schema")
        member_schema = descriptor.get("member_schema")
        key = json.dumps([params_schema, member_schema], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if params_schema is not None:
            problems += schema_errors(params_schema, settings, "params")
            problems += _header_list_errors(params_schema, settings, "params")
        if member_schema is not None:
            for path, member in members:
                problems += schema_errors(member_schema, member, path)
                problems += _header_list_errors(member_schema, member, path)
    return list(dict.fromkeys(problems))


# --------------------------------------------------------------------------- built-ins


def _manifest_kind(value: str) -> PluginKind:
    """A kind as a manifest spells it: `router-strategy` (docs/plugins.md) or a proto name."""
    if value in {kind.value for kind in PLUGIN_KINDS}:
        return PluginKind(value)
    if value in _PROTO_KIND_NAMES:
        return _PROTO_KIND_NAMES[value]
    raise ValueError(f"unknown plugin kind {value!r} in the built-in manifest")


def _manifest_schema(entry: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A manifest schema: `params_schema_json` (proto spelling) or an inline `params_schema`."""
    if key in entry:
        schema: Any = entry[key]
    elif f"{key}_json" in entry:
        schema = json.loads(entry[f"{key}_json"])
    else:
        return None
    if not isinstance(schema, dict):
        raise ValueError(f"built-in manifest {key} is not an object schema")
    if problem := schema_subset_problem(schema):
        raise ValueError(f"built-in manifest {key} is outside the subset: {problem}")
    return schema


def descriptor_digest(descriptor: dict[str, Any]) -> str:
    """The `digest` of a descriptor: changes whenever any other field does."""
    body = {key: value for key, value in descriptor.items() if key != "digest"}
    return hashlib.sha256(params_json(body).encode("utf-8")).hexdigest()


def _builtin_descriptor(kind: PluginKind, entry: dict[str, Any]) -> dict[str, Any]:
    name = entry.get("name", "")
    if not PLUGIN_NAME.fullmatch(name):
        raise ValueError(f"built-in {kind.value} name {name!r} is invalid")
    router = entry.get("router") or {}
    descriptor = {
        "kind": kind.value,
        "name": name,
        "title": str(entry.get("title", ""))[:_MAX_TITLE],
        "description": str(entry.get("description", ""))[:_MAX_DESCRIPTION],
        "params_schema": _manifest_schema(entry, "params_schema"),
        "member_schema": _manifest_schema(router, "member_schema")
        if kind is PluginKind.ROUTER_STRATEGY
        else None,
        "uses_weight": bool(router.get("uses_weight", False)),
        "needs": list(router.get("needs", [])),
        "wasm_sha256": "",
        "schema_problems": [],
    }
    digest = entry.get("digest", "")
    descriptor["digest"] = digest if _DIGEST.fullmatch(digest) else descriptor_digest(descriptor)
    return descriptor


def _load_builtins() -> dict[PluginKind, dict[str, dict[str, Any]]]:
    """The committed manifest, read once. It is ours, so a problem is a startup error."""
    manifest = json.loads(Path(__file__).with_name("builtin_plugins.json").read_text("utf-8"))
    entries = manifest["plugins"] if isinstance(manifest, dict) else manifest
    builtins: dict[PluginKind, dict[str, dict[str, Any]]] = {kind: {} for kind in PLUGIN_KINDS}
    for entry in entries:
        kind = _manifest_kind(entry["kind"])
        descriptor = _builtin_descriptor(kind, entry)
        if descriptor["name"] in builtins[kind]:
            raise ValueError(f"built-in {kind.value} {descriptor['name']!r} is listed twice")
        builtins[kind][descriptor["name"]] = descriptor
    return builtins


BUILTIN_PLUGINS: dict[PluginKind, dict[str, dict[str, Any]]] = _load_builtins()


# --------------------------------------------------------------------------- Hello


@dataclass(frozen=True)
class ReportedPlugin:
    kind: PluginKind
    name: str
    digest: str
    descriptor: dict[str, Any]


@dataclass(frozen=True)
class NormalizedHello:
    """What a Hello is taken to say, bounded. `plugins` is None when it could not be read:
    what is stored then stays as it is. `ignored` maps a kind (or `other`) to how many
    entries were left out of it.
    """

    capabilities: list[str]
    plugins: list[ReportedPlugin] | None
    ignored: dict[str, int] = field(default_factory=dict)


def bounded_plugin_names(names: Iterable[str]) -> tuple[list[str], int]:
    """The per-gateway set of one kind: valid names, deduplicated, up to the limit, and how
    many entries were left out. Descriptors and capabilities both pass through this.
    """
    kept: dict[str, None] = {}
    ignored = 0
    for name in names:
        if name in kept:
            continue
        if PLUGIN_NAME.fullmatch(name) and len(kept) < MAX_GATEWAY_PLUGINS:
            kept[name] = None
        else:
            ignored += 1
    return list(kept), ignored


def _capability_names(capabilities: Iterable[str], kind: PluginKind) -> Iterable[str]:
    prefix = f"{CAPABILITY_PREFIX[kind]}:"
    return (
        capability[len(prefix) :] for capability in capabilities if capability.startswith(prefix)
    )


def _plugin_capability(capability: str) -> bool:
    return any(capability.startswith(f"{prefix}:") for prefix in CAPABILITY_PREFIX.values())


def _reported_kind(value: int) -> PluginKind | None:
    """A `PluginDescriptor.kind` from the wire; None for one no kind covers."""
    try:
        return _PROTO_KIND_NAMES.get(cpb.PluginKind.Name(value))
    except ValueError:  # an enum value this control plane does not know
        return None


def _clean_text(text: str, limit: int) -> str:
    return text.replace("\x00", "")[:limit]


def _clean_descriptor(kind: PluginKind, item: Any) -> ReportedPlugin:
    params_schema, params_error = parse_untrusted_schema(item.params_schema_json)
    member_schema = None
    member_error = None
    if kind is PluginKind.ROUTER_STRATEGY:
        member_schema, member_error = parse_untrusted_schema(item.router.member_schema_json)
    digest = item.digest
    if not _DIGEST.fullmatch(digest):
        digest = hashlib.sha256(item.SerializeToString(deterministic=True)).hexdigest()
    wasm_sha256 = item.wasm_sha256.lower()
    return ReportedPlugin(
        kind=kind,
        name=item.name,
        digest=digest,
        descriptor={
            "kind": kind.value,
            "name": item.name,
            "title": _clean_text(item.title, _MAX_TITLE),
            "description": _clean_text(item.description, _MAX_DESCRIPTION),
            "params_schema": params_schema,
            "member_schema": member_schema,
            "uses_weight": item.router.uses_weight,
            "needs": [n for n in item.router.needs[:_MAX_NEEDS] if _NEED.fullmatch(n)],
            "wasm_sha256": wasm_sha256 if _SHA256.fullmatch(wasm_sha256) else "",
            "schema_problems": [p for p in (params_error, member_error) if p],
        },
    )


def normalize_hello(hello: Any) -> NormalizedHello:
    """Hello in the form stored. A bad schema is dropped, not the plugin: the gateway still
    has it, only its settings cannot be validated before `configure`. Past the per-kind
    limit the rest is ignored and counted. CPU bound on hostile input: run off the event loop.
    """
    described: dict[PluginKind, dict[str, Any]] = {kind: {} for kind in PLUGIN_KINDS}
    ignored: Counter[str] = Counter()
    for item in hello.plugins:
        kind = _reported_kind(item.kind)
        if kind is None:
            ignored[OTHER_KIND] += 1
            continue
        described[kind].setdefault(item.name, item)
    capabilities: list[str] = []
    others = 0
    for capability in hello.supported_capabilities:
        if _plugin_capability(capability):
            continue
        if (
            others < _MAX_OTHER_CAPABILITIES
            and len(capability) <= _MAX_CAPABILITY_LENGTH
            and "\x00" not in capability
        ):
            capabilities.append(capability)
            others += 1
        else:
            ignored[OTHER_KIND] += 1
    reported: list[ReportedPlugin] = []
    for kind in PLUGIN_KINDS:
        names, dropped = bounded_plugin_names(
            [*described[kind], *_capability_names(hello.supported_capabilities, kind)]
        )
        ignored[kind.value] += dropped
        reported.extend(
            _clean_descriptor(kind, described[kind][name])
            for name in names
            if name in described[kind]
        )
        # A connected gateway's presence is read from here, so every kept plugin is listed.
        capabilities.extend(f"{CAPABILITY_PREFIX[kind]}:{name}" for name in names)
    return NormalizedHello(capabilities=capabilities, plugins=reported, ignored=dict(ignored))


async def replace_gateway_plugins(
    session: AsyncSession,
    scope: str,
    gateway_id: str,
    token_id: uuid.UUID,
    reported: list[ReportedPlugin],
    now: datetime,
) -> None:
    """What a gateway reports replaces all it reported before; a changed digest updates. The
    caller holds the gateway's session row lock and has checked that this Hello still owns it.
    """
    stale = delete(GatewayPlugin).where(
        GatewayPlugin.scope == scope, GatewayPlugin.gateway_id == gateway_id
    )
    if reported:
        stale = stale.where(
            tuple_(GatewayPlugin.kind, GatewayPlugin.name).not_in(
                [(item.kind.value, item.name) for item in reported]
            )
        )
    await session.execute(stale)
    if not reported:
        return
    stmt = pg_insert(GatewayPlugin).values(
        [
            {
                "scope": scope,
                "gateway_id": gateway_id,
                "kind": item.kind,
                "name": item.name,
                "digest": item.digest,
                "descriptor": item.descriptor,
                "token_id": token_id,
                "seen_at": now,
            }
            for item in reported
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                GatewayPlugin.scope,
                GatewayPlugin.gateway_id,
                GatewayPlugin.kind,
                GatewayPlugin.name,
            ],
            set_={
                "digest": stmt.excluded.digest,
                "descriptor": stmt.excluded.descriptor,
                "token_id": stmt.excluded.token_id,
                "seen_at": stmt.excluded.seen_at,
            },
        )
    )


# --------------------------------------------------------------------------- what counts


@dataclass(frozen=True)
class GatewaySupport:
    gateway_id: str
    connected: bool
    seen_at: datetime | None
    digest: str | None
    # None: the gateway advertises the capability without a usable report.
    descriptor: dict[str, Any] | None


@dataclass
class PluginRegistry:
    """The plugins of a scope that count at one moment. Read-only snapshot of the tables."""

    connected: list[str] = field(default_factory=list)
    _support: dict[tuple[PluginKind, str], list[GatewaySupport]] = field(default_factory=dict)

    def builtin_names(self, kind: PluginKind) -> frozenset[str]:
        return frozenset(BUILTIN_PLUGINS[kind])

    def names(self, kind: PluginKind) -> list[str]:
        reported = {name for (reported_kind, name) in self._support if reported_kind is kind}
        return sorted(set(BUILTIN_PLUGINS[kind]) | reported)

    def support(self, kind: PluginKind, name: str) -> list[GatewaySupport]:
        """Gateways with this plugin: connected ones first, then the most recently seen."""
        return self._support.get((kind, name), [])

    def known(self, kind: PluginKind, name: str) -> bool:
        return name in BUILTIN_PLUGINS[kind] or bool(self.support(kind, name))

    def missing(self, kind: PluginKind, name: str) -> list[str]:
        """Connected gateways that lack it: they would NACK every version using it."""
        have = {s.gateway_id for s in self.support(kind, name)}
        return [gateway for gateway in self.connected if gateway not in have]

    def _reported(self, kind: PluginKind, name: str) -> list[dict[str, Any]]:
        return [s.descriptor for s in self.support(kind, name) if s.descriptor is not None]

    def validating(self, kind: PluginKind, name: str) -> list[dict[str, Any]]:
        """Descriptors settings must pass: all connected gateways' (rolling upgrades); with
        none connected, the manifest for a built-in or the newest report otherwise.
        """
        live = [
            s.descriptor
            for s in self.support(kind, name)
            if s.connected and s.descriptor is not None
        ]
        if live:
            return live
        if name in BUILTIN_PLUGINS[kind]:
            return [BUILTIN_PLUGINS[kind][name]]
        return self._reported(kind, name)[:1]

    def current(self, kind: PluginKind, name: str) -> dict[str, Any] | None:
        """What a form shows: the newest connected report, else as in `validating`."""
        validating = self.validating(kind, name)
        return validating[0] if validating else None

    def problems(
        self,
        kind: PluginKind,
        name: str,
        params: dict[str, Any],
        members: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> list[str]:
        """Why stored settings are invalid today. Never changes them."""
        if not self.known(kind, name):
            return [f"no gateway reports {FIELD_NAME[kind]} {name!r}"]
        return settings_errors(self.validating(kind, name), params, members or [])

    def router_problems(
        self, name: str, params: dict[str, Any], members: list[tuple[str, dict[str, Any]]]
    ) -> list[str]:
        """`problems` for a router's strategy, which is the router read path's question."""
        return self.problems(PluginKind.ROUTER_STRATEGY, name, params, members)


async def load_plugins(
    session: AsyncSession, scope: str, *, now: datetime | None = None
) -> PluginRegistry:
    """A gateway counts while the token of its latest Hello is unrevoked and it is connected or
    was seen within 30 days; a report counts while its own token is unrevoked. One statement,
    so this sees one snapshot: never a Hello's capabilities with another Hello's reports. It
    takes no lock, so any caller's lock order is safe.
    """
    cutoff = (now or datetime.now(UTC)) - GATEWAY_RETENTION
    unrevoked = select(GatewayToken.id).where(GatewayToken.revoked_at.is_(None))
    rows = await session.execute(
        select(
            GatewaySession.gateway_id,
            GatewaySession.connected,
            GatewaySession.capabilities,
            GatewaySession.connected_at,
            GatewayPlugin.kind,
            GatewayPlugin.name,
            GatewayPlugin.digest,
            GatewayPlugin.descriptor,
            GatewayPlugin.seen_at,
        )
        .outerjoin(
            GatewayPlugin,
            and_(
                GatewayPlugin.scope == GatewaySession.scope,
                GatewayPlugin.gateway_id == GatewaySession.gateway_id,
                GatewayPlugin.token_id.in_(unrevoked),
            ),
        )
        .where(
            GatewaySession.scope == scope,
            GatewaySession.token_id.in_(unrevoked),
            or_(GatewaySession.connected, GatewaySession.last_seen_at >= cutoff),
        )
    )
    gateways: dict[str, Any] = {}
    reports: dict[str, dict[tuple[PluginKind, str], Any]] = {}
    for row in rows:
        gateways[row.gateway_id] = row
        if row.name is not None:
            reports.setdefault(row.gateway_id, {})[(row.kind, row.name)] = row
    support: dict[tuple[PluginKind, str], list[GatewaySupport]] = {}
    for gateway_id, gateway in gateways.items():
        reported = reports.get(gateway_id, {})
        for kind in PLUGIN_KINDS:
            # A connected gateway says what it has now; an offline one, what it last reported.
            if gateway.connected:
                names, _ = bounded_plugin_names(_capability_names(gateway.capabilities, kind))
            else:
                names, _ = bounded_plugin_names(
                    sorted(name for reported_kind, name in reported if reported_kind is kind)
                )
            for name in names:
                report = reported.get((kind, name))
                support.setdefault((kind, name), []).append(
                    GatewaySupport(
                        gateway_id,
                        gateway.connected,
                        report.seen_at if report else gateway.connected_at,
                        report.digest if report else None,
                        report.descriptor if report else None,
                    )
                )
    oldest = datetime.min.replace(tzinfo=UTC)
    for entries in support.values():
        entries.sort(key=lambda s: (s.connected, s.seen_at or oldest), reverse=True)
    return PluginRegistry(
        connected=sorted(g.gateway_id for g in gateways.values() if g.connected),
        _support=support,
    )


# --------------------------------------------------------------------------- saving


class PluginRejected(Exception):
    """A write naming a plugin cannot be saved. `field` names the offending request field."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


def member_settings(
    members: Sequence[Any], splits: Sequence[Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Each member's params with the path errors name it by, split members included."""
    settings = [(f"members[{i}].params", m.params) for i, m in enumerate(members)]
    for s, split in enumerate(splits):
        settings += [
            (f"traffic_split[{s}].members[{i}].params", m.params)
            for i, m in enumerate(split.members)
        ]
    return settings


def check_plugin(
    registry: PluginRegistry,
    kind: PluginKind,
    name: str,
    params: dict[str, Any],
    *,
    members: list[tuple[str, dict[str, Any]]] | None = None,
    force: bool,
) -> list[str]:
    """The rule for every path that saves or republishes an entity naming a plugin. Raises
    PluginRejected unless the gateways can serve it; returns the connected gateways `force`
    is saving past, which the caller may audit.
    """
    field_name = FIELD_NAME[kind]
    if problem := shape_problem(kind, name, params):
        raise PluginRejected(*problem)
    if not registry.known(kind, name):
        raise PluginRejected(field_name, f"no gateway reports {field_name} {name!r}")
    missing = registry.missing(kind, name)
    if missing and not force:
        raise PluginRejected(
            field_name,
            f"connected gateways lack {name!r} and would reject the whole config: "
            f"{', '.join(missing)}; send force to save anyway",
        )
    if problems := registry.problems(kind, name, params, members):
        raise PluginRejected("params", "; ".join(problems[:10]))
    return missing


async def validate_plugin_params(
    session: AsyncSession,
    kind: PluginKind,
    name: str,
    params: dict[str, Any],
    *,
    force: bool,
    members: list[tuple[str, dict[str, Any]]] | None = None,
    scope: str | None = None,
) -> list[str]:
    """`check_plugin` on a freshly loaded registry: the one call every write makes for any
    kind of plugin. Raises PluginRejected (the API answers 422 with `exc.field`), and returns
    the connected gateways a forced save is saving past, for the caller to audit.
    """
    registry = await load_plugins(session, scope or get_settings().scope)
    return check_plugin(registry, kind, name, params, members=members, force=force)


def audit_forced(
    session: AsyncSession,
    *,
    router_id: uuid.UUID,
    name: str,
    strategy: str,
    missing: list[str],
    principal: Principal | None,
) -> None:
    """Without a principal the actor is the rollback, as in `catalog.audit_grant_removal`."""
    if not missing:
        return
    audit.record(
        session,
        action="router.force_save",
        resource_type="router",
        resource_id=router_id,
        principal=principal,
        actor_type=None if principal else ActorType.SYSTEM,
        actor_id=None if principal else "rollback",
        details={"name": name, "strategy": strategy, "missing_gateways": missing},
    )


@dataclass(frozen=True)
class _SplitMembers:
    members: list[Any]


async def check_stored_routers(
    session: AsyncSession,
    scope: str,
    *,
    force: bool,
    principal: Principal | None,
    now: datetime | None,
) -> None:
    """`check_plugin` on every router in the source tables, for a rollback that has just
    restored them in this transaction: a rejection aborts it all.
    """
    registry = await load_plugins(session, scope, now=now)
    members: dict[uuid.UUID, list[RouterMember]] = {}
    for member in await session.scalars(select(RouterMember).order_by(RouterMember.position)):
        members.setdefault(member.router_id, []).append(member)
    split_members: dict[uuid.UUID, list[RouterSplitMember]] = {}
    for member in await session.scalars(
        select(RouterSplitMember).order_by(RouterSplitMember.position)
    ):
        split_members.setdefault(member.split_id, []).append(member)
    splits: dict[uuid.UUID, list[Any]] = {}
    for split in await session.scalars(select(RouterSplit).order_by(RouterSplit.position)):
        splits.setdefault(split.router_id, []).append(
            _SplitMembers(split_members.get(split.id, []))
        )
    routers = await session.scalars(
        select(Router).order_by(Router.name_id).execution_options(populate_existing=True)
    )
    for router in routers:
        settings = member_settings(members.get(router.id, []), splits.get(router.id, []))
        try:
            missing = check_plugin(
                registry,
                PluginKind.ROUTER_STRATEGY,
                router.strategy,
                router.params,
                members=settings,
                force=force,
            )
        except PluginRejected as exc:
            raise PluginRejected(exc.field, f"router {router.name!r}: {exc.message}") from exc
        audit_forced(
            session,
            router_id=router.id,
            name=router.name,
            strategy=router.strategy,
            missing=missing,
            principal=principal,
        )

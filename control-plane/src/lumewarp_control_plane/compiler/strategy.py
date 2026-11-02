"""Router strategies: what is known about them and validation of their settings.

A gateway reports each compiled strategy in Hello with JSON Schemas of its settings; the
manifest of built-ins is committed here so they work with no gateway connected. Schemas from
gateways are untrusted: they are capped, restricted to a subset that has no `$ref` and no
`pattern`, and otherwise ignored, in which case only the gateway's `configure` validates.
Keep the subset in step with the gateway's `lumewarp-router-api/src/schema.rs`; both pass
`lumewarp-router-api/testdata/schema-subset.json`.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators
from referencing import Registry
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import GatewaySession, GatewayStrategy, GatewayToken

_MANIFEST = json.loads(Path(__file__).with_name("builtin_strategies.json").read_text("utf-8"))
BUILTIN_DESCRIPTORS: dict[str, dict[str, Any]] = {d["name"]: d for d in _MANIFEST["strategies"]}
BUILTIN_STRATEGIES = frozenset(BUILTIN_DESCRIPTORS)

MAX_PARAMS_BYTES = 16 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
MAX_HELLO_STRATEGIES = 128
# A gateway unseen for longer no longer makes a strategy available.
GATEWAY_RETENTION = timedelta(days=30)
STRATEGY_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
HEADER_LIST_WIDGET = "header-list"
_DIGEST = re.compile(r"[0-9a-f]{1,128}")
_NEED = re.compile(r"[a-z][a-z0-9_]{0,62}")
_MAX_TITLE = 200
_MAX_DESCRIPTION = 2000
_MAX_NEEDS = 16


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


def strategy_problem(strategy: str, params: dict[str, Any]) -> str | None:
    """The shape every router must have, also when no schema can be consulted."""
    if not STRATEGY_NAME.fullmatch(strategy):
        return "strategy must match ^[a-z][a-z0-9_]{0,62}$"
    if strategy in BUILTIN_STRATEGIES and params:
        return f"built-in strategy {strategy!r} takes no params"
    return params_problem(params)


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


# --------------------------------------------------------------------------- Hello


@dataclass(frozen=True)
class ReportedStrategy:
    name: str
    digest: str
    descriptor: dict[str, Any]


def _clean_text(text: str, limit: int) -> str:
    return text.replace("\x00", "")[:limit]


def clean_reported_strategies(reported: Iterable[Any]) -> list[ReportedStrategy]:
    """Hello's descriptors in the form stored. A bad schema is dropped, not the strategy: the
    gateway still has it, only its settings cannot be validated before `configure`.
    """
    cleaned: dict[str, ReportedStrategy] = {}
    for item in reported:
        if not STRATEGY_NAME.fullmatch(item.name) or (
            item.name not in cleaned and len(cleaned) >= MAX_HELLO_STRATEGIES
        ):
            continue
        params_schema, params_error = parse_untrusted_schema(item.params_schema_json)
        member_schema, member_error = parse_untrusted_schema(item.member_schema_json)
        digest = item.digest
        if not _DIGEST.fullmatch(digest):
            digest = hashlib.sha256(item.SerializeToString(deterministic=True)).hexdigest()
        cleaned[item.name] = ReportedStrategy(
            name=item.name,
            digest=digest,
            descriptor={
                "name": item.name,
                "title": _clean_text(item.title, _MAX_TITLE),
                "description": _clean_text(item.description, _MAX_DESCRIPTION),
                "uses_weight": item.uses_weight,
                "needs": [n for n in item.needs[:_MAX_NEEDS] if _NEED.fullmatch(n)],
                "params_schema": params_schema,
                "member_schema": member_schema,
                "schema_problems": [p for p in (params_error, member_error) if p],
            },
        )
    return list(cleaned.values())


async def replace_gateway_strategies(
    session: AsyncSession,
    scope: str,
    gateway_id: str,
    reported: list[ReportedStrategy],
    now: datetime,
) -> None:
    """What a gateway reports replaces all it reported before; a changed digest updates."""
    await session.execute(
        delete(GatewayStrategy).where(
            GatewayStrategy.scope == scope,
            GatewayStrategy.gateway_id == gateway_id,
            GatewayStrategy.name.not_in([item.name for item in reported]),
        )
    )
    if not reported:
        return
    stmt = pg_insert(GatewayStrategy).values(
        [
            {
                "scope": scope,
                "gateway_id": gateway_id,
                "name": item.name,
                "digest": item.digest,
                "descriptor": item.descriptor,
                "seen_at": now,
            }
            for item in reported
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                GatewayStrategy.scope,
                GatewayStrategy.gateway_id,
                GatewayStrategy.name,
            ],
            set_={
                "digest": stmt.excluded.digest,
                "descriptor": stmt.excluded.descriptor,
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
class StrategyRegistry:
    """The strategies of a scope that count at one moment. Read-only snapshot of the tables."""

    connected: list[str] = field(default_factory=list)
    _support: dict[str, list[GatewaySupport]] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(BUILTIN_STRATEGIES | set(self._support))

    def support(self, name: str) -> list[GatewaySupport]:
        """Gateways with this strategy: connected ones first, then the most recently seen."""
        return self._support.get(name, [])

    def known(self, name: str) -> bool:
        return name in BUILTIN_STRATEGIES or bool(self.support(name))

    def missing(self, name: str) -> list[str]:
        """Connected gateways that lack it: they would NACK every version using it."""
        have = {s.gateway_id for s in self.support(name)}
        return [gateway for gateway in self.connected if gateway not in have]

    def _reported(self, name: str) -> list[dict[str, Any]]:
        return [s.descriptor for s in self.support(name) if s.descriptor is not None]

    def validating(self, name: str) -> list[dict[str, Any]]:
        """Descriptors settings must pass: all connected gateways' (rolling upgrades); with
        none connected, the manifest for a built-in or the newest report otherwise.
        """
        live = [
            s.descriptor for s in self.support(name) if s.connected and s.descriptor is not None
        ]
        if live:
            return live
        if name in BUILTIN_DESCRIPTORS:
            return [BUILTIN_DESCRIPTORS[name]]
        return self._reported(name)[:1]

    def current(self, name: str) -> dict[str, Any] | None:
        """What a form shows: the newest connected report, else as in `validating`."""
        validating = self.validating(name)
        return validating[0] if validating else None

    def router_problems(
        self, name: str, params: dict[str, Any], members: list[tuple[str, dict[str, Any]]]
    ) -> list[str]:
        """Why stored settings are invalid today. Never changes them."""
        if not self.known(name):
            return [f"no gateway reports strategy {name!r}"]
        return settings_errors(self.validating(name), params, members)


async def load_strategies(
    session: AsyncSession, scope: str, *, now: datetime | None = None
) -> StrategyRegistry:
    """Only gateways with an unrevoked token that are connected or seen within 30 days count."""
    cutoff = (now or datetime.now(UTC)) - GATEWAY_RETENTION
    tokened = set(
        (
            await session.scalars(
                select(GatewayToken.gateway_id).where(
                    GatewayToken.scope == scope, GatewayToken.revoked_at.is_(None)
                )
            )
        ).all()
    )
    sessions = {
        row.gateway_id: row
        for row in (
            await session.scalars(select(GatewaySession).where(GatewaySession.scope == scope))
        ).all()
        if row.gateway_id in tokened
        and (row.connected or (row.last_seen_at is not None and row.last_seen_at >= cutoff))
    }
    support: dict[str, dict[str, GatewaySupport]] = {}
    for row in (
        await session.scalars(select(GatewayStrategy).where(GatewayStrategy.scope == scope))
    ).all():
        gateway = sessions.get(row.gateway_id)
        if gateway is not None:
            support.setdefault(row.name, {})[row.gateway_id] = GatewaySupport(
                row.gateway_id, gateway.connected, row.seen_at, row.digest, row.descriptor
            )
    # A connected gateway's capabilities are authoritative for presence, report or not.
    for gateway in sessions.values():
        if not gateway.connected:
            continue
        for capability in gateway.capabilities:
            name = capability.removeprefix("strategy:")
            if name != capability and gateway.gateway_id not in support.get(name, {}):
                support.setdefault(name, {})[gateway.gateway_id] = GatewaySupport(
                    gateway.gateway_id, True, gateway.connected_at, None, None
                )
    oldest = datetime.min.replace(tzinfo=UTC)
    return StrategyRegistry(
        connected=sorted(g.gateway_id for g in sessions.values() if g.connected),
        _support={
            name: sorted(
                entries.values(),
                key=lambda s: (s.connected, s.seen_at or oldest),
                reverse=True,
            )
            for name, entries in support.items()
        },
    )

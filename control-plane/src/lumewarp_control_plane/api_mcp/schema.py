"""Tool schemas from the REST API's OpenAPI document.

A tool's input is one flat JSON object: the operation's path and query parameters plus the
properties of its JSON body, with every `$ref` inlined (MCP clients and models handle flat,
self-contained schemas best). `readOnly` body fields are dropped.

Tools declare no output schema: the response schemas run to ~140 KB for the whole table, a
context cost some clients pay on every turn. The result still carries the JSON body as
structured content; a body whose root is not an object is wrapped as `{"result": ...}`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

_REF_PREFIX = "#/components/schemas/"
# Inlining depth cap: the exposed operations' input schemas are shallow; a deeper one is a
# recursive component, which is left as an unconstrained value.
_MAX_DEPTH = 12


@dataclass(frozen=True)
class Operation:
    """One REST operation, as much of it as a tool call needs."""

    operation_id: str
    method: str
    path: str
    description: str
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_fields: tuple[str, ...] | None  # None: the operation takes no JSON body
    body_required: bool
    input_schema: dict[str, Any]
    wraps_result: bool  # structured content is `{"result": <body>}`


def operations(spec: dict[str, Any]) -> dict[str, Operation]:
    """Every operation under `/api/` of an OpenAPI document, by operationId."""
    components = spec.get("components", {}).get("schemas", {})
    out: dict[str, Operation] = {}
    for path, item in spec["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            out[op["operationId"]] = _operation(method.upper(), path, op, components)
    return out


def _operation(method: str, path: str, op: dict, components: dict) -> Operation:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    for param in op.get("parameters", []):
        name = param["name"]
        where = param["in"]
        if where == "path":
            path_params.append(name)
        elif where == "query":
            query_params.append(name)
        else:  # headers and cookies are the transport's business, never a tool argument
            continue
        schema = _inline(param.get("schema", {}), components)
        if param.get("description") and "description" not in schema:
            schema["description"] = param["description"]
        properties[name] = schema
        if param.get("required"):
            required.append(name)

    body_fields: tuple[str, ...] | None = None
    body_required = False
    request_body = op.get("requestBody")
    if request_body is not None:
        body_schema = request_body["content"]["application/json"]["schema"]
        body = _inline(body_schema, components)
        body = _strip_nullable(body)
        if body.get("type") != "object":
            raise ValueError(f"{op['operationId']}: a JSON body must be an object to flatten")
        body_required = bool(request_body.get("required"))
        names: list[str] = []
        for name, schema in body.get("properties", {}).items():
            if schema.get("readOnly"):
                continue
            if name in properties:
                raise ValueError(f"{op['operationId']}: body field {name!r} shadows a parameter")
            properties[name] = schema
            names.append(name)
            if body_required and name in body.get("required", []):
                required.append(name)
        body_fields = tuple(names)

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    input_schema["additionalProperties"] = False

    description = op.get("description") or op.get("summary") or op["operationId"]
    return Operation(
        operation_id=op["operationId"],
        method=method,
        path=path,
        description=description.strip(),
        path_params=tuple(path_params),
        query_params=tuple(query_params),
        body_fields=body_fields,
        body_required=body_required,
        input_schema=input_schema,
        wraps_result=_wraps_result(op, components),
    )


def _wraps_result(op: dict, components: dict) -> bool:
    """Whether the 2xx JSON body's root is not an object (MCP structured content must be)."""
    for status, response in op.get("responses", {}).items():
        if not status.startswith("2"):
            continue
        content = (response.get("content") or {}).get("application/json")
        if not content or "schema" not in content:
            return False
        root = content["schema"]
        ref = root.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
            root = components[ref.removeprefix(_REF_PREFIX)]
        return root.get("type") != "object"
    return False


def _inline(schema: dict, components: dict, depth: int = 0) -> dict:
    """A copy of `schema` with every component `$ref` replaced by the component."""
    if depth > _MAX_DEPTH:
        return {}
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith(_REF_PREFIX):
            raise ValueError(f"unsupported $ref {ref!r}")
        target = _inline(components[ref.removeprefix(_REF_PREFIX)], components, depth + 1)
        # Sibling keywords (a parameter's description next to its $ref) win.
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        return _clean({**target, **_clean(siblings)})
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: _inline(sub, components, depth + 1)
                for name, sub in value.items()
                if not (isinstance(sub, dict) and sub.get("readOnly"))
            }
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            out[key] = _inline(value, components, depth + 1)
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems") and isinstance(value, list):
            out[key] = [_inline(sub, components, depth + 1) for sub in value]
        else:
            out[key] = copy.deepcopy(value)
    return _clean(out)


def _clean(schema: dict) -> dict:
    """Drop keywords that only cost tokens: generated titles."""
    if isinstance(schema.get("title"), str):
        schema = {k: v for k, v in schema.items() if k != "title"}
    return schema


def _strip_nullable(schema: dict) -> dict:
    """`anyOf: [X, null]` (an optional body) -> X."""
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        rest = [v for v in variants if v.get("type") != "null"]
        if len(rest) == 1:
            merged = {k: v for k, v in schema.items() if k != "anyOf"}
            return {**rest[0], **merged}
    return schema

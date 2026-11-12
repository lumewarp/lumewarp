"""The OpenAPI document stays complete enough to be the API reference.

It is published with the manual and served at `/docs`, so a new operation or request
field that explains nothing fails here instead of shipping as a blank row.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.settings import Settings

METHODS = ("get", "post", "put", "patch", "delete")
SECURITY = [{"PersonalAccessToken": []}, {"Session": []}]
# Reached without credentials: the login flow, the session probe and the health checks.
PUBLIC = {
    "getSession",
    "loginBreakGlass",
    "startOidcLogin",
    "completeOidcLogin",
    "healthz_healthz_get",
    "readyz_readyz_get",
}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        background_enabled=False,
    )
    return create_app(settings).openapi()


def operations(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield f"{method.upper()} {path}", operation


def refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"].rsplit("/", 1)[-1]
        for value in node.values():
            yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def test_every_operation_has_a_description(spec: dict[str, Any]) -> None:
    missing = [name for name, op in operations(spec) if not op.get("description", "").strip()]
    assert not missing, f"add a docstring to the handler of: {missing}"


def test_security_is_declared_on_everything_but_the_public_operations(
    spec: dict[str, Any],
) -> None:
    assert set(spec["components"]["securitySchemes"]) == {"PersonalAccessToken", "Session"}
    public = set()
    for name, op in operations(spec):
        if "security" not in op:
            public.add(op["operationId"])
        else:
            assert op["security"] == SECURITY, name
    assert public == PUBLIC


def test_every_tag_is_described_and_used(spec: dict[str, Any]) -> None:
    described = {tag["name"] for tag in spec["tags"] if tag.get("description")}
    used = {tag for _, op in operations(spec) for tag in op.get("tags", [])}
    assert described == used


def test_every_success_response_is_typed(spec: dict[str, Any]) -> None:
    untyped = []
    for name, op in operations(spec):
        for status, response in op["responses"].items():
            for media in response.get("content", {}).values():
                if status.startswith("2") and not media.get("schema"):
                    untyped.append(name)
    assert not untyped


def test_every_request_field_has_a_description(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    pending = {name for _, op in operations(spec) for name in refs(op.get("requestBody", {}))}
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        reached.add(name)
        pending |= set(refs(schemas[name])) - reached
    missing = sorted(
        f"{name}.{field}"
        for name in reached
        for field, prop in schemas[name].get("properties", {}).items()
        if not prop.get("description")
    )
    assert not missing, f"describe these on the field or in api/field_docs.py: {missing}"


def test_rate_limited_operations_document_429(spec: dict[str, Any]) -> None:
    limited = {op["operationId"] for _, op in operations(spec) if "429" in op["responses"]}
    assert limited == {"createKey", "updateKey", "rotateKey", "loginBreakGlass"}

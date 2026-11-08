"""Schemas from gateways are untrusted: the subset, the limits, and what validates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumewarp_control_plane.compiler.plugins import (
    BUILTIN_PLUGINS,
    MAX_SCHEMA_BYTES,
    params_problem,
    parse_untrusted_schema,
    schema_errors,
    schema_subset_problem,
    settings_errors,
)
from lumewarp_control_plane.compiler.validate import SUPPORTED_CAPABILITIES
from lumewarp_control_plane.models import PluginKind

# The subset exists on both sides: the gateway's SDK normalizes a schema with the same rules
# this module enforces. The file moves with the SDK, so try the crates it has lived in.
_CASES = next(
    (
        path
        for path in (
            Path(__file__).parents[3]
            / "gateway/crates/lumewarp-plugin-sdk/testdata/schema-subset.json",
            Path(__file__).parents[3]
            / "gateway/crates/lumewarp-router-api/testdata/schema-subset.json",
        )
        if path.exists()
    ),
    None,
)
if _CASES is None:  # pragma: no cover -- a hand-off, not a behaviour change
    pytest.skip("the gateway's schema-subset testdata is missing", allow_module_level=True)
CASES = json.loads(_CASES.read_text("utf-8"))
SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 5},
        "ratio": {"type": "number"},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "nested": {
            "type": "object",
            "properties": {"on": {"type": "boolean"}},
            "required": ["on"],
            "additionalProperties": False,
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "pass": {"type": "array", "items": {"type": "string"}, "x-lumewarp-widget": "header-list"},
    },
    "required": ["n"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("schema", CASES["inside"], ids=lambda s: json.dumps(s)[:50])
def test_same_subset_as_the_gateway_inside(schema):
    assert schema_subset_problem(schema) is None
    assert parse_untrusted_schema(json.dumps(schema))[0] == schema


@pytest.mark.parametrize("schema", CASES["outside"], ids=lambda s: json.dumps(s)[:50])
def test_same_subset_as_the_gateway_outside(schema):
    assert schema_subset_problem(schema) is not None
    assert parse_untrusted_schema(json.dumps(schema))[0] is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        "null",
        '{"type":"object","title":NaN}',
        '{"type":"object","default":1e999}',
        '{"type":"object","title":"\\ud800"}',
        '{"type":"object","title":"\\u0000"}',
        '{"type":"object","default":' + "1" * 5000 + "}",
        "[" * 100_000,
        json.dumps({"type": "object", "description": "x" * MAX_SCHEMA_BYTES}),
        # Multi-byte: under the limit in characters, over it in bytes.
        json.dumps(
            {"type": "object", "description": "é" * (MAX_SCHEMA_BYTES // 2 + 1)},
            ensure_ascii=False,
        ),
    ],
    ids=lambda t: t[:30],
)
def test_unusable_text_is_a_problem_and_never_raises(text):
    schema, problem = parse_untrusted_schema(text)
    assert schema is None and problem


def test_a_schema_at_the_limit_is_accepted():
    padding = MAX_SCHEMA_BYTES - len(json.dumps({"type": "object", "description": ""}))
    text = json.dumps({"type": "object", "description": "x" * padding})
    assert len(text) == MAX_SCHEMA_BYTES
    assert parse_untrusted_schema(text)[0] is not None


def test_validation_messages_name_the_field_and_not_the_value():
    problems = schema_errors(
        SCHEMA,
        {
            "n": "private-value",
            "ratio": "private-value",
            "mode": "private-value",
            "nested": {},
            "tags": [1],
        },
        "params",
    )
    text = "\n".join(problems)
    assert "params.n: must be of type integer" in text
    assert "params.mode: must be one of" in text
    assert "'on' is a required property" in text
    assert "params.tags.0: must be of type string" in text
    assert "private-value" not in text


@pytest.mark.parametrize(
    ("instance", "ok"),
    [
        ({"n": 1}, True),
        ({"n": 5, "ratio": 1, "mode": "b", "nested": {"on": True}, "tags": ["x"]}, True),
        ({}, False),
        ({"n": 0}, False),
        ({"n": 6}, False),
        # The gateway's serde refuses these, so they would otherwise be NACKs.
        ({"n": 1.0}, False),
        ({"n": True}, False),
        ({"n": None}, False),
        ({"n": 1, "unknown": 1}, False),
        ({"n": 1, "nested": {"on": True, "unknown": 1}}, False),
    ],
)
def test_jsonschema_agrees_with_what_configure_accepts(instance, ok):
    assert (not schema_errors(SCHEMA, instance, "params")) is ok


def test_a_ref_past_the_subset_check_never_fetches():
    # schema_errors is only reached with subset-checked schemas; this is the second layer.
    schema = {"$ref": "http://169.254.169.254/latest/meta-data"}
    assert schema_errors(schema, {}, "params") == []


@pytest.mark.parametrize(
    "header",
    [
        "authorization",
        "Cookie",
        "SET-COOKIE",
        "proxy-authorization",
        "x-api-key",
        "x-lumewarp-key",
    ],
)
def test_header_list_widget_blocks_credentials_whatever_the_schema(header):
    descriptor = {"params_schema": SCHEMA, "member_schema": SCHEMA}
    problems = settings_errors(
        [descriptor],
        {"n": 1, "pass": [header]},
        [("members[0].params", {"n": 1, "pass": [header]})],
    )
    assert any("params.pass" in p and "permitted" in p for p in problems)
    assert any("members[0].params.pass" in p for p in problems)
    assert not settings_errors([descriptor], {"n": 1, "pass": ["x-session"]}, [])


def test_headers_are_reserved_only_for_router_strategies():
    descriptor = {"params_schema": SCHEMA, "member_schema": None}
    # A router strategy's `headers` is the platform's: removed before the schema sees it.
    assert not settings_errors(
        [descriptor], {"n": 1, "headers": ["x-pin"]}, [], PluginKind.ROUTER_STRATEGY
    )
    # Every other kind validates it like any other setting: this schema does not declare it.
    problems = settings_errors(
        [descriptor], {"n": 1, "headers": ["x-pin"]}, [], PluginKind.UPSTREAM_AUTH
    )
    assert any("headers" in problem and "unexpected" in problem for problem in problems)
    problems = settings_errors(
        [descriptor], {"n": 1, "headers": ["x-pin"]}, [], PluginKind.A2A_MIDDLEWARE
    )
    assert problems


def test_allowed_origins_is_reserved_for_upstream_auth():
    schema = {
        "type": "object",
        "properties": {"audience": {"type": "string"}},
        "required": ["audience"],
        "additionalProperties": False,
    }
    descriptor = {"params_schema": schema, "member_schema": None}
    params = {"audience": "lumewarp", "allowed_origins": ["https://idp.example"]}
    assert not settings_errors([descriptor], params, [], PluginKind.UPSTREAM_AUTH)
    # Another kind's reserved key does not exist here, so it must be declared to be allowed.
    for kind in (PluginKind.ROUTER_STRATEGY, PluginKind.A2A_MIDDLEWARE):
        problems = settings_errors([descriptor], params, [], kind)
        assert any("allowed_origins" in problem for problem in problems)


@pytest.mark.parametrize(
    "origins",
    [
        [],
        ["https://idp.example"],
        ["https://idp.example:8443/"],
        ["http://127.0.0.1:8080"],
        ["https://idp.example", "http://other.example"],
        [f"https://h{i}.example" for i in range(32)],
    ],
)
def test_valid_allowed_origins(origins):
    assert params_problem({"allowed_origins": origins}, kind=PluginKind.UPSTREAM_AUTH) is None


@pytest.mark.parametrize(
    "origins",
    [
        "https://idp.example",
        ["https://idp.example/token"],
        ["https://idp.example?x=1"],
        ["https://idp.example#f"],
        ["https://user:pw@idp.example"],
        ["ftp://idp.example"],
        [7],
        ["idp.example"],
        [f"https://h{i}.example" for i in range(33)],
    ],
)
def test_invalid_allowed_origins(origins):
    assert params_problem({"allowed_origins": origins}, kind=PluginKind.UPSTREAM_AUTH) is not None


def test_the_reserved_key_rules_belong_to_their_kind():
    # `headers` on an auth plugin is an ordinary setting: no header-name rule applies.
    assert params_problem({"headers": {"x": 1}}, kind=PluginKind.UPSTREAM_AUTH) is None
    assert params_problem({"headers": ["authorization"]}, kind=PluginKind.ROUTER_STRATEGY)
    # ... and `allowed_origins` on a router is an ordinary setting too (its schema decides).
    assert (
        params_problem({"allowed_origins": ["https://a.example"]}, kind=PluginKind.ROUTER_STRATEGY)
        is None
    )
    assert params_problem({"headers": ["x-ok"]}, kind=PluginKind.A2A_MIDDLEWARE) is None


def test_unusable_schemas_mean_cannot_validate():
    descriptor = {"params_schema": None, "member_schema": None}
    assert not settings_errors([descriptor], {"anything": 1}, [("m", {"anything": 1})])


def test_manifest_is_the_built_ins_and_every_schema_is_in_the_subset():
    strategies = BUILTIN_PLUGINS[PluginKind.ROUTER_STRATEGY]
    assert {f"strategy:{name}" for name in strategies} <= SUPPORTED_CAPABILITIES
    assert set(strategies) == {"weighted", "least_in_flight", "lowest_latency", "vllm_load"}
    assert set(BUILTIN_PLUGINS[PluginKind.UPSTREAM_AUTH]) == {
        "none",
        "static_bearer",
        "passthrough",
    }
    assert BUILTIN_PLUGINS[PluginKind.A2A_MIDDLEWARE] == {}
    for kind, descriptors in BUILTIN_PLUGINS.items():
        for descriptor in descriptors.values():
            assert descriptor["kind"] == kind.value
            assert schema_subset_problem(descriptor["params_schema"]) is None
            if kind is PluginKind.ROUTER_STRATEGY:
                assert schema_subset_problem(descriptor["member_schema"]) is None
            else:
                assert descriptor["member_schema"] is None
    assert strategies["weighted"]["uses_weight"] is True
    assert strategies["vllm_load"]["needs"] == ["vllm_load"]
    assert strategies["lowest_latency"]["needs"] == ["latency"]

"""JCS hashing: the same vectors as `gateway/crates/lumewarp-gateway/src/mcp_hash.rs`.

The control plane approves a definition by the hash a gateway reported, so these two
implementations must produce identical bytes for identical JSON. Every number vector, the
string escaping, the key order and both projections are copied from the Rust tests; a
regression on either side fails here or there.
"""

import hashlib
import json
import struct

import pytest

from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import McpDefinitionKind

TOOL = McpDefinitionKind.TOOL
PROMPT = McpDefinitionKind.PROMPT


def _f64(bits: int) -> float:
    return struct.unpack(">d", bits.to_bytes(8, "big"))[0]


# RFC 8785 Appendix B, transcribed through ECMAScript `String(Number)`: the bit pattern is
# the RFC's, the expected text is what the JCS number rule produces for it.
RFC_NUMBERS = [
    (0x0000000000000000, "0"),
    (0x8000000000000000, "0"),
    (0x0000000000000001, "5e-324"),
    (0x8000000000000001, "-5e-324"),
    (0x7FEFFFFFFFFFFFFF, "1.7976931348623157e+308"),
    (0xFFEFFFFFFFFFFFFF, "-1.7976931348623157e+308"),
    (0x4340000000000000, "9007199254740992"),
    (0xC340000000000000, "-9007199254740992"),
    (0x4430000000000000, "295147905179352830000"),
    (0x44B52D02C7E14AF5, "9.999999999999997e+22"),
    (0x44B52D02C7E14AF6, "1e+23"),
    (0x44B52D02C7E14AF7, "1.0000000000000001e+23"),
    (0x444B1AE4D6E2EF4E, "999999999999999700000"),
    (0x444B1AE4D6E2EF4F, "999999999999999900000"),
    (0x444B1AE4D6E2EF50, "1e+21"),
    (0x3EB0C6F7A0B5ED8C, "9.999999999999997e-7"),
    (0x3EB0C6F7A0B5ED8D, "0.000001"),
    (0x41B3DE4355555553, "333333333.3333332"),
    (0x41B3DE4355555554, "333333333.33333325"),
    (0x41B3DE4355555555, "333333333.3333333"),
    (0x41B3DE4355555556, "333333333.3333334"),
    (0x41B3DE4355555557, "333333333.33333343"),
    (0xBECBF647612F3696, "-0.0000033333333333333333"),
    (0x43143FF3C1CB0959, "1424953923781206.2"),
]


@pytest.mark.parametrize(("bits", "expected"), RFC_NUMBERS)
def test_rfc8785_number_serialization(bits: int, expected: str) -> None:
    assert hashing.canonicalize(_f64(bits)) == expected


def test_number_boundaries_that_do_not_use_exponents_in_jcs() -> None:
    # ES6 switches to the exponent form at 1e21, not at 1e16 like Python's repr.
    assert hashing.canonicalize(1e16) == "10000000000000000"
    assert hashing.canonicalize(1e17) == "100000000000000000"
    assert hashing.canonicalize(1e20) == "100000000000000000000"
    assert hashing.canonicalize(1e21) == "1e+21"
    assert hashing.canonicalize(1e30) == "1e+30"
    assert hashing.canonicalize(1e-6) == "0.000001"
    assert hashing.canonicalize(1e-7) == "1e-7"
    assert hashing.canonicalize(2e-3) == "0.002"
    assert hashing.canonicalize(-0.0) == "0"
    assert hashing.canonicalize(4.50) == "4.5"
    assert hashing.canonicalize(-1.5) == "-1.5"
    assert hashing.canonicalize(100.0) == "100"


def test_json_integers_are_doubles() -> None:
    assert hashing.canonicalize(9007199254740992) == "9007199254740992"
    # 2^53 + 1 is not representable: JCS rounds it (RFC 8785 3.2.2.3).
    assert hashing.canonicalize(9007199254740993) == "9007199254740992"
    assert hashing.canonicalize(1000000000000000000000) == "1e+21"
    assert hashing.canonicalize(json.loads("1000000000000000000000")) == "1e+21"


def test_long_forms_stay_shortest_and_closest() -> None:
    assert hashing.canonicalize(0.1 + 0.2) == "0.30000000000000004"
    assert hashing.canonicalize(1.0 / 3.0) == "0.3333333333333333"
    assert hashing.canonicalize(_f64(1)) == "5e-324"
    assert hashing.canonicalize(_f64(2)) == "1e-323"
    assert hashing.canonicalize(1.0000000000000002) == "1.0000000000000002"


def test_string_escaping_follows_ecmascript() -> None:
    assert hashing.canonicalize('\u20ac$\x0f\nA\'B"\\\\"/') == '"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"'
    # U+2028/U+2029 are not escaped, non-BMP characters stay as UTF-8.
    assert hashing.canonicalize("\u2028\u2029") == '"\u2028\u2029"'
    assert hashing.canonicalize("😀") == '"😀"'
    for char, escaped in [
        ("\x00", "\\u0000"),
        ("\x1f", "\\u001f"),
        ("\x08", "\\b"),
        ("\x09", "\\t"),
        ("\x0a", "\\n"),
        ("\x0c", "\\f"),
        ("\x0d", "\\r"),
    ]:
        assert hashing.canonicalize(char) == f'"{escaped}"'


def test_keys_sort_by_utf16_code_units() -> None:
    # U+1F600 (surrogate pair D83D DE00) sorts before U+FF61 in UTF-16, while UTF-8 byte
    # order would sort it last.
    assert hashing.canonicalize({"\uff61": 1, "\U0001f600": 2, "a": 3}) == ('{"a":3,"😀":2,"｡":1}')
    assert hashing.canonicalize({"b": {"z": 1, "a": 2}, "a": []}) == '{"a":[],"b":{"a":2,"z":1}}'


def test_rfc8785_whole_document_vector() -> None:
    # RFC 8785 Appendix B's object, as parsed from JSON text.
    value = json.loads(
        r"""{
            "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
            "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
            "literals": [null, true, false]
        }"""
    )
    assert hashing.canonicalize(value) == (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


def test_tool_projection_drops_every_key_not_listed() -> None:
    tool = {
        "name": "echo",
        "title": "Echo",
        "description": "Echoes text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "outputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
        "icons": [{"src": "https://example.test/icon.png"}],
        "_meta": {"vendor": {"hidden": True}},
        "x-vendor": 1,
    }
    observed = hashing.observe(TOOL, tool)
    assert observed is not None
    assert observed.name == "echo"
    assert observed.canonical_json == (
        '{"annotations":{"readOnlyHint":true},'
        '"description":"Echoes text",'
        '"inputSchema":{"properties":{"text":{"type":"string"}},"type":"object"},'
        '"name":"echo",'
        '"outputSchema":{"type":"object"},'
        '"title":"Echo"}'
    )
    assert observed.hash == hashing.hash_canonical(observed.canonical_json)
    assert observed.hash != b"\x00" * 32


def test_prompt_projection_and_omitted_keys() -> None:
    observed = hashing.observe(
        PROMPT,
        {
            "name": "greet",
            "description": "Greets",
            "arguments": [{"name": "who", "required": True}],
            "_meta": {"page": 1},
        },
    )
    assert observed is not None
    # "title" was omitted upstream, so it is not added as null.
    assert observed.canonical_json == (
        '{"arguments":[{"name":"who","required":true}],"description":"Greets","name":"greet"}'
    )


def test_hash_ignores_key_order_and_unknown_keys_but_not_content() -> None:
    a = hashing.observe(
        TOOL,
        json.loads('{"name":"t","description":"d","inputSchema":{"type":"object"},"_meta":{}}'),
    )
    b = hashing.observe(
        TOOL, json.loads('{"inputSchema":{"type":"object"},"description":"d","name":"t"}')
    )
    c = hashing.observe(
        TOOL, json.loads('{"name":"t","description":"changed","inputSchema":{"type":"object"}}')
    )
    assert a is not None and b is not None and c is not None
    assert a == b
    assert a.hash != c.hash


def test_rejects_definitions_that_cannot_be_approved() -> None:
    assert hashing.observe(None, {"name": "t"}) is None
    assert hashing.observe(TOOL, ["name"]) is None
    assert hashing.observe(TOOL, {"description": "d"}) is None
    assert hashing.observe(TOOL, {"name": ""}) is None
    assert hashing.observe(TOOL, {"name": 7}) is None
    assert hashing.observe(TOOL, {"name": "x" * (hashing.MAX_NAME_BYTES + 1)}) is None
    assert hashing.keys(None) is None
    assert hashing.keys(TOOL) == hashing.TOOL_KEYS
    assert hashing.keys(PROMPT) == hashing.PROMPT_KEYS


def test_canonical_text_is_stable_across_runs() -> None:
    tool = {"name": "n", "inputSchema": {"type": "object"}}
    first = hashing.observe(TOOL, tool)
    for _ in range(10):
        assert hashing.observe(TOOL, tool) == first
    # sha256("{}") is a fixed, well-known value: proves the hash input is the canonical
    # text and nothing else.
    assert hashing.hash_canonical("{}").hex() == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert hashlib.sha256(b"{}").digest() == hashing.hash_canonical("{}")


def test_observe_json_rejects_non_json_and_lone_surrogates() -> None:
    assert hashing.observe_json(TOOL, "not json") is None
    assert hashing.observe_json(TOOL, '{"name":"t"}') is not None
    # A lone surrogate cannot be UTF-8: refuse to hash rather than produce other bytes.
    assert hashing.observe(TOOL, {"name": "t", "description": "\ud800"}) is None


def test_an_integer_a_double_cannot_hold_is_unhashable_not_fatal() -> None:
    """M3: JSON accepts any number of digits; a 310-digit integer is not a double."""
    huge = 10**310
    with pytest.raises(hashing.UnhashableDefinition):
        hashing.canonicalize(huge)
    assert hashing.observe(TOOL, {"name": "t", "inputSchema": huge}) is None
    assert hashing.observe_json(TOOL, '{"name":"t","inputSchema":' + "9" * 310 + "}") is None
    # The largest integer a double holds still rounds like ECMAScript, rather than raising.
    assert hashing.canonicalize(int(1.7976931348623157e308)) == "1.7976931348623157e+308"

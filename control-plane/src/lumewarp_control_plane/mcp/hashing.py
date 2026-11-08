"""MCP definition hashes: RFC 8785 (JCS) canonical JSON, then sha256.

`docs/plugins.md` ("Definition hash") fixes what is hashed: a fixed list of keys per
definition kind, every other key the upstream sent (`_meta`, `icons`, vendor extensions)
dropped, and a key the upstream omitted left out rather than filled in. The gateway hashes
the same bytes in `gateway/crates/lumewarp-gateway/src/mcp_hash.rs`, so a definition the
gateway withheld and reported must hash identically here: that is why this module
implements JCS instead of using `json.dumps`.

JCS in three rules:

- object keys sorted by UTF-16 code units (a non-BMP character sorts by its surrogate
  pair, before U+E000..U+FFFF, while UTF-8 byte order would put it last);
- strings escaped like ECMAScript `JSON.stringify` (quote, backslash and U+0000..U+001F;
  everything else, including non-BMP characters and U+2028/U+2029, as UTF-8);
- numbers printed by ECMAScript `Number::toString`: Python's `repr` already produces the
  shortest digit string that reads back as the same double (ties included), so only the
  layout rules differ (`1e+21`, `1e-7`, `0.000001`, `100000000000000000000`), and JSON
  numbers are doubles, so an integer beyond 2**53 is rounded first.

The vectors in the tests are the gateway's, so a change here fails there too.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from lumewarp_control_plane.models import McpDefinitionKind

# Keys kept when hashing a tool definition.
TOOL_KEYS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
# Keys kept when hashing a prompt definition.
PROMPT_KEYS = ("name", "title", "description", "arguments")
# The gateway's bound, so a name that hashes here is one the gateway would also serve.
MAX_NAME_BYTES = 256
# Largest integer an IEEE 754 double represents exactly (2**53).
MAX_EXACT_INT = 2**53

KEYS: dict[McpDefinitionKind, tuple[str, ...]] = {
    McpDefinitionKind.TOOL: TOOL_KEYS,
    McpDefinitionKind.PROMPT: PROMPT_KEYS,
}


class UnhashableDefinition(ValueError):
    """The value cannot be canonicalised as JCS (not finite, lone surrogate, too deep)."""


@dataclass(frozen=True)
class Observed:
    """One definition projected, canonicalised and hashed."""

    name: str
    # JCS text of the projection: exactly what is stored and what a report carries.
    canonical_json: str
    # sha256(canonical_json), 32 bytes.
    hash: bytes


def keys(kind: McpDefinitionKind | None) -> tuple[str, ...] | None:
    """The keys hashed for `kind`; None for an unknown kind."""
    return KEYS.get(kind) if kind is not None else None


def project(kind: McpDefinitionKind | None, definition: object) -> dict[str, object] | None:
    """The keys `kind` hashes, in a dict, or None when the definition is unusable."""
    kept = keys(kind)
    if kept is None or not isinstance(definition, dict):
        return None
    projection = {key: definition[key] for key in kept if key in definition}
    if definition_name(projection) is None:
        return None
    return projection


def definition_name(definition: dict[str, object]) -> str | None:
    """The `name` of a projected definition, if it has a usable one."""
    name = definition.get("name")
    if not isinstance(name, str) or not name:
        return None
    if len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        return None
    return name


def observe(kind: McpDefinitionKind | None, definition: object) -> Observed | None:
    """Projection plus canonical text plus hash, in one step; None when unusable.

    A definition comes from an untrusted upstream: nothing in it may raise here. Beyond
    `UnhashableDefinition`, a number too large for a double (`OverflowError`) and a string
    JSON accepted but UTF-8 cannot (`UnicodeError`) are both "cannot be hashed".
    """
    projection = project(kind, definition)
    if projection is None:
        return None
    name = definition_name(projection)
    assert name is not None
    try:
        canonical_json = canonicalize(projection)
    except (UnhashableDefinition, OverflowError, UnicodeError):
        return None
    return Observed(
        name=name,
        canonical_json=canonical_json,
        hash=hash_canonical(canonical_json),
    )


def observe_json(kind: McpDefinitionKind | None, text: str) -> Observed | None:
    """`observe` on definition JSON text; None when it is not JSON or cannot be hashed."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return observe(kind, value)


def hash_canonical(text: str) -> bytes:
    """sha256 of an already canonical text."""
    return hashlib.sha256(text.encode("utf-8")).digest()


def canonicalize(value: object) -> str:
    """RFC 8785 canonical JSON text of `value`."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def _write(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        _write_string(value, out)
    elif isinstance(value, int):
        _write_int(value, out)
    elif isinstance(value, float):
        _write_float(value, out)
    elif isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if not isinstance(key, str):
                raise UnhashableDefinition("object keys must be strings")
            if index:
                out.append(",")
            _write_string(key, out)
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise UnhashableDefinition(f"{type(value).__name__} is not JSON")


def _utf16_key(key: object) -> bytes:
    """Sort key: UTF-16 code units, big-endian, which orders like the code unit sequence."""
    if not isinstance(key, str):
        raise UnhashableDefinition("object keys must be strings")
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:  # a lone surrogate cannot be hashed
        raise UnhashableDefinition("string contains a lone surrogate") from exc


def _write_string(text: str, out: list[str]) -> None:
    """ECMAScript `JSON.stringify` escaping; lone surrogates cannot be UTF-8."""
    out.append('"')
    for char in text:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\f":
            out.append("\\f")
        elif char == "\r":
            out.append("\\r")
        elif "\ud800" <= char <= "\udfff":
            raise UnhashableDefinition("string contains a lone surrogate")
        elif char < "\x20":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')


def _write_int(value: int, out: list[str]) -> None:
    """A double holds 2**53 exactly; a bigger integer is rounded like ECMAScript does."""
    if abs(value) <= MAX_EXACT_INT:
        out.append(str(value))
        return
    try:
        rounded = float(value)
    except OverflowError:
        # An integer JSON accepts (any number of digits) but no double can hold.
        raise UnhashableDefinition("integer is too large for a double") from None
    _write_float(rounded, out)


def _write_float(value: float, out: list[str]) -> None:
    """ECMAScript `Number::toString` (ES2015 7.1.12.1) for a finite double."""
    if not math.isfinite(value):
        raise UnhashableDefinition("JSON cannot hold NaN or infinity")
    if value == 0:
        out.append("0")
        return
    sign = "-" if value < 0 else ""
    digits, n = _shortest_digits(abs(value))
    k = len(digits)
    if k <= n <= 21:
        out.append(sign + digits + "0" * (n - k))
    elif 0 < n <= 21:
        out.append(sign + digits[:n] + "." + digits[n:])
    elif -6 < n <= 0:
        out.append(sign + "0." + "0" * -n + digits)
    else:
        exponent = n - 1
        mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
        out.append(sign + mantissa + "e" + ("+" if exponent >= 0 else "") + str(exponent))


def _shortest_digits(value: float) -> tuple[str, int]:
    """The shortest digit string that reads back as `value`, and the decimal exponent.

    `repr` is the shortest round-tripping form (CPython's dtoa, correctly rounded, ties to
    the closest decimal), written either plainly or as `d[.ddd]e±k`. The result is
    `0.<digits> * 10**n`, with trailing zeros removed so `k` is minimal (JCS step 5).
    """
    text = repr(value)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    whole, _, fraction = mantissa.partition(".")
    full = whole + fraction
    stripped = full.lstrip("0")
    leading = len(full) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    n = exponent + len(whole) - leading
    return digits, n

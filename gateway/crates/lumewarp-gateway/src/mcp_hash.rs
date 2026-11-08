//! MCP definition hashes: RFC 8785 (JCS) canonical JSON of a projection of a
//! tool or prompt definition, then SHA-256.
//!
//! `docs/plugins.md` ("Definition hash") fixes what is hashed: a fixed list of
//! keys per definition kind, with every other key the upstream sent (`_meta`,
//! `icons`, vendor extensions) dropped, and a key the upstream omitted left out
//! rather than filled in. The gateway never serves a definition from the
//! snapshot, so the same bytes must hash equally here and in the control plane;
//! that is why this module implements JCS instead of relying on `serde_json`'s
//! own formatting (`to_string` preserves the order a value was built in, does
//! not sort keys and prints floats with Rust's rules).
//!
//! JCS in three rules: object keys sorted by UTF-16 code units, strings escaped
//! like ECMAScript `JSON.stringify` (quote, backslash and U+0000..U+001F, every
//! other character as UTF-8), numbers printed by ECMAScript `Number::toString`
//! (for example `1e+21`, `1e-7`, `0.002`, `-0` → `0`). Objects and arrays keep
//! no whitespace, and duplicate keys cannot exist because the input is a
//! `serde_json::Value`.

// The MCP/A2A request path (W2) wires these modules; until then most of the
// API has no caller in the binary, which `-D warnings` would reject.
#![allow(dead_code)]
use lumewarp_proto::config::v1::McpDefinitionKind;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// Keys kept when hashing a tool definition.
pub const TOOL_KEYS: [&str; 6] = [
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "annotations",
];

/// Keys kept when hashing a prompt definition.
pub const PROMPT_KEYS: [&str; 4] = ["name", "title", "description", "arguments"];

/// Longest definition name the gateway tracks. MCP says 1..=128 characters;
/// the bound here only exists to keep names (and their metric labels) small.
pub const MAX_NAME_BYTES: usize = 256;

/// Largest integer that an IEEE 754 double represents exactly (2^53).
const MAX_EXACT_INT: u64 = 9_007_199_254_740_992;

/// The keys hashed for `kind`; `None` for `MCP_DEFINITION_KIND_UNSPECIFIED`.
pub fn keys(kind: McpDefinitionKind) -> Option<&'static [&'static str]> {
    match kind {
        McpDefinitionKind::Tool => Some(&TOOL_KEYS),
        McpDefinitionKind::Prompt => Some(&PROMPT_KEYS),
        McpDefinitionKind::Unspecified => None,
    }
}

/// One definition projected, canonicalized and hashed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observed {
    /// Upstream name, without any `<server>__` prefix.
    pub name: String,
    /// JCS text of the projection: what a `CatalogReport` carries.
    pub canonical_json: String,
    /// `sha256(canonical_json)`, 32 bytes.
    pub hash: [u8; 32],
}

/// Projects `definition` to the keys `kind` hashes and canonicalizes it.
/// `None` when the kind is unspecified, the value is not a JSON object, or it
/// has no usable `name` — such a definition cannot be approved or reported.
pub fn project(kind: McpDefinitionKind, definition: &Value) -> Option<Map<String, Value>> {
    let kept = keys(kind)?;
    let object = definition.as_object()?;
    let mut projection = Map::new();
    for key in kept {
        if let Some(value) = object.get(*key) {
            projection.insert((*key).to_owned(), value.clone());
        }
    }
    definition_name(&projection)?;
    Some(projection)
}

/// The `name` of a projected definition, if it has a usable one.
pub fn definition_name(definition: &Map<String, Value>) -> Option<&str> {
    let name = definition.get("name")?.as_str()?;
    if name.is_empty() || name.len() > MAX_NAME_BYTES {
        return None;
    }
    Some(name)
}

/// Projection plus canonical text plus hash, in one step.
pub fn observe(kind: McpDefinitionKind, definition: &Value) -> Option<Observed> {
    let projection = project(kind, definition)?;
    let name = definition_name(&projection)?.to_owned();
    let canonical_json = canonicalize(&Value::Object(projection));
    let hash = hash_canonical(&canonical_json);
    Some(Observed {
        name,
        canonical_json,
        hash,
    })
}

/// `sha256(text)`, the hash of an already canonical definition.
pub fn hash_canonical(text: &str) -> [u8; 32] {
    Sha256::digest(text.as_bytes()).into()
}

/// RFC 8785 canonical JSON text of `value`.
pub fn canonicalize(value: &Value) -> String {
    let mut out = String::with_capacity(64);
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => write_number(number, out),
        Value::String(text) => write_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(object) => {
            // JCS sorts by UTF-16 code units, not by code points: a character
            // above U+FFFF (a surrogate pair, 0xD800..) sorts before
            // U+E000..U+FFFF, while UTF-8 byte order would put it last.
            let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write_value(&object[key], out);
            }
            out.push('}');
        }
    }
}

/// ECMAScript `JSON.stringify` string escaping: quote, backslash and the C0
/// controls are escaped (`\b \t \n \f \r`, otherwise `\u00xx`); everything
/// else, including non-BMP characters and U+2028/U+2029, is emitted as UTF-8.
fn write_string(text: &str, out: &mut String) {
    use std::fmt::Write as _;
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{09}' => out.push_str("\\t"),
            '\u{0a}' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\u{0d}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_number(number: &serde_json::Number, out: &mut String) {
    if let Some(value) = number.as_u64() {
        return write_integer(value, out);
    }
    if let Some(value) = number.as_i64() {
        if value >= 0 {
            return write_integer(value as u64, out);
        }
        out.push('-');
        return write_integer(value.unsigned_abs(), out);
    }
    // `serde_json` only holds finite f64 values; NaN and the infinities cannot
    // be constructed through `Number::from_f64`, so there is nothing to reject.
    if let Some(value) = number.as_f64() {
        write_float(value, out);
    }
}

/// Integers an IEEE 754 double represents exactly keep their digits; anything
/// larger is rounded to a double first, because JCS defines JSON numbers as
/// doubles (RFC 8785 3.2.2.3: 9007199254740993 serializes as 9007199254740992).
fn write_integer(value: u64, out: &mut String) {
    if value <= MAX_EXACT_INT {
        out.push_str(itoa(value).as_str());
    } else {
        write_float(value as f64, out);
    }
}

fn itoa(value: u64) -> String {
    value.to_string()
}

/// The digits of the ECMAScript form of `value` (positive): the shortest digit
/// string that reads back as `value`, and among those with that length the one
/// closest to `value`, ties to even (ES2015 7.1.12.1 step 5).
///
/// Rust's shortest float formatter resolves a tie by rounding away from zero,
/// so a value exactly halfway between two k-digit decimals (1424953923781206.25,
/// RFC 8785 Appendix B) would print `...63` instead of `...62`. Ties need the
/// spacing of k-digit decimals to be smaller than one ULP, which only happens
/// for k >= 16, so shorter forms take the fast path unchanged. When the form is
/// long enough, Rust's fixed-precision formatter (exact, half to even) gives the
/// candidate ES6 wants; it is used only when it still reads back as `value`.
fn es6_digits(value: f64, shortest: &str, k: usize) -> String {
    if k < 16 {
        return shortest.to_owned();
    }
    let rounded = format!("{:.*e}", k - 1, value);
    let (mantissa, _) = rounded
        .split_once('e')
        .expect("LowerExp always writes an exponent");
    let candidate: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if candidate.len() != k || candidate == shortest {
        return shortest.to_owned();
    }
    match rounded.parse::<f64>() {
        Ok(parsed) if parsed.to_bits() == value.to_bits() => candidate,
        _ => shortest.to_owned(),
    }
}

/// ECMAScript `Number::toString` (ES2015 7.1.12.1) for a finite double.
fn write_float(value: f64, out: &mut String) {
    use std::fmt::Write as _;
    // `-0` and `0` both serialize as `0`.
    if value == 0.0 {
        out.push('0');
        return;
    }
    if !value.is_finite() {
        // Unreachable through serde_json; never emit invalid JSON regardless.
        out.push_str("null");
        return;
    }
    if value < 0.0 {
        out.push('-');
    }
    // Rust's `{:e}` produces the shortest digit string that round-trips, in
    // scientific notation with a normalized mantissa (`d[.ddd]e±k`), which is
    // exactly the (k, n, s) triple the ECMAScript algorithm needs.
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("LowerExp always writes an exponent");
    let exponent: i32 = exponent
        .parse()
        .expect("LowerExp writes an integer exponent");
    let digits: Vec<u8> = mantissa.bytes().filter(|b| b.is_ascii_digit()).collect();
    debug_assert!(!digits.is_empty());
    let k = digits.len() as i32;
    // n is the position of the decimal point: value = 0.<digits> * 10^n.
    let n = exponent + 1;
    let shortest = std::str::from_utf8(&digits).expect("ASCII digits");
    let digits = es6_digits(value.abs(), shortest, k as usize);
    if k <= n && n <= 21 {
        out.push_str(&digits);
        for _ in k..n {
            out.push('0');
        }
    } else if 0 < n && n <= 21 {
        out.push_str(&digits[..n as usize]);
        out.push('.');
        out.push_str(&digits[n as usize..]);
    } else if -6 < n && n <= 0 {
        out.push_str("0.");
        for _ in n..0 {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        out.push_str(&digits[..1]);
        if k > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        let exponent = n - 1;
        if exponent >= 0 {
            out.push('+');
        }
        let _ = write!(out, "{exponent}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// RFC 8785 Appendix B, transcribed through ECMAScript `String(Number)`:
    /// the bit pattern is the RFC's, the expected text is what the JCS number
    /// rule produces for it.
    #[test]
    fn rfc8785_number_serialization() {
        let cases: &[(u64, &str)] = &[
            (0x0000000000000000, "0"),
            (0x8000000000000000, "0"),
            (0x0000000000000001, "5e-324"),
            (0x8000000000000001, "-5e-324"),
            (0x7fefffffffffffff, "1.7976931348623157e+308"),
            (0xffefffffffffffff, "-1.7976931348623157e+308"),
            (0x4340000000000000, "9007199254740992"),
            (0xc340000000000000, "-9007199254740992"),
            (0x4430000000000000, "295147905179352830000"),
            (0x44b52d02c7e14af5, "9.999999999999997e+22"),
            (0x44b52d02c7e14af6, "1e+23"),
            (0x44b52d02c7e14af7, "1.0000000000000001e+23"),
            (0x444b1ae4d6e2ef4e, "999999999999999700000"),
            (0x444b1ae4d6e2ef4f, "999999999999999900000"),
            (0x444b1ae4d6e2ef50, "1e+21"),
            (0x3eb0c6f7a0b5ed8c, "9.999999999999997e-7"),
            (0x3eb0c6f7a0b5ed8d, "0.000001"),
            (0x41b3de4355555553, "333333333.3333332"),
            (0x41b3de4355555554, "333333333.33333325"),
            (0x41b3de4355555555, "333333333.3333333"),
            (0x41b3de4355555556, "333333333.3333334"),
            (0x41b3de4355555557, "333333333.33333343"),
            (0xbecbf647612f3696, "-0.0000033333333333333333"),
            (0x43143ff3c1cb0959, "1424953923781206.2"),
        ];
        for (bits, expected) in cases {
            assert_eq!(
                canonicalize(&json!(f64::from_bits(*bits))),
                *expected,
                "bits {bits:016x}"
            );
        }
    }

    #[test]
    fn number_boundaries_that_do_not_use_exponents_in_jcs() {
        // ES6 switches to the exponent form at 1e21, not at 1e16 like Python.
        assert_eq!(canonicalize(&json!(1e16)), "10000000000000000");
        assert_eq!(canonicalize(&json!(1e17)), "100000000000000000");
        assert_eq!(canonicalize(&json!(1e20)), "100000000000000000000");
        assert_eq!(canonicalize(&json!(1e21)), "1e+21");
        assert_eq!(canonicalize(&json!(1e30)), "1e+30");
        // And at 1e-6 / 1e-7.
        assert_eq!(canonicalize(&json!(0.000001)), "0.000001");
        assert_eq!(canonicalize(&json!(1e-7)), "1e-7");
        assert_eq!(canonicalize(&json!(2e-3)), "0.002");
        // Integers an IEEE double holds exactly keep their digits.
        assert_eq!(
            canonicalize(&serde_json::from_str::<Value>("9007199254740992").unwrap()),
            "9007199254740992"
        );
        // 2^53 + 1 is not representable: JCS rounds it (RFC 8785 3.2.2.3).
        assert_eq!(
            canonicalize(&serde_json::from_str::<Value>("9007199254740993").unwrap()),
            "9007199254740992"
        );
        assert_eq!(
            canonicalize(&serde_json::from_str::<Value>("1000000000000000000000").unwrap()),
            "1e+21"
        );
        assert_eq!(canonicalize(&json!(-0.0)), "0");
        assert_eq!(canonicalize(&json!(4.50)), "4.5");
        assert_eq!(canonicalize(&json!(-1.5)), "-1.5");
    }

    #[test]
    fn long_forms_stay_shortest_and_closest() {
        // 17 significant digits are the worst case; none of these is a tie, so
        // the exact re-rounding must leave them alone.
        assert_eq!(canonicalize(&json!(0.1 + 0.2)), "0.30000000000000004");
        assert_eq!(canonicalize(&json!(1.0 / 3.0)), "0.3333333333333333");
        // The two smallest subnormal doubles.
        assert_eq!(canonicalize(&json!(f64::from_bits(1))), "5e-324");
        assert_eq!(canonicalize(&json!(f64::from_bits(2))), "1e-323");
        // Exactly one ULP above 1.0.
        assert_eq!(
            canonicalize(&json!(1.0000000000000002)),
            "1.0000000000000002"
        );
        // The RFC tie: the even neighbour wins.
        assert_eq!(
            canonicalize(&json!(f64::from_bits(0x43143ff3c1cb0959))),
            "1424953923781206.2"
        );
    }

    #[test]
    fn string_escaping_follows_ecmascript() {
        let value = json!("\u{20ac}$\u{0f}\nA'B\"\\\\\"/");
        assert_eq!(canonicalize(&value), "\"€$\\u000f\\nA'B\\\"\\\\\\\\\\\"/\"");
        // U+2028/U+2029 are not escaped, non-BMP characters stay as UTF-8.
        assert_eq!(
            canonicalize(&json!("\u{2028}\u{2029}")),
            "\"\u{2028}\u{2029}\""
        );
        assert_eq!(canonicalize(&json!("😀")), "\"😀\"");
        for (c, escaped) in [
            ('\u{0}', "\\u0000"),
            ('\u{1f}', "\\u001f"),
            ('\u{08}', "\\b"),
            ('\u{09}', "\\t"),
            ('\u{0a}', "\\n"),
            ('\u{0c}', "\\f"),
            ('\u{0d}', "\\r"),
        ] {
            assert_eq!(
                canonicalize(&json!(c.to_string())),
                format!("\"{escaped}\"")
            );
        }
    }

    #[test]
    fn keys_sort_by_utf16_code_units() {
        // U+1F600 (surrogate pair D83D DE00) sorts before U+FF61 in UTF-16,
        // while UTF-8 byte order would sort it last.
        let value = json!({"\u{ff61}": 1, "\u{1f600}": 2, "a": 3});
        assert_eq!(canonicalize(&value), "{\"a\":3,\"😀\":2,\"｡\":1}");
        let nested = json!({"b": {"z": 1, "a": 2}, "a": []});
        assert_eq!(canonicalize(&nested), "{\"a\":[],\"b\":{\"a\":2,\"z\":1}}");
    }

    #[test]
    fn rfc8785_whole_document_vector() {
        // RFC 8785 Appendix B's object, as parsed from JSON text.
        let input = r#"{
            "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
            "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
            "literals": [null, true, false]
        }"#;
        let value: Value = serde_json::from_str(input).unwrap();
        assert_eq!(
            canonicalize(&value),
            "{\"literals\":[null,true,false],\"numbers\":[333333333.3333333,1e+30,4.5,0.002,1e-27],\"string\":\"€$\\u000f\\nA'B\\\"\\\\\\\\\\\"/\"}"
        );
    }

    #[test]
    fn tool_projection_drops_every_key_not_listed() {
        let tool = json!({
            "name": "echo",
            "title": "Echo",
            "description": "Echoes text",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "outputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": true},
            "icons": [{"src": "https://example.test/icon.png"}],
            "_meta": {"vendor": {"hidden": true}},
            "x-vendor": 1
        });
        let observed = observe(McpDefinitionKind::Tool, &tool).unwrap();
        assert_eq!(observed.name, "echo");
        assert_eq!(
            observed.canonical_json,
            concat!(
                "{\"annotations\":{\"readOnlyHint\":true},",
                "\"description\":\"Echoes text\",",
                "\"inputSchema\":{\"properties\":{\"text\":{\"type\":\"string\"}},\"type\":\"object\"},",
                "\"name\":\"echo\",",
                "\"outputSchema\":{\"type\":\"object\"},",
                "\"title\":\"Echo\"}"
            )
        );
        assert_eq!(observed.hash, hash_canonical(&observed.canonical_json));
        assert_ne!(observed.hash, [0u8; 32]);
    }

    #[test]
    fn prompt_projection_and_omitted_keys() {
        let prompt = json!({
            "name": "greet",
            "description": "Greets",
            "arguments": [{"name": "who", "required": true}],
            "_meta": {"page": 1}
        });
        let observed = observe(McpDefinitionKind::Prompt, &prompt).unwrap();
        // "title" was omitted upstream, so it is not added as null.
        assert_eq!(
            observed.canonical_json,
            "{\"arguments\":[{\"name\":\"who\",\"required\":true}],\"description\":\"Greets\",\"name\":\"greet\"}"
        );
    }

    #[test]
    fn hash_ignores_key_order_and_unknown_keys_but_not_content() {
        let a: Value = serde_json::from_str(
            r#"{"name":"t","description":"d","inputSchema":{"type":"object"},"_meta":{}}"#,
        )
        .unwrap();
        let b: Value = serde_json::from_str(
            r#"{"inputSchema":{"type":"object"},"description":"d","name":"t"}"#,
        )
        .unwrap();
        let c: Value = serde_json::from_str(
            r#"{"name":"t","description":"changed","inputSchema":{"type":"object"}}"#,
        )
        .unwrap();
        let a = observe(McpDefinitionKind::Tool, &a).unwrap();
        let b = observe(McpDefinitionKind::Tool, &b).unwrap();
        let c = observe(McpDefinitionKind::Tool, &c).unwrap();
        assert_eq!(a, b);
        assert_ne!(a.hash, c.hash);
    }

    #[test]
    fn rejects_definitions_that_cannot_be_approved() {
        // Unspecified kind, non-objects, missing/empty/oversized names.
        assert!(observe(McpDefinitionKind::Unspecified, &json!({"name": "t"})).is_none());
        assert!(observe(McpDefinitionKind::Tool, &json!(["name"])).is_none());
        assert!(observe(McpDefinitionKind::Tool, &json!({"description": "d"})).is_none());
        assert!(observe(McpDefinitionKind::Tool, &json!({"name": ""})).is_none());
        assert!(observe(McpDefinitionKind::Tool, &json!({"name": 7})).is_none());
        let long = "x".repeat(MAX_NAME_BYTES + 1);
        assert!(observe(McpDefinitionKind::Tool, &json!({"name": long})).is_none());
        assert!(keys(McpDefinitionKind::Unspecified).is_none());
        assert_eq!(keys(McpDefinitionKind::Tool), Some(&TOOL_KEYS[..]));
        assert_eq!(keys(McpDefinitionKind::Prompt), Some(&PROMPT_KEYS[..]));
    }

    #[test]
    fn canonical_text_is_stable_across_runs() {
        let tool = json!({"name": "n", "inputSchema": {"type": "object"}});
        let first = observe(McpDefinitionKind::Tool, &tool).unwrap();
        for _ in 0..10 {
            assert_eq!(observe(McpDefinitionKind::Tool, &tool).unwrap(), first);
        }
        // sha256("{}") is a fixed, well-known value: proves the hash input is
        // the canonical text and nothing else.
        assert_eq!(
            hex::encode(hash_canonical("{}")),
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }
}

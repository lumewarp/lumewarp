//! JSON Schema of a strategy's settings: `schemars` output normalized to the
//! subset the control plane accepts and the UI renders.
//!
//! The subset is `string`, `integer`, `number`, `boolean`, `enum`, arrays of
//! strings and one level of nested objects, with `title`, `description`,
//! `default`, `required`, `minimum`, `maximum`, `additionalProperties: false`
//! and `x-lumewarp-widget`. Keep it in step with `compiler/strategy.py`;
//! `testdata/schema-subset.json` is the conformance list of both.

use schemars::{generate::SchemaSettings, JsonSchema};
use serde_json::{json, Map, Value};

/// A larger schema is ignored by the control plane.
pub const MAX_SCHEMA_BYTES: usize = 16 * 1024;

/// The normalized schema of `T`.
pub fn schema_for<T: JsonSchema>() -> Value {
    let generator = SchemaSettings::draft2020_12()
        .with(|settings| {
            settings.inline_subschemas = true;
            settings.meta_schema = None;
        })
        .into_generator();
    let mut schema = generator.into_root_schema_for::<T>().to_value();
    normalize(&mut schema);
    if let Some(root) = schema.as_object_mut() {
        // The Rust type's name, not a label anybody chose.
        root.remove("title");
    }
    schema
}

/// `Option<T>` becomes a plain `T`, which is optional by not being `required`;
/// a known numeric `format` becomes the bounds serde enforces; doc-commented
/// enum variants become a plain `enum`. Any other `format` stays, which leaves
/// the subset: nothing here could validate it, and serde would (`ipv4`).
fn normalize(value: &mut Value) {
    let Some(schema) = value.as_object_mut() else {
        return;
    };
    unwrap_nullable_any_of(schema);
    unify_const_one_of(schema);
    if let Some(Value::Array(types)) = schema.get("type") {
        let types: Vec<_> = types.iter().filter(|t| *t != "null").cloned().collect();
        if let [only] = types.as_slice() {
            schema.insert("type".into(), only.clone());
        }
    }
    if schema.get("default") == Some(&Value::Null) {
        schema.remove("default");
    }
    normalize_format(schema);
    if let Some(Value::Object(properties)) = schema.get_mut("properties") {
        properties.values_mut().for_each(normalize);
    }
    if let Some(items) = schema.get_mut("items") {
        normalize(items);
    }
}

/// `{"anyOf": [X, {"type": "null"}]}` is `X`; the outer keys take precedence.
fn unwrap_nullable_any_of(schema: &mut Map<String, Value>) {
    let Some(Value::Array(variants)) = schema.get("anyOf") else {
        return;
    };
    let null = json!({"type": "null"});
    let rest: Vec<_> = variants.iter().filter(|v| **v != null).cloned().collect();
    if rest.len() != 1 || variants.len() != 2 {
        return;
    }
    schema.remove("anyOf");
    if let Value::Object(inner) = rest.into_iter().next().expect("one variant") {
        for (key, value) in inner {
            schema.entry(key).or_insert(value);
        }
    }
}

fn unify_const_one_of(schema: &mut Map<String, Value>) {
    let Some(Value::Array(variants)) = schema.get("oneOf") else {
        return;
    };
    let constants: Option<Vec<Value>> = variants
        .iter()
        .map(|v| (v.get("type")? == "string").then(|| v.get("const").cloned())?)
        .collect();
    if let Some(constants) = constants {
        schema.remove("oneOf");
        schema.insert("type".into(), json!("string"));
        schema.insert("enum".into(), Value::Array(constants));
    }
}

fn normalize_format(schema: &mut Map<String, Value>) {
    let kind = schema.get("type").and_then(Value::as_str);
    let Some(format) = schema.get("format").and_then(Value::as_str) else {
        return;
    };
    let integer = match (kind, format) {
        (Some("number"), "float" | "double") => None,
        (Some("integer"), "uint8") => Some((0, i128::from(u8::MAX))),
        (Some("integer"), "uint16") => Some((0, i128::from(u16::MAX))),
        (Some("integer"), "uint32") => Some((0, i128::from(u32::MAX))),
        (Some("integer"), "uint64" | "uint") => Some((0, i128::from(u64::MAX))),
        (Some("integer"), "int8") => Some((i128::from(i8::MIN), i128::from(i8::MAX))),
        (Some("integer"), "int16") => Some((i128::from(i16::MIN), i128::from(i16::MAX))),
        (Some("integer"), "int32") => Some((i128::from(i32::MIN), i128::from(i32::MAX))),
        (Some("integer"), "int64" | "int") => Some((i128::from(i64::MIN), i128::from(i64::MAX))),
        _ => return,
    };
    schema.remove("format");
    if let Some((minimum, maximum)) = integer {
        narrow_bound(schema, "minimum", minimum, true);
        narrow_bound(schema, "maximum", maximum, false);
    }
}

/// The bound serde enforces, unless an annotation is already tighter: a range
/// annotation replaces the type's bound, and may be wider than the type allows.
fn narrow_bound(schema: &mut Map<String, Value>, keyword: &str, limit: i128, lower: bool) {
    let tighter = schema.get(keyword).is_some_and(|existing| {
        let Some(value) = existing.as_f64() else {
            return false;
        };
        match as_integer(existing) {
            Some(value) if lower => value > limit,
            Some(value) => value < limit,
            None if lower => value > limit as f64,
            None => value < limit as f64,
        }
    });
    if !tighter {
        let limit = i64::try_from(limit)
            .map(Value::from)
            .unwrap_or_else(|_| Value::from(limit as u64));
        schema.insert(keyword.into(), limit);
    }
}

fn as_integer(value: &Value) -> Option<i128> {
    value
        .as_i64()
        .map(i128::from)
        .or_else(|| value.as_u64().map(i128::from))
}

/// `minimum > maximum`: a field no value can satisfy.
fn empty_range(schema: &Map<String, Value>) -> bool {
    let (Some(minimum), Some(maximum)) = (schema.get("minimum"), schema.get("maximum")) else {
        return false;
    };
    match (as_integer(minimum), as_integer(maximum)) {
        (Some(minimum), Some(maximum)) => minimum > maximum,
        _ => minimum.as_f64() > maximum.as_f64(),
    }
}

const COMMON_KEYWORDS: &[&str] = &["type", "title", "description", "default"];

fn text_problem(path: &str, schema: &Map<String, Value>) -> Option<String> {
    ["title", "description"].iter().find_map(|keyword| {
        schema
            .get(*keyword)
            .is_some_and(|text| !text.is_string())
            .then(|| format!("{path}: {keyword} must be a string"))
    })
}

/// Why `schema` is outside the subset, or `None` when it is inside.
pub fn subset_problem(schema: &Value) -> Option<String> {
    if serde_json::to_vec(schema).map_or(true, |text| text.len() > MAX_SCHEMA_BYTES) {
        return Some(format!("schema exceeds {MAX_SCHEMA_BYTES} bytes"));
    }
    object_problem("$", schema, true)
}

fn object_problem(path: &str, schema: &Value, may_nest: bool) -> Option<String> {
    let Some(object) = schema.as_object() else {
        return Some(format!("{path}: not an object schema"));
    };
    if object.get("type") != Some(&json!("object")) {
        return Some(format!("{path}: type must be object"));
    }
    if let Some(key) = object.keys().find(|key| {
        !COMMON_KEYWORDS.contains(&key.as_str())
            && !matches!(
                key.as_str(),
                "properties" | "required" | "additionalProperties"
            )
    }) {
        return Some(format!("{path}: keyword {key:?} is outside the subset"));
    }
    if let Some(problem) = text_problem(path, object) {
        return Some(problem);
    }
    if object
        .get("additionalProperties")
        .is_some_and(|additional| *additional != json!(false))
    {
        return Some(format!("{path}: additionalProperties must be false"));
    }
    let properties = match object.get("properties") {
        None => &Map::new(),
        Some(Value::Object(properties)) => properties,
        Some(_) => return Some(format!("{path}: properties must be an object")),
    };
    if let Some(required) = object.get("required") {
        let names = required.as_array().filter(|names| {
            names
                .iter()
                .all(|name| name.as_str().is_some_and(|n| properties.contains_key(n)))
        });
        if names.is_none() {
            return Some(format!("{path}: required must name declared properties"));
        }
    }
    properties.iter().find_map(|(name, property)| {
        let path = format!("{path}.{name}");
        match property.get("type").and_then(Value::as_str) {
            Some("object") if may_nest => object_problem(&path, property, false),
            Some("object") => Some(format!("{path}: only one level of nesting is allowed")),
            _ => scalar_problem(&path, property),
        }
    })
}

fn scalar_problem(path: &str, schema: &Value) -> Option<String> {
    let Some(object) = schema.as_object() else {
        return Some(format!("{path}: not an object schema"));
    };
    let kind = object.get("type").and_then(Value::as_str);
    let allowed: &[&str] = match kind {
        Some("string") => &["enum"],
        Some("integer" | "number") => &["enum", "minimum", "maximum"],
        Some("boolean") => &[],
        Some("array") => &["items", "x-lumewarp-widget"],
        _ => return Some(format!("{path}: unsupported type")),
    };
    if let Some(key) = object
        .keys()
        .find(|key| !COMMON_KEYWORDS.contains(&key.as_str()) && !allowed.contains(&key.as_str()))
    {
        return Some(format!("{path}: keyword {key:?} is outside the subset"));
    }
    if let Some(problem) = text_problem(path, object) {
        return Some(problem);
    }
    for bound in ["minimum", "maximum"] {
        if object.get(bound).is_some_and(|value| !value.is_number()) {
            return Some(format!("{path}: {bound} must be a number"));
        }
    }
    if empty_range(object) {
        return Some(format!("{path}: minimum is above maximum"));
    }
    if let Some(values) = object.get("enum") {
        let matches_type = |value: &Value| match kind {
            Some("string") => value.is_string(),
            Some("integer") => value.is_i64() || value.is_u64(),
            _ => value.is_number(),
        };
        if !values
            .as_array()
            .is_some_and(|values| !values.is_empty() && values.iter().all(matches_type))
        {
            return Some(format!("{path}: enum values must match the type"));
        }
    }
    if kind == Some("array") {
        if object.get("items") != Some(&json!({"type": "string"})) {
            return Some(format!("{path}: only arrays of strings are allowed"));
        }
        if object
            .get("x-lumewarp-widget")
            .is_some_and(|widget| *widget != json!("header-list"))
        {
            return Some(format!("{path}: unknown x-lumewarp-widget"));
        }
    }
    None
}

/// Panics with the reason when the schema of `T` leaves the subset.
pub fn assert_schema_subset<T: JsonSchema>() {
    let schema = schema_for::<T>();
    if let Some(problem) = subset_problem(&schema) {
        panic!(
            "{} is outside the strategy schema subset (docs/router-strategies.md): {problem}\n{schema:#}",
            std::any::type_name::<T>()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize, JsonSchema)]
    #[serde(rename_all = "snake_case")]
    #[allow(dead_code)]
    enum Mode {
        Fast,
        Slow,
    }

    #[derive(Deserialize, JsonSchema)]
    #[serde(rename_all = "snake_case")]
    #[allow(dead_code)]
    enum Documented {
        /// The first.
        First,
        /// The second.
        Second,
    }

    #[derive(Deserialize, JsonSchema)]
    #[serde(deny_unknown_fields)]
    #[allow(dead_code)]
    struct Nested {
        /// Inner level.
        #[schemars(range(min = 1, max = 5))]
        level: u8,
        label: Option<String>,
    }

    fn three() -> u32 {
        3
    }

    #[derive(Deserialize, JsonSchema)]
    #[serde(deny_unknown_fields)]
    #[allow(dead_code)]
    struct Everything {
        /// Skip members with more waiting requests than this.
        #[schemars(title = "Max waiting requests", range(min = 0))]
        max_waiting: Option<u32>,
        #[serde(default = "three")]
        retries: u32,
        ratio: f64,
        on: bool,
        mode: Mode,
        documented: Option<Documented>,
        tags: Vec<String>,
        nested: Nested,
        optional_nested: Option<Nested>,
        #[schemars(extend("x-lumewarp-widget" = "header-list"))]
        #[serde(default)]
        pass_through: Vec<String>,
    }

    #[test]
    fn schemars_output_converges_on_the_subset() {
        let schema = schema_for::<Everything>();
        assert_eq!(subset_problem(&schema), None, "{schema:#}");
        assert_eq!(schema.get("title"), None);
        assert_eq!(schema["additionalProperties"], json!(false));
        let p = &schema["properties"];
        assert_eq!(
            p["max_waiting"],
            json!({
                "title": "Max waiting requests",
                "description": "Skip members with more waiting requests than this.",
                "type": "integer",
                "minimum": 0,
                "maximum": u32::MAX,
            })
        );
        assert_eq!(p["retries"]["default"], json!(3));
        assert_eq!(p["ratio"], json!({"type": "number"}));
        assert_eq!(
            p["mode"],
            json!({"type": "string", "enum": ["fast", "slow"]})
        );
        assert_eq!(
            p["documented"],
            json!({"type": "string", "enum": ["first", "second"]})
        );
        assert_eq!(
            p["tags"],
            json!({"type": "array", "items": {"type": "string"}})
        );
        assert_eq!(p["nested"]["properties"]["level"]["maximum"], json!(5));
        assert_eq!(p["optional_nested"]["type"], json!("object"));
        assert_eq!(p["pass_through"]["x-lumewarp-widget"], json!("header-list"));
        let required = schema["required"].as_array().unwrap();
        assert!(required.contains(&json!("ratio")));
        assert!(!required.contains(&json!("max_waiting")));
        assert!(!required.contains(&json!("retries")));
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Numbers {
        numbers: Vec<u32>,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct TooDeep {
        outer: Nesting,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Nesting {
        inner: Nested,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Pattern {
        #[schemars(regex(pattern = "^a+$"))]
        name: String,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Tagged {
        variant: Variant,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    enum Variant {
        Number(u32),
        Text(String),
    }

    #[test]
    #[should_panic(expected = "only arrays of strings")]
    fn array_of_numbers_fails_the_helper() {
        assert_schema_subset::<Numbers>();
    }

    #[test]
    #[should_panic(expected = "one level of nesting")]
    fn deep_nesting_fails_the_helper() {
        assert_schema_subset::<TooDeep>();
    }

    #[test]
    #[should_panic(expected = "\"pattern\" is outside")]
    fn a_regex_fails_the_helper() {
        assert_schema_subset::<Pattern>();
    }

    #[test]
    #[should_panic(expected = "outside the strategy schema subset")]
    fn a_data_enum_fails_the_helper() {
        assert_schema_subset::<Tagged>();
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Address {
        ip: std::net::Ipv4Addr,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct OptionalAddress {
        ip: Option<std::net::IpAddr>,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Addresses {
        ips: Vec<std::net::Ipv4Addr>,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Unknown {
        #[schemars(extend("format" = "made-up"))]
        name: String,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct FloatFormatOnAnInteger {
        #[schemars(extend("format" = "double"))]
        n: i32,
    }

    #[test]
    #[should_panic(expected = "\"format\" is outside")]
    fn ipv4_fails_the_helper() {
        assert_schema_subset::<Address>();
    }

    #[test]
    #[should_panic(expected = "\"format\" is outside")]
    fn optional_ip_fails_the_helper() {
        assert_schema_subset::<OptionalAddress>();
    }

    #[test]
    #[should_panic(expected = "only arrays of strings")]
    fn a_list_of_addresses_fails_the_helper() {
        assert_schema_subset::<Addresses>();
    }

    #[test]
    #[should_panic(expected = "\"format\" is outside")]
    fn an_unknown_format_fails_the_helper() {
        assert_schema_subset::<Unknown>();
    }

    #[test]
    #[should_panic(expected = "\"format\" is outside")]
    fn a_format_on_the_wrong_type_fails_the_helper() {
        assert_schema_subset::<FloatFormatOnAnInteger>();
    }

    /// `#[schemars(range)]` replaces a type's bound rather than narrowing it.
    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Annotated {
        #[schemars(range(max = 1000))]
        wide_max: u8,
        #[schemars(range(min = -5))]
        wide_min: u8,
        #[schemars(range(min = 2, max = 7))]
        narrow: u8,
        #[schemars(range(min = 0.5))]
        fractional: u8,
        #[schemars(range(max = 300))]
        optional: Option<u8>,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Impossible {
        #[schemars(range(min = 300))]
        n: u8,
    }

    #[derive(Deserialize, JsonSchema)]
    #[allow(dead_code)]
    struct Inverted {
        #[schemars(range(min = 9, max = 3))]
        n: u32,
    }

    #[test]
    fn annotations_cannot_widen_what_the_type_accepts() {
        let schema = schema_for::<Annotated>();
        assert_eq!(subset_problem(&schema), None, "{schema:#}");
        let bounds = |name: &str| {
            let field = &schema["properties"][name];
            (field["minimum"].clone(), field["maximum"].clone())
        };
        assert_eq!(bounds("wide_max"), (json!(0), json!(255)));
        assert_eq!(bounds("wide_min"), (json!(0), json!(255)));
        assert_eq!(bounds("narrow"), (json!(2), json!(7)));
        assert_eq!(bounds("fractional"), (json!(0.5), json!(255)));
        assert_eq!(bounds("optional"), (json!(0), json!(255)));
    }

    #[test]
    #[should_panic(expected = "minimum is above maximum")]
    fn a_range_outside_the_type_fails_the_helper() {
        assert_schema_subset::<Impossible>();
    }

    #[test]
    #[should_panic(expected = "minimum is above maximum")]
    fn an_inverted_range_fails_the_helper() {
        assert_schema_subset::<Inverted>();
    }

    /// What the control plane's validator decides for a number, from the schema.
    fn schema_accepts(schema: &Value, value: i128) -> bool {
        let above = schema["minimum"]
            .as_f64()
            .is_none_or(|_| as_integer(&schema["minimum"]).is_none_or(|min| value >= min));
        let below = schema["maximum"]
            .as_f64()
            .is_none_or(|_| as_integer(&schema["maximum"]).is_none_or(|max| value <= max));
        above && below
    }

    fn serde_accepts<T: serde::de::DeserializeOwned>(value: i128) -> bool {
        serde_json::from_str::<T>(&format!("{{\"n\":{value}}}")).is_ok()
    }

    macro_rules! boundaries_agree {
        ($($int:ident),* $(,)?) => {$({
            #[derive(Deserialize, JsonSchema)]
            #[serde(deny_unknown_fields)]
            #[allow(dead_code)]
            struct Plain {
                n: $int,
            }
            #[derive(Deserialize, JsonSchema)]
            #[serde(deny_unknown_fields)]
            #[allow(dead_code)]
            struct WiderAnnotation {
                #[schemars(range(min = -100000, max = 100000))]
                n: $int,
            }
            let (min, max) = (i128::from($int::MIN), i128::from($int::MAX));
            let plain = schema_for::<Plain>();
            assert_eq!(subset_problem(&plain), None, "{plain:#}");
            let plain = &plain["properties"]["n"];
            for value in [min - 1, min, min + 1, 0, max - 1, max, max + 1] {
                assert_eq!(
                    schema_accepts(plain, value),
                    serde_accepts::<Plain>(value),
                    "{} at {value}: {plain}",
                    stringify!($int),
                );
            }
            let wider = schema_for::<WiderAnnotation>();
            if subset_problem(&wider).is_none() {
                let wider = &wider["properties"]["n"];
                for value in [-100001, -100000, -1, 0, 255, 256, 65536, 100000, 100001, min, max] {
                    // The annotation only narrows, and never admits what serde refuses.
                    assert!(
                        !schema_accepts(wider, value) || serde_accepts::<WiderAnnotation>(value),
                        "{} at {value}: {wider}",
                        stringify!($int),
                    );
                }
            }
        })*};
    }

    #[test]
    fn schema_and_deserialize_agree_at_every_integer_boundary() {
        boundaries_agree!(u8, u16, u32, u64, i8, i16, i32, i64);
    }

    #[test]
    fn a_float_is_a_plain_number_that_serde_accepts() {
        #[derive(Deserialize, JsonSchema)]
        #[allow(dead_code)]
        struct Floats {
            single: f32,
            double: f64,
        }
        let schema = schema_for::<Floats>();
        assert_eq!(schema["properties"]["single"], json!({"type": "number"}));
        assert_eq!(schema["properties"]["double"], json!({"type": "number"}));
        assert!(serde_json::from_str::<Floats>(r#"{"single":1,"double":1.5}"#).is_ok());
    }

    #[test]
    fn conformance_list_shared_with_the_control_plane() {
        let cases: Value =
            serde_json::from_str(include_str!("../testdata/schema-subset.json")).unwrap();
        for schema in cases["inside"].as_array().unwrap() {
            assert_eq!(subset_problem(schema), None, "{schema}");
        }
        for schema in cases["outside"].as_array().unwrap() {
            assert!(subset_problem(schema).is_some(), "{schema}");
        }
    }
}

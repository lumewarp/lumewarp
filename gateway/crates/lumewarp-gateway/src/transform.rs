//! Per-model request body changes: `default_body_json`, `reasoning_effort`
//! and the `max_output_tokens` bound (snapshot `Model`).
//!
//! Only a model that configures one of them gets a `Transform`; every other
//! model keeps the plain splice in `proxy.rs`. The body is read as a list of
//! top-level `(key, raw value)` pairs in their original order, so values the
//! gateway does not change keep their exact bytes (numbers, escapes,
//! unknown fields). Objects present on both sides of a merge are re-encoded
//! the same way, one level at a time.
//!
//! Precedence, lowest first: `default_body_json`, the reasoning fragment for
//! the requested level, the client's own fields.

use std::{borrow::Cow, collections::HashMap, fmt};

use bytes::Bytes;
use lumewarp_proto::config::v1 as pb;
use serde::de::{Deserializer, MapAccess, Visitor};
use serde_json::{value::RawValue, Map, Value};

/// Keys the gateway owns; a default body or a fragment may not set them.
pub const RESERVED_KEYS: [&str; 3] = ["model", "stream", "stream_options"];
pub const REASONING_LEVELS: [&str; 6] = ["none", "minimal", "low", "medium", "high", "xhigh"];
const MAX_JSON_BYTES: usize = 16 * 1024;
/// Object/array nesting allowed in a default body or fragment (the control plane checks
/// the same bound, well inside serde_json's recursion limit).
pub const MAX_JSON_DEPTH: usize = 32;
const REASONING_FIELD: &str = "reasoning_effort";
const OUTPUT_FIELDS: [&str; 2] = ["max_tokens", "max_completion_tokens"];

#[derive(Debug)]
enum Reasoning {
    Passthrough,
    Strip,
    Mapped(HashMap<String, Map<String, Value>>),
}

#[derive(Debug)]
pub struct Transform {
    max_output_tokens: Option<u64>,
    default_body: Map<String, Value>,
    reasoning: Reasoning,
    default_level: Option<String>,
}

/// Why the request was refused (400, OpenAI `invalid_request_error`).
#[derive(Debug, PartialEq, Eq)]
pub struct Rejected {
    pub reason: &'static str,
    pub message: String,
}

/// Nesting depth of objects and arrays in JSON text (strings skipped); a scan, so text
/// too deep for serde_json still gets a clear error.
fn json_depth(text: &str) -> usize {
    let (mut depth, mut max, mut in_string, mut escaped) = (0usize, 0usize, false, false);
    for b in text.bytes() {
        if in_string {
            match (escaped, b) {
                (true, _) => escaped = false,
                (false, b'\\') => escaped = true,
                (false, b'"') => in_string = false,
                _ => {}
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'{' | b'[' => {
                depth += 1;
                max = max.max(depth);
            }
            b'}' | b']' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    max
}

fn object(text: &str, what: &str, reserved: &[&str]) -> Result<Map<String, Value>, String> {
    if text.len() > MAX_JSON_BYTES {
        return Err(format!("{what} exceeds 16 KiB"));
    }
    if json_depth(text) > MAX_JSON_DEPTH {
        return Err(format!("{what} nests deeper than {MAX_JSON_DEPTH} levels"));
    }
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(text) else {
        return Err(format!("{what} must be a JSON object"));
    };
    if let Some(key) = reserved.iter().find(|k| map.contains_key(**k)) {
        return Err(format!("{what} must not set {key:?}"));
    }
    Ok(map)
}

impl Transform {
    /// `None` when the model changes nothing (the common case).
    pub fn compile(model: &pb::Model) -> Result<Option<Self>, String> {
        let default_body = if model.default_body_json.is_empty() {
            Map::new()
        } else {
            // reasoning_effort has its own setting (default_level), which every mode honours.
            let mut reserved = RESERVED_KEYS.to_vec();
            reserved.push(REASONING_FIELD);
            object(&model.default_body_json, "default_body_json", &reserved)?
        };
        let (reasoning, default_level) = match &model.reasoning_effort {
            None => (Reasoning::Passthrough, None),
            Some(r) => {
                let level = (!r.default_level.is_empty()).then(|| r.default_level.clone());
                if let Some(level) = &level {
                    if !REASONING_LEVELS.contains(&level.as_str()) {
                        return Err(format!("unknown reasoning default_level {level:?}"));
                    }
                }
                let mode = pb::ReasoningEffortMode::try_from(r.mode)
                    .map_err(|_| "unknown reasoning_effort mode".to_string())?;
                let reasoning = match mode {
                    pb::ReasoningEffortMode::Unspecified | pb::ReasoningEffortMode::Passthrough => {
                        if !r.fragments_json.is_empty() {
                            return Err("reasoning fragments need mode MAPPED".into());
                        }
                        Reasoning::Passthrough
                    }
                    pb::ReasoningEffortMode::Strip => {
                        if !r.fragments_json.is_empty() || level.is_some() {
                            return Err("reasoning mode STRIP takes no fragments or level".into());
                        }
                        Reasoning::Strip
                    }
                    pb::ReasoningEffortMode::Mapped => {
                        if r.fragments_json.is_empty() {
                            return Err("reasoning mode MAPPED needs fragments".into());
                        }
                        let mut fragments = HashMap::new();
                        for (key, text) in &r.fragments_json {
                            if !REASONING_LEVELS.contains(&key.as_str()) {
                                return Err(format!("unknown reasoning level {key:?}"));
                            }
                            fragments.insert(
                                key.clone(),
                                object(text, "reasoning fragment", &RESERVED_KEYS)?,
                            );
                        }
                        if let Some(level) = &level {
                            if !fragments.contains_key(level) {
                                return Err("reasoning default_level has no fragment".into());
                            }
                        }
                        Reasoning::Mapped(fragments)
                    }
                };
                (reasoning, level)
            }
        };
        let transform = Self {
            max_output_tokens: (model.max_output_tokens > 0)
                .then_some(model.max_output_tokens.into()),
            default_body,
            reasoning,
            default_level,
        };
        let noop = transform.max_output_tokens.is_none()
            && transform.default_body.is_empty()
            && matches!(transform.reasoning, Reasoning::Passthrough)
            && transform.default_level.is_none();
        Ok((!noop).then_some(transform))
    }

    /// Whether the body must be rewritten (not only checked).
    fn rewrites(&self) -> bool {
        !self.default_body.is_empty()
            || !matches!(self.reasoning, Reasoning::Passthrough)
            || self.default_level.is_some()
    }

    /// `body` is the already spliced request (model, stream_options).
    pub fn apply(&self, body: Bytes) -> Result<Bytes, Rejected> {
        let text =
            std::str::from_utf8(&body).map_err(|_| invalid("Invalid JSON request object"))?;
        let mut entries = Entries::parse(text)?;
        if self.rewrites() {
            let mut defaults = self.default_body.clone();
            match &self.reasoning {
                // The client's value goes through as sent, whatever it is.
                Reasoning::Passthrough => {
                    if let Some(level) = &self.default_level {
                        if entries
                            .get(REASONING_FIELD)
                            .is_none_or(|v| v.trim() == "null")
                        {
                            entries.remove(REASONING_FIELD);
                            defaults.insert(REASONING_FIELD.into(), Value::String(level.clone()));
                        }
                    }
                }
                Reasoning::Strip => entries.remove(REASONING_FIELD),
                Reasoning::Mapped(fragments) => {
                    let level = entries.reasoning_level()?.or(self.default_level.as_deref());
                    entries.remove(REASONING_FIELD);
                    if let Some(level) = level {
                        let Some(fragment) = fragments.get(level) else {
                            return Err(Rejected {
                                reason: "reasoning_effort",
                                message: format!(
                                    "reasoning_effort {level:?} is not supported by this model"
                                ),
                            });
                        };
                        merge_values(&mut defaults, fragment);
                    }
                }
            }
            entries.merge_defaults(&defaults)?;
        }
        if let Some(limit) = self.max_output_tokens {
            for field in OUTPUT_FIELDS {
                if let Some(raw) = entries.get(field) {
                    if serde_json::from_str::<f64>(raw).is_ok_and(|n| n > limit as f64) {
                        return Err(Rejected {
                            reason: "max_output_tokens",
                            message: format!(
                                "{field} exceeds this model's limit of {limit} output tokens"
                            ),
                        });
                    }
                }
            }
        }
        if !self.rewrites() {
            return Ok(body);
        }
        Ok(Bytes::from(entries.encode().into_owned()))
    }
}

fn invalid(message: &str) -> Rejected {
    Rejected {
        reason: "invalid_body",
        message: message.into(),
    }
}

/// `over` wins; objects on both sides merge recursively.
fn merge_values(base: &mut Map<String, Value>, over: &Map<String, Value>) {
    for (key, value) in over {
        match (base.get_mut(key), value) {
            (Some(Value::Object(b)), Value::Object(o)) => merge_values(b, o),
            _ => {
                base.insert(key.clone(), value.clone());
            }
        }
    }
}

/// Top-level members in order, values as raw JSON text.
struct Entries<'a>(Vec<(String, Cow<'a, str>)>);

struct EntriesVisitor<'a>(std::marker::PhantomData<&'a ()>);

impl<'de: 'a, 'a> Visitor<'de> for EntriesVisitor<'a> {
    type Value = Vec<(String, Cow<'a, str>)>;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a JSON object")
    }
    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut out = Vec::new();
        while let Some((key, value)) = map.next_entry::<String, &'de RawValue>()? {
            out.push((key, Cow::Borrowed(value.get())));
        }
        Ok(out)
    }
}

impl<'a> Entries<'a> {
    fn parse(text: &'a str) -> Result<Self, Rejected> {
        let mut de = serde_json::Deserializer::from_str(text);
        let entries = (&mut de)
            .deserialize_map(EntriesVisitor(std::marker::PhantomData))
            .and_then(|e| de.end().map(|()| e))
            .map_err(|_| invalid("Invalid JSON request object"))?;
        Ok(Self(entries))
    }

    /// The last occurrence wins, as in serde_json and most parsers.
    fn get(&self, key: &str) -> Option<&str> {
        self.0
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_ref())
    }

    fn remove(&mut self, key: &str) {
        self.0.retain(|(k, _)| k != key);
    }

    fn reasoning_level(&self) -> Result<Option<&'static str>, Rejected> {
        let Some(raw) = self.get(REASONING_FIELD) else {
            return Ok(None);
        };
        match serde_json::from_str::<Option<String>>(raw) {
            Ok(None) => Ok(None),
            Ok(Some(level)) => REASONING_LEVELS
                .iter()
                .find(|l| **l == level)
                .map(|l| Some(*l))
                .ok_or_else(|| Rejected {
                    reason: "reasoning_effort",
                    message: format!("unknown reasoning_effort {level:?}"),
                }),
            Err(_) => Err(Rejected {
                reason: "reasoning_effort",
                message: "reasoning_effort must be a string".into(),
            }),
        }
    }

    /// Adds every default the client did not send; objects on both sides merge.
    fn merge_defaults(&mut self, defaults: &Map<String, Value>) -> Result<(), Rejected> {
        for (key, value) in defaults {
            let found = self.0.iter().rposition(|(k, _)| k == key);
            match (found, value) {
                (None, _) => self.0.push((key.clone(), Cow::Owned(value.to_string()))),
                (Some(i), Value::Object(inner)) if self.0[i].1.trim_start().starts_with('{') => {
                    let mut nested = Entries::parse(&self.0[i].1)?;
                    nested.merge_defaults(inner)?;
                    let text = nested.encode().into_owned();
                    self.0[i].1 = Cow::Owned(text);
                }
                (Some(_), _) => {}
            }
        }
        Ok(())
    }

    fn encode(&self) -> Cow<'static, str> {
        let size: usize = self.0.iter().map(|(k, v)| k.len() + v.len() + 4).sum();
        let mut out = String::with_capacity(size + 2);
        out.push('{');
        for (i, (key, value)) in self.0.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&serde_json::to_string(key).expect("strings serialize"));
            out.push(':');
            out.push_str(value);
        }
        out.push('}');
        Cow::Owned(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn model() -> pb::Model {
        pb::Model {
            name: "m".into(),
            upstream: "u".into(),
            upstream_model: "x".into(),
            ..Default::default()
        }
    }

    fn mapped(fragments: &[(&str, &str)], default_level: &str) -> pb::ReasoningEffort {
        pb::ReasoningEffort {
            mode: pb::ReasoningEffortMode::Mapped as i32,
            fragments_json: fragments
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            default_level: default_level.into(),
        }
    }

    fn run(m: &pb::Model, body: &str) -> Result<String, Rejected> {
        let t = Transform::compile(m).unwrap().expect("configured");
        t.apply(Bytes::copy_from_slice(body.as_bytes()))
            .map(|b| String::from_utf8(b.to_vec()).unwrap())
    }

    fn json(text: &str) -> Value {
        serde_json::from_str(text).unwrap()
    }

    #[test]
    fn passthrough_and_strip_never_judge_the_clients_value() {
        let mut m = model();
        m.default_body_json = r#"{"temperature":0.5}"#.into();
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":"future-effort"}"#).unwrap());
        assert_eq!(out["reasoning_effort"], "future-effort");
        m.reasoning_effort = Some(pb::ReasoningEffort {
            mode: pb::ReasoningEffortMode::Strip as i32,
            ..Default::default()
        });
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":7}"#).unwrap());
        assert!(out.get("reasoning_effort").is_none());
        // A default level fills a missing or null value only.
        m.reasoning_effort = Some(pb::ReasoningEffort {
            mode: pb::ReasoningEffortMode::Passthrough as i32,
            default_level: "low".into(),
            ..Default::default()
        });
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":null}"#).unwrap());
        assert_eq!(out["reasoning_effort"], "low");
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":"odd"}"#).unwrap());
        assert_eq!(out["reasoning_effort"], "odd");
    }

    #[test]
    fn default_body_cannot_set_reasoning_effort_or_nest_too_deep() {
        let mut m = model();
        m.default_body_json = r#"{"reasoning_effort":"high"}"#.into();
        assert!(Transform::compile(&m)
            .unwrap_err()
            .contains("reasoning_effort"));
        m.default_body_json = format!("{}1{}", r#"{"a":"#.repeat(33), "}".repeat(33));
        assert!(Transform::compile(&m).unwrap_err().contains("deeper"));
        m.default_body_json = format!("{}1{}", r#"{"a":"#.repeat(32), "}".repeat(32));
        assert!(Transform::compile(&m).is_ok());
        // Brackets inside strings do not count.
        assert_eq!(json_depth(r#"{"a":"[[[{{{\"]]"}"#), 1);
        // A fragment may map a level onto the native field.
        m.default_body_json.clear();
        m.reasoning_effort = Some(mapped(&[("xhigh", r#"{"reasoning_effort":"high"}"#)], ""));
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":"xhigh"}"#).unwrap());
        assert_eq!(out["reasoning_effort"], "high");
    }

    #[test]
    fn plain_models_have_no_transform() {
        assert!(Transform::compile(&model()).unwrap().is_none());
        let mut m = model();
        m.reasoning_effort = Some(pb::ReasoningEffort::default());
        assert!(Transform::compile(&m).unwrap().is_none());
    }

    #[test]
    fn default_body_merges_under_the_client() {
        let mut m = model();
        m.default_body_json =
            r#"{"temperature":0.2,"chat_template_kwargs":{"enable_thinking":false,"x":1},"top_k":20}"#
                .into();
        let out = run(
            &m,
            r#"{"model":"x","temperature":1.0000000000000002,"chat_template_kwargs":{"x":2},"n":12345678901234567890}"#,
        )
        .unwrap();
        // Untouched values keep their exact bytes, in their order.
        assert!(
            out.starts_with(r#"{"model":"x","temperature":1.0000000000000002,"#),
            "{out}"
        );
        assert!(out.contains("12345678901234567890"), "{out}");
        assert_eq!(
            json(&out),
            json(
                r#"{"model":"x","temperature":1.0000000000000002,"chat_template_kwargs":{"x":2,"enable_thinking":false},"n":12345678901234567890,"top_k":20}"#
            )
        );
        // A client value of another type wins whole.
        let out = run(&m, r#"{"model":"x","chat_template_kwargs":null}"#).unwrap();
        assert_eq!(json(&out)["chat_template_kwargs"], Value::Null);
    }

    #[test]
    fn mapped_reasoning_replaces_the_field() {
        let mut m = model();
        m.default_body_json =
            r#"{"chat_template_kwargs":{"enable_thinking":true,"keep":1}}"#.into();
        m.reasoning_effort = Some(mapped(
            &[
                (
                    "none",
                    r#"{"chat_template_kwargs":{"enable_thinking":false}}"#,
                ),
                (
                    "high",
                    r#"{"chat_template_kwargs":{"enable_thinking":true},"top_p":0.9}"#,
                ),
            ],
            "",
        ));
        let out = json(&run(&m, r#"{"model":"x","reasoning_effort":"none"}"#).unwrap());
        assert_eq!(
            out,
            json(r#"{"model":"x","chat_template_kwargs":{"enable_thinking":false,"keep":1}}"#)
        );
        // The client's own field still wins over the fragment.
        let out = json(
            &run(
                &m,
                r#"{"model":"x","reasoning_effort":"high","chat_template_kwargs":{"enable_thinking":false}}"#,
            )
            .unwrap(),
        );
        assert_eq!(out["chat_template_kwargs"]["enable_thinking"], false);
        assert_eq!(out["top_p"], 0.9);
        assert!(out.get("reasoning_effort").is_none());
        // No level and no default: only the default body.
        let out = json(&run(&m, r#"{"model":"x"}"#).unwrap());
        assert_eq!(out["chat_template_kwargs"]["enable_thinking"], true);
        let err = run(&m, r#"{"model":"x","reasoning_effort":"low"}"#).unwrap_err();
        assert_eq!(err.reason, "reasoning_effort");
        let err = run(&m, r#"{"model":"x","reasoning_effort":"huge"}"#).unwrap_err();
        assert!(err.message.contains("unknown"));
        assert!(run(&m, r#"{"model":"x","reasoning_effort":3}"#).is_err());
    }

    #[test]
    fn default_level_and_strip() {
        let mut m = model();
        m.reasoning_effort = Some(mapped(&[("low", r#"{"a":1}"#)], "low"));
        assert_eq!(json(&run(&m, r#"{"model":"x"}"#).unwrap())["a"], 1);
        assert_eq!(
            json(&run(&m, r#"{"model":"x","reasoning_effort":null}"#).unwrap())["a"],
            1
        );
        m.reasoning_effort = Some(pb::ReasoningEffort {
            mode: pb::ReasoningEffortMode::Passthrough as i32,
            default_level: "medium".into(),
            ..Default::default()
        });
        assert_eq!(
            json(&run(&m, r#"{"model":"x"}"#).unwrap())["reasoning_effort"],
            "medium"
        );
        assert_eq!(
            json(&run(&m, r#"{"model":"x","reasoning_effort":"high"}"#).unwrap())
                ["reasoning_effort"],
            "high"
        );
        m.reasoning_effort = Some(pb::ReasoningEffort {
            mode: pb::ReasoningEffortMode::Strip as i32,
            ..Default::default()
        });
        assert_eq!(
            run(&m, r#"{"model":"x","reasoning_effort":"high","n":1}"#).unwrap(),
            r#"{"model":"x","n":1}"#
        );
    }

    #[test]
    fn output_limit_checks_the_final_body_without_rewriting() {
        let mut m = model();
        m.max_output_tokens = 100;
        let body = r#"{ "model" : "x", "max_tokens": 100 }"#;
        // A check-only transform keeps the body byte for byte.
        assert_eq!(run(&m, body).unwrap(), body);
        let err = run(&m, r#"{"model":"x","max_completion_tokens":101}"#).unwrap_err();
        assert_eq!(err.reason, "max_output_tokens");
        assert!(err.message.contains("100"));
        m.default_body_json = r#"{"max_tokens":500}"#.into();
        assert!(run(&m, r#"{"model":"x"}"#).is_err());
        assert!(run(&m, r#"{"model":"x","max_tokens":50}"#).is_ok());
    }

    #[test]
    fn invalid_configs_are_rejected() {
        let mut m = model();
        for bad in [
            r#"[1]"#,
            r#"{"model":"y"}"#,
            r#"{"stream":true}"#,
            "not json",
        ] {
            m.default_body_json = bad.into();
            assert!(Transform::compile(&m).is_err(), "{bad}");
        }
        m.default_body_json.clear();
        for r in [
            mapped(&[], ""),
            mapped(&[("ultra", "{}")], ""),
            mapped(&[("low", "{}")], "high"),
            mapped(&[("low", r#"{"stream_options":{}}"#)], ""),
            pb::ReasoningEffort {
                mode: pb::ReasoningEffortMode::Strip as i32,
                default_level: "low".into(),
                ..Default::default()
            },
            pb::ReasoningEffort {
                mode: 99,
                ..Default::default()
            },
        ] {
            m.reasoning_effort = Some(r.clone());
            assert!(Transform::compile(&m).is_err(), "{r:?}");
        }
    }

    #[test]
    fn invalid_client_bodies_are_400() {
        let mut m = model();
        m.default_body_json = r#"{"a":1}"#.into();
        for bad in [r#"{"model":"x""#, r#"{"model":"x"} trailing"#, r#"[1]"#] {
            assert_eq!(run(&m, bad).unwrap_err().reason, "invalid_body", "{bad}");
        }
    }
}

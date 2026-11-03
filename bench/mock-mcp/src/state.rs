//! Immutable per-instance state: tool definitions, pre-serialized listings and the
//! header annotations derived from each definition.

use crate::model::{
    CacheScope, Era, InstanceConfig, ToolConfig, DEFAULT_TTL_MS, LEGACY_VERSION, MODERN_VERSION,
};
use bytes::Bytes;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HeaderKind {
    String,
    Integer,
    Boolean,
}

#[derive(Clone, Debug)]
pub struct ParamHeader {
    pub path: Vec<String>,
    pub name: String,
    pub kind: HeaderKind,
}

#[derive(Clone, Debug)]
pub struct Tool {
    pub config: ToolConfig,
    pub definition: Value,
    pub param_headers: Vec<ParamHeader>,
}

#[derive(Debug)]
pub struct InstanceState {
    pub config: InstanceConfig,
    pub tools: HashMap<String, Arc<Tool>>,
    pub tool_order: Vec<Arc<Tool>>,
    pub server_info: Value,
    pub modern_list: Bytes,
    pub legacy_list: Bytes,
    pub discover: Bytes,
    pub ttl_ms: u64,
    pub cache_scope: CacheScope,
}

impl InstanceState {
    pub fn build(config: InstanceConfig) -> Result<Self, String> {
        config.validate()?;
        let mut tools = HashMap::new();
        let mut tool_order = Vec::with_capacity(config.tools.len());
        for tool_config in &config.tools {
            let tool = Arc::new(build_tool(tool_config)?);
            tools.insert(tool_config.name.clone(), tool.clone());
            tool_order.push(tool);
        }
        let server_info = json!({
            "name": format!("mock-mcp/{}", config.name),
            "version": env!("CARGO_PKG_VERSION"),
        });
        let modern_list = list_result(&tool_order, &config, Era::Modern, &server_info);
        let legacy_list = list_result(&tool_order, &config, Era::Legacy, &server_info);
        let discover = discover_result(&config, &server_info);
        Ok(Self {
            ttl_ms: config.ttl_ms,
            cache_scope: config.cache_scope,
            config,
            tools,
            tool_order,
            server_info,
            modern_list,
            legacy_list,
            discover,
        })
    }

    pub fn list_for(&self, era: Era) -> &Bytes {
        match era {
            Era::Modern => &self.modern_list,
            Era::Legacy => &self.legacy_list,
        }
    }

    pub fn tool(&self, name: &str) -> Option<Arc<Tool>> {
        self.tools.get(name).cloned()
    }

    pub fn shape(&self) -> crate::model::Shape {
        self.config.response
    }

    pub fn era(&self) -> Era {
        self.config.era
    }

    pub fn name(&self) -> &str {
        &self.config.name
    }
}

fn build_tool(config: &ToolConfig) -> Result<Tool, String> {
    let definition = match &config.definition {
        Some(definition) => definition.clone(),
        None => {
            let mut object = Map::new();
            object.insert("name".into(), Value::String(config.name.clone()));
            if let Some(description) = &config.description {
                object.insert("description".into(), Value::String(description.clone()));
            }
            object.insert("inputSchema".into(), config.input_schema.clone());
            Value::Object(object)
        }
    };
    if !definition.is_object() {
        return Err(format!(
            "tool {}: definition must be a JSON object",
            config.name
        ));
    }
    let param_headers = derive_param_headers(&definition);
    Ok(Tool {
        config: config.clone(),
        definition,
        param_headers,
    })
}

/// The spec's statically reachable walk: only `properties` keys are followed.
/// An annotation that violates the constraints is ignored here rather than
/// rejecting the tool: a mock must be able to serve a bad definition to the
/// control plane's discovery validator.
pub fn derive_param_headers(definition: &Value) -> Vec<ParamHeader> {
    let schema = definition
        .get("inputSchema")
        .filter(|schema| schema.is_object());
    let mut out = Vec::new();
    if let Some(schema) = schema {
        let mut seen = HashSet::new();
        walk_properties(schema, &mut Vec::new(), &mut out, &mut seen);
    }
    out
}

fn walk_properties(
    schema: &Value,
    path: &mut Vec<String>,
    out: &mut Vec<ParamHeader>,
    seen: &mut HashSet<String>,
) {
    let Some(properties) = schema.get("properties").and_then(Value::as_object) else {
        return;
    };
    for (name, property) in properties {
        let Some(property) = property.as_object() else {
            continue;
        };
        path.push(name.clone());
        if let Some(header) = property.get("x-mcp-header").and_then(Value::as_str) {
            if let Some(kind) = header_kind(property) {
                if is_token(header) && seen.insert(header.to_ascii_lowercase()) {
                    out.push(ParamHeader {
                        path: path.clone(),
                        name: header.to_string(),
                        kind,
                    });
                }
            }
        }
        // Nested object properties are reachable, but only through `properties`.
        if property.get("properties").is_some() {
            walk_properties(&Value::Object(property.clone()), path, out, seen);
        }
        path.pop();
    }
}

fn header_kind(property: &Map<String, Value>) -> Option<HeaderKind> {
    match property.get("type").and_then(Value::as_str)? {
        "string" => Some(HeaderKind::String),
        "integer" => {
            // The spec bounds annotated integers to the JavaScript safe range.
            let bound_ok = |key: &str| {
                property
                    .get(key)
                    .and_then(Value::as_f64)
                    .map(|value| value.abs() <= 9_007_199_254_740_991.0)
                    .unwrap_or(true)
            };
            (bound_ok("minimum") && bound_ok("maximum")).then_some(HeaderKind::Integer)
        }
        "boolean" => Some(HeaderKind::Boolean),
        _ => None,
    }
}

/// HTTP field-name token, RFC 9110 section 5.1.
pub fn is_token(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(
                    byte,
                    b'!' | b'#'
                        | b'$'
                        | b'%'
                        | b'&'
                        | b'\''
                        | b'*'
                        | b'+'
                        | b'-'
                        | b'.'
                        | b'^'
                        | b'_'
                        | b'`'
                        | b'|'
                        | b'~'
                )
        })
}

fn list_result(
    tools: &[Arc<Tool>],
    config: &InstanceConfig,
    era: Era,
    server_info: &Value,
) -> Bytes {
    let mut result = Map::new();
    if era == Era::Modern {
        result.insert("resultType".into(), json!("complete"));
    }
    result.insert(
        "tools".into(),
        Value::Array(tools.iter().map(|tool| tool.definition.clone()).collect()),
    );
    if era == Era::Modern {
        result.insert("ttlMs".into(), json!(config.ttl_ms));
        result.insert("cacheScope".into(), json!(config.cache_scope.as_str()));
        result.insert(
            "_meta".into(),
            json!({"io.modelcontextprotocol/serverInfo": server_info}),
        );
    }
    Bytes::from(serde_json::to_vec(&Value::Object(result)).expect("serialize tool list"))
}

fn discover_result(config: &InstanceConfig, server_info: &Value) -> Bytes {
    let mut result = Map::new();
    result.insert("resultType".into(), json!("complete"));
    result.insert("supportedVersions".into(), json!([MODERN_VERSION]));
    result.insert("capabilities".into(), json!({"tools": {}}));
    result.insert(
        "_meta".into(),
        json!({"io.modelcontextprotocol/serverInfo": server_info}),
    );
    if let Some(instructions) = &config.instructions {
        result.insert("instructions".into(), json!(instructions));
    }
    result.insert("ttlMs".into(), json!(config.ttl_ms));
    result.insert("cacheScope".into(), json!(config.cache_scope.as_str()));
    Bytes::from(serde_json::to_vec(&Value::Object(result)).expect("serialize discover"))
}

#[allow(dead_code)]
pub fn protocol_versions() -> [&'static str; 2] {
    [MODERN_VERSION, LEGACY_VERSION]
}

#[allow(dead_code)]
pub fn default_ttl() -> u64 {
    DEFAULT_TTL_MS
}

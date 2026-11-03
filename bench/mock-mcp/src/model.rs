//! Configuration, wire faults and request records for the MCP mock.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const MODERN_VERSION: &str = "2026-07-28";
pub const LEGACY_VERSION: &str = "2025-11-25";
pub const DEFAULT_TTL_MS: u64 = 60_000;
pub const DEFAULT_OVERSIZED_BYTES: usize = 17 * 1024 * 1024;
pub const DEFAULT_SLOW_EVENTS: u32 = 40;
pub const DEFAULT_SLOW_INTERVAL_MS: u64 = 10;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Era {
    #[default]
    Modern,
    Legacy,
}

impl Era {
    pub fn version(self) -> &'static str {
        match self {
            Era::Modern => MODERN_VERSION,
            Era::Legacy => LEGACY_VERSION,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Shape {
    #[default]
    Json,
    Sse,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CacheScope {
    #[default]
    Public,
    Private,
}

impl CacheScope {
    pub fn as_str(self) -> &'static str {
        match self {
            CacheScope::Public => "public",
            CacheScope::Private => "private",
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Auth {
    #[default]
    None,
    Bearer {
        token: String,
    },
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SessionMode {
    Off,
    #[default]
    Optional,
    Required,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Behaviour {
    #[default]
    Echo,
    Fixed,
    ToolError,
    ProtocolError,
    Sleep,
    Progress,
    Mrtr,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ErrorSpec {
    #[serde(default)]
    pub code: Option<i64>,
    #[serde(default)]
    pub message: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FaultKind {
    OversizedSseEvent,
    Compressed,
    InvalidSse,
    StalledStream,
    SlowReads,
    ResetMidStream,
    WrongContentType,
    Unauthorized,
    ServerError,
    #[serde(rename = "session_404_once")]
    Session404Once,
}

impl FaultKind {
    pub fn is_stream(self) -> bool {
        matches!(
            self,
            FaultKind::OversizedSseEvent
                | FaultKind::InvalidSse
                | FaultKind::StalledStream
                | FaultKind::SlowReads
                | FaultKind::ResetMidStream
        )
    }

    pub fn as_str(self) -> &'static str {
        match self {
            FaultKind::OversizedSseEvent => "oversized_sse_event",
            FaultKind::Compressed => "compressed",
            FaultKind::InvalidSse => "invalid_sse",
            FaultKind::StalledStream => "stalled_stream",
            FaultKind::SlowReads => "slow_reads",
            FaultKind::ResetMidStream => "reset_mid_stream",
            FaultKind::WrongContentType => "wrong_content_type",
            FaultKind::Unauthorized => "unauthorized",
            FaultKind::ServerError => "server_error",
            FaultKind::Session404Once => "session_404_once",
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fault {
    pub kind: FaultKind,
    #[serde(default)]
    pub bytes: Option<usize>,
    #[serde(default)]
    pub events: Option<u32>,
    #[serde(default)]
    pub interval_ms: Option<u64>,
    #[serde(default)]
    pub after_events: Option<u32>,
    #[serde(default)]
    pub status: Option<u16>,
    #[serde(default)]
    pub content_type: Option<String>,
    #[serde(default)]
    pub www_authenticate: Option<String>,
}

impl Fault {
    pub fn new(kind: FaultKind) -> Self {
        Self {
            kind,
            bytes: None,
            events: None,
            interval_ms: None,
            after_events: None,
            status: None,
            content_type: None,
            www_authenticate: None,
        }
    }

    pub fn oversized_bytes(&self) -> usize {
        self.bytes.unwrap_or(DEFAULT_OVERSIZED_BYTES)
    }

    pub fn slow_events(&self) -> u32 {
        self.events.unwrap_or(DEFAULT_SLOW_EVENTS)
    }

    pub fn slow_interval_ms(&self) -> u64 {
        self.interval_ms.unwrap_or(DEFAULT_SLOW_INTERVAL_MS)
    }

    pub fn after_events(&self) -> u32 {
        self.after_events.unwrap_or(1)
    }

    pub fn status(&self) -> u16 {
        self.status.unwrap_or(503)
    }

    pub fn content_type(&self) -> &str {
        self.content_type.as_deref().unwrap_or("text/plain")
    }

    pub fn www_authenticate(&self) -> &str {
        self.www_authenticate
            .as_deref()
            .unwrap_or("Bearer realm=\"mock-mcp\", error=\"invalid_token\"")
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ToolConfig {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default = "default_input_schema")]
    pub input_schema: Value,
    /// Served verbatim in `tools/list` when present. This is what makes a
    /// definition change (a rug pull) a plain admin update.
    #[serde(default)]
    pub definition: Option<Value>,
    #[serde(default)]
    pub behaviour: Behaviour,
    #[serde(default)]
    pub result: Option<Value>,
    #[serde(default)]
    pub error: Option<ErrorSpec>,
    #[serde(default)]
    pub sleep_ms: u64,
    #[serde(default)]
    pub progress_events: u32,
    #[serde(default)]
    pub progress_interval_ms: u64,
    #[serde(default)]
    pub response: Option<Shape>,
    #[serde(default)]
    pub fault: Option<Fault>,
}

fn default_input_schema() -> Value {
    serde_json::json!({"type": "object"})
}

impl Default for ToolConfig {
    fn default() -> Self {
        Self {
            name: String::new(),
            description: None,
            input_schema: default_input_schema(),
            definition: None,
            behaviour: Behaviour::Echo,
            result: None,
            error: None,
            sleep_ms: 0,
            progress_events: 0,
            progress_interval_ms: 0,
            response: None,
            fault: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct InstanceConfig {
    pub name: String,
    #[serde(default)]
    pub era: Era,
    #[serde(default)]
    pub response: Shape,
    #[serde(default)]
    pub auth: Auth,
    /// An absent `Origin` is always accepted; a present one must match exactly.
    #[serde(default)]
    pub allowed_origins: Vec<String>,
    #[serde(default = "default_ttl")]
    pub ttl_ms: u64,
    #[serde(default)]
    pub cache_scope: CacheScope,
    #[serde(default)]
    pub session: SessionMode,
    /// Legacy only: answer GET with a standalone SSE stream instead of 405.
    #[serde(default)]
    pub get_stream: bool,
    #[serde(default)]
    pub instructions: Option<String>,
    #[serde(default)]
    pub tools: Vec<ToolConfig>,
    #[serde(default)]
    pub fault: Option<Fault>,
}

fn default_ttl() -> u64 {
    DEFAULT_TTL_MS
}

impl Default for InstanceConfig {
    fn default() -> Self {
        Self {
            name: String::new(),
            era: Era::Modern,
            response: Shape::Json,
            auth: Auth::None,
            allowed_origins: Vec::new(),
            ttl_ms: DEFAULT_TTL_MS,
            cache_scope: CacheScope::Public,
            session: SessionMode::Optional,
            get_stream: false,
            instructions: None,
            tools: Vec::new(),
            fault: None,
        }
    }
}

impl InstanceConfig {
    pub fn validate(&self) -> Result<(), String> {
        if self.name.is_empty()
            || self.name.len() > 64
            || !self
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        {
            return Err("name must be 1..=64 characters of [A-Za-z0-9_.-]".into());
        }
        if let Auth::Bearer { token } = &self.auth {
            if token.is_empty() {
                return Err("auth token must not be empty".into());
            }
        }
        if let Some(fault) = &self.fault {
            validate_fault(fault)?;
        }
        let mut names = std::collections::HashSet::new();
        for tool in &self.tools {
            validate_tool(tool)?;
            if !names.insert(tool.name.clone()) {
                return Err(format!("duplicate tool name: {}", tool.name));
            }
        }
        Ok(())
    }
}

pub fn validate_tool(tool: &ToolConfig) -> Result<(), String> {
    if tool.name.is_empty() {
        return Err("tool name must not be empty".into());
    }
    if let Some(definition) = &tool.definition {
        if !definition.is_object() {
            return Err(format!(
                "tool {}: definition must be a JSON object",
                tool.name
            ));
        }
    }
    if !tool.input_schema.is_object() && tool.definition.is_none() {
        return Err(format!(
            "tool {}: inputSchema must be a JSON object",
            tool.name
        ));
    }
    if let Some(fault) = &tool.fault {
        validate_fault(fault)?;
    }
    if tool.progress_events > 10_000 {
        return Err(format!(
            "tool {}: progress_events must be <= 10000",
            tool.name
        ));
    }
    Ok(())
}

pub fn validate_fault(fault: &Fault) -> Result<(), String> {
    if let Some(bytes) = fault.bytes {
        if bytes == 0 {
            return Err("fault bytes must be positive".into());
        }
    }
    if let Some(status) = fault.status {
        if !(400..=599).contains(&status) {
            return Err("fault status must be 400..=599".into());
        }
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct SetTools {
    pub tools: Vec<ToolConfig>,
}

#[derive(Debug, Deserialize)]
pub struct SetFault {
    pub fault: Option<Fault>,
}

#[derive(Debug, Deserialize)]
pub struct SetRecording {
    pub enabled: bool,
}

/// One request as the mock saw it. Mutated in place while the call is in flight.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RequestRecord {
    pub seq: u64,
    pub server: String,
    pub http_method: String,
    pub path: String,
    pub received_at_ms: u64,
    pub completed_at_ms: Option<u64>,
    /// Header names are lowercase; every value is kept, in order.
    pub headers: BTreeMap<String, Vec<String>>,
    pub body: String,
    pub body_truncated: bool,
    pub era: Option<Era>,
    pub method: Option<String>,
    pub request_id: Option<Value>,
    pub tool: Option<String>,
    pub session_id: Option<String>,
    pub status: Option<u16>,
    pub content_type: Option<String>,
    pub outcome: Option<String>,
    pub result_type: Option<String>,
    pub cancelled: bool,
    pub cancelled_by: Option<String>,
    pub disconnected: bool,
    pub completed_after_disconnect: bool,
    pub fault: Option<String>,
    pub notes: Option<String>,
}

impl RequestRecord {
    pub fn new(seq: u64, server: String, http_method: String, path: String) -> Self {
        Self {
            seq,
            server,
            http_method,
            path,
            received_at_ms: now_ms(),
            completed_at_ms: None,
            headers: BTreeMap::new(),
            body: String::new(),
            body_truncated: false,
            era: None,
            method: None,
            request_id: None,
            tool: None,
            session_id: None,
            status: None,
            content_type: None,
            outcome: None,
            result_type: None,
            cancelled: false,
            cancelled_by: None,
            disconnected: false,
            completed_after_disconnect: false,
            fault: None,
            notes: None,
        }
    }
}

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

//! MCP servers and A2A agents compiled from a snapshot into what the request
//! path reads: URLs, outbound auth, timeouts, review state and approved
//! definition hashes (`docs/plugins.md`, `docs/config-protocol.md`).
//!
//! Nothing here runs per request: [`Targets::compile`] runs on the apply task,
//! after [`validate`], once per snapshot. Secrets are opened here (sealed by the
//! control plane like `Upstream.api_key_ciphertext`, with the MCP/A2A HKDF
//! info) and never carried through the snapshot or the config stream.
//!
//! The module does not own snapshot validation: it exposes [`validate`] for
//! `snapshot::validate` to call and [`capabilities`] for the required-capability
//! check. Both are pure (no environment, no secrets) so a snapshot can be
//! rejected before anything is opened.

// The MCP/A2A request path (W2) wires these modules; until then most of the
// API has no caller in the binary, which `-D warnings` would reject.
#![allow(dead_code)]
use std::{
    collections::{HashMap, HashSet},
    fmt,
    sync::Arc,
    time::Duration,
};

use hyper::{header::HeaderValue, Uri};
use lumewarp_proto::config::v1::{self as pb, Snapshot};
use ring::{aead, hkdf};
use serde_json::Value;

use crate::{
    mcp_hash::{self, MAX_NAME_BYTES},
    router::plain_http_uri,
    snapshot::ApplyError,
};

/// HKDF info for `UpstreamAuth.secret_ciphertext`
/// (`snapshot.proto:361-370`, `sealed.rs` has the sibling upstream-key info).
pub const SEALED_INFO: &[u8] = b"lumewarp/upstream-auth-secret/v1";

/// Timeout of one upstream request when `timeout_ms` is 0.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);

/// Largest `PluginRef.params_json` (proto: "at most 16 KiB UTF-8").
pub const MAX_PARAMS_BYTES: usize = 16 * 1024;

/// Largest `A2aAgent.card_json` (proto: "JSON, at most 64 KiB").
pub const MAX_CARD_BYTES: usize = 64 * 1024;

/// Approved definitions accepted per MCP server. Bounds apply-time work and the
/// per-name hash lists; a server with more should split.
pub const MAX_APPROVED_DEFINITIONS: usize = 10_000;

/// Where the agent card lives when `A2aAgent.card_url` is empty.
pub const WELL_KNOWN_CARD_PATH: &str = "/.well-known/agent-card.json";

/// Separator between server and tool on the aggregated MCP endpoint.
pub const TOOL_SEPARATOR: &str = "__";

/// Built-in upstream auth plugins (`docs/plugins.md`, "Kinds and capabilities").
pub const BUILTIN_AUTH: [&str; 3] = ["none", "static_bearer", "passthrough"];

/// `^[a-z][a-z0-9_-]{0,62}$`: MCP server and A2A agent names.
pub fn valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_' || *b == b'-')
}

/// `^[a-z][a-z0-9_]{0,62}$`: plugin names (from a manifest, so no hyphen).
pub fn valid_plugin_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

/// How the gateway decides which upstream definitions to serve.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReviewMode {
    /// A name seen for the first time is served and reported; a changed
    /// definition of a known name is withheld (the proto default).
    Tofu,
    /// Only approved definitions are served.
    Manual,
    /// Definitions are not checked.
    Off,
}

impl ReviewMode {
    /// `UNSPECIFIED` is documented as TOFU (`snapshot.proto:377-390`).
    /// `None` for an enum value this gateway does not know.
    pub fn from_pb(value: i32) -> Option<Self> {
        match pb::McpReviewMode::try_from(value).ok()? {
            pb::McpReviewMode::Unspecified | pb::McpReviewMode::Tofu => Some(Self::Tofu),
            pb::McpReviewMode::Manual => Some(Self::Manual),
            pb::McpReviewMode::Off => Some(Self::Off),
        }
    }
}

/// A plugin and its settings, validated at apply time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plugin {
    pub name: Arc<str>,
    /// Settings JSON text (`{}` when the snapshot left it empty), passed
    /// unchanged to the plugin's `configure`.
    pub params_json: Arc<str>,
    /// sha256 of the canonical settings: the cache key for outbound auth
    /// (plan P9) and the identity of a middleware instance.
    pub digest: [u8; 32],
}

/// How the gateway authenticates to an MCP server or A2A agent.
#[derive(Clone)]
pub enum Auth {
    /// No `Authorization` header upstream.
    None,
    /// `Authorization: Bearer <token>`, parsed at apply time.
    StaticBearer { authorization: HeaderValue },
    /// Forward the client's `Authorization` header unchanged.
    Passthrough,
    /// A WASM `upstream-auth` plugin (built-ins are the variants above).
    Plugin(Plugin),
}

impl Auth {
    /// The configured plugin name, built-ins included.
    pub fn plugin_name(&self) -> &str {
        match self {
            Self::None => "none",
            Self::StaticBearer { .. } => "static_bearer",
            Self::Passthrough => "passthrough",
            Self::Plugin(plugin) => &plugin.name,
        }
    }

    /// The `Authorization` header to send upstream, when the gateway has one.
    /// `Passthrough` borrows the client's; a WASM plugin produces one per call.
    pub fn authorization(&self) -> Option<&HeaderValue> {
        match self {
            Self::StaticBearer { authorization } => Some(authorization),
            _ => None,
        }
    }

    /// The WASM plugin, for the adapter. `None` for the built-ins.
    pub fn plugin(&self) -> Option<&Plugin> {
        match self {
            Self::Plugin(plugin) => Some(plugin),
            _ => None,
        }
    }
}

impl fmt::Debug for Auth {
    /// Never prints the secret.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StaticBearer { .. } => f.write_str("Auth::StaticBearer(<redacted>)"),
            Self::None => f.write_str("Auth::None"),
            Self::Passthrough => f.write_str("Auth::Passthrough"),
            Self::Plugin(plugin) => f.debug_tuple("Auth::Plugin").field(&plugin.name).finish(),
        }
    }
}

/// Approved definition hashes of one MCP server, keyed by upstream name.
///
/// A name can carry more than one hash while the control plane approves a
/// replacement before the upstream switches: any approved hash is served.
#[derive(Clone, Debug, Default)]
pub struct Approved {
    tools: HashMap<Arc<str>, Vec<[u8; 32]>>,
    prompts: HashMap<Arc<str>, Vec<[u8; 32]>>,
    count: usize,
}

impl Approved {
    fn map(&self, kind: pb::McpDefinitionKind) -> Option<&HashMap<Arc<str>, Vec<[u8; 32]>>> {
        match kind {
            pb::McpDefinitionKind::Tool => Some(&self.tools),
            pb::McpDefinitionKind::Prompt => Some(&self.prompts),
            pb::McpDefinitionKind::Unspecified => None,
        }
    }

    /// Whether this exact definition was approved.
    pub fn accepts(&self, kind: pb::McpDefinitionKind, name: &str, hash: &[u8; 32]) -> bool {
        self.map(kind)
            .and_then(|map| map.get(name))
            .is_some_and(|hashes| hashes.contains(hash))
    }

    /// Whether any definition of this name was approved: under TOFU a known
    /// name is withheld when its hash changed, a new name is served.
    pub fn has_name(&self, kind: pb::McpDefinitionKind, name: &str) -> bool {
        self.map(kind).is_some_and(|map| map.contains_key(name))
    }

    /// Approved names of one kind, unsorted.
    pub fn names(&self, kind: pb::McpDefinitionKind) -> impl Iterator<Item = &str> {
        self.map(kind)
            .into_iter()
            .flat_map(|map| map.keys().map(|name| name.as_ref()))
    }

    /// Number of approved definitions (duplicate hashes counted once).
    pub fn len(&self) -> usize {
        self.count
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }

    fn insert(&mut self, kind: pb::McpDefinitionKind, name: &str, hash: [u8; 32]) {
        let name: Arc<str> = name.into();
        let map = match kind {
            pb::McpDefinitionKind::Tool => &mut self.tools,
            pb::McpDefinitionKind::Prompt => &mut self.prompts,
            pb::McpDefinitionKind::Unspecified => return,
        };
        let hashes = map.entry(name).or_default();
        if !hashes.contains(&hash) {
            hashes.push(hash);
            self.count += 1;
        }
    }
}

/// One MCP server as the request path sees it.
#[derive(Debug)]
pub struct McpTarget {
    pub id: Arc<str>,
    pub name: Arc<str>,
    /// Streamable HTTP endpoint.
    pub url: Uri,
    pub auth: Auth,
    pub timeout: Duration,
    pub review: ReviewMode,
    approved: Approved,
}

impl McpTarget {
    pub fn approved(&self) -> &Approved {
        &self.approved
    }

    /// The name a tool has on the aggregated `/mcp` endpoint.
    pub fn qualified_tool(&self, tool: &str) -> String {
        let mut name = String::with_capacity(self.name.len() + TOOL_SEPARATOR.len() + tool.len());
        name.push_str(&self.name);
        name.push_str(TOOL_SEPARATOR);
        name.push_str(tool);
        name
    }
}

/// One A2A agent as the request path sees it.
#[derive(Debug)]
pub struct A2aTarget {
    pub id: Arc<str>,
    pub name: Arc<str>,
    /// JSON-RPC endpoint of the upstream agent.
    pub url: Uri,
    /// Where the agent card is fetched (`card_url`, or the origin of `url` plus
    /// the well-known path).
    pub card_url: Uri,
    pub auth: Auth,
    /// At most one middleware (plan D14).
    pub middleware: Option<Plugin>,
    pub timeout: Duration,
    /// Card the control plane stored (pasted by an administrator or reported by
    /// a gateway), served until the gateway fetched one itself.
    pub card_json: Option<Arc<str>>,
}

/// The MCP servers and A2A agents of one snapshot.
#[derive(Debug, Default)]
pub struct Targets {
    servers: HashMap<Arc<str>, Arc<McpTarget>>,
    agents: HashMap<Arc<str>, Arc<A2aTarget>>,
    /// Sorted by name, for stable listings.
    server_list: Vec<Arc<McpTarget>>,
    agent_list: Vec<Arc<A2aTarget>>,
}

impl Targets {
    /// An empty set: no MCP server and no A2A agent.
    pub fn empty() -> Self {
        Self::default()
    }

    /// Validates and compiles both entity lists. `encryption_secret` is
    /// `LUMEWARP_ENCRYPTION_KEY` (`sealed::env_secret()`); a sealed secret
    /// without it is an INVALID_CONFIG, exactly like an upstream API key.
    pub fn compile(
        snapshot: &Snapshot,
        encryption_secret: Option<&str>,
    ) -> Result<Self, ApplyError> {
        validate(snapshot)?;
        let mut targets = Targets::default();
        for server in &snapshot.mcp_servers {
            let name: Arc<str> = server.name.as_str().into();
            let target = Arc::new(McpTarget {
                id: server.id.as_str().into(),
                name: name.clone(),
                url: plain_http_uri(&server.url)
                    .ok_or_else(|| invalid(format!("mcp server {name:?} has an invalid url")))?,
                auth: compile_auth(
                    server.auth.as_ref(),
                    &format!("mcp server {name:?}"),
                    encryption_secret,
                )?,
                timeout: timeout(server.timeout_ms),
                review: ReviewMode::from_pb(server.review_mode).ok_or_else(|| {
                    invalid(format!("mcp server {name:?} has an unknown review_mode"))
                })?,
                approved: compile_approved(server, &name)?,
            });
            targets.server_list.push(target.clone());
            targets.servers.insert(name, target);
        }
        for agent in &snapshot.a2a_agents {
            let name: Arc<str> = agent.name.as_str().into();
            let url = plain_http_uri(&agent.url)
                .ok_or_else(|| invalid(format!("a2a agent {name:?} has an invalid url")))?;
            let card_url = if agent.card_url.is_empty() {
                card_uri(&url)
                    .ok_or_else(|| invalid(format!("a2a agent {name:?} has an invalid url")))?
            } else {
                plain_http_uri(&agent.card_url)
                    .ok_or_else(|| invalid(format!("a2a agent {name:?} has an invalid card_url")))?
            };
            let target = Arc::new(A2aTarget {
                id: agent.id.as_str().into(),
                name: name.clone(),
                url,
                card_url,
                auth: compile_auth(
                    agent.auth.as_ref(),
                    &format!("a2a agent {name:?}"),
                    encryption_secret,
                )?,
                middleware: agent
                    .middleware
                    .as_ref()
                    .filter(|plugin| !plugin.plugin.is_empty())
                    .map(|plugin| compile_plugin(plugin, &format!("a2a agent {name:?} middleware")))
                    .transpose()?,
                timeout: timeout(agent.timeout_ms),
                card_json: (!agent.card_json.is_empty())
                    .then(|| Arc::from(agent.card_json.as_str())),
            });
            targets.agent_list.push(target.clone());
            targets.agents.insert(name, target);
        }
        targets.server_list.sort_by(|a, b| a.name.cmp(&b.name));
        targets.agent_list.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(targets)
    }

    pub fn server(&self, name: &str) -> Option<&Arc<McpTarget>> {
        self.servers.get(name)
    }

    pub fn agent(&self, name: &str) -> Option<&Arc<A2aTarget>> {
        self.agents.get(name)
    }

    /// Every MCP server, sorted by name.
    pub fn servers(&self) -> impl Iterator<Item = &Arc<McpTarget>> {
        self.server_list.iter()
    }

    /// Every A2A agent, sorted by name.
    pub fn agents(&self) -> impl Iterator<Item = &Arc<A2aTarget>> {
        self.agent_list.iter()
    }

    pub fn is_empty(&self) -> bool {
        self.servers.is_empty() && self.agents.is_empty()
    }
}

/// Capabilities these entities require: `mcp`, `a2a`, and one `auth:<plugin>`
/// or `a2a_middleware:<plugin>` per plugin they name, sorted and deduplicated.
/// The caller feeds each of them to the `require(used, cap)` check in
/// `snapshot::validate`.
pub fn capabilities(snapshot: &Snapshot) -> Vec<String> {
    let mut caps = Vec::new();
    if !snapshot.mcp_servers.is_empty() {
        caps.push("mcp".to_owned());
    }
    if !snapshot.a2a_agents.is_empty() {
        caps.push("a2a".to_owned());
    }
    for server in &snapshot.mcp_servers {
        if let Some(plugin) = server.auth.as_ref().and_then(|auth| auth.plugin.as_ref()) {
            caps.push(format!("auth:{}", plugin.plugin));
        }
    }
    for agent in &snapshot.a2a_agents {
        if let Some(plugin) = agent.auth.as_ref().and_then(|auth| auth.plugin.as_ref()) {
            caps.push(format!("auth:{}", plugin.plugin));
        }
        if let Some(middleware) = &agent.middleware {
            caps.push(format!("a2a_middleware:{}", middleware.plugin));
        }
    }
    caps.sort();
    caps.dedup();
    caps
}

/// Checks everything [`Targets::compile`] needs, without touching secrets or
/// the environment. Safe to call from `snapshot::validate`.
pub fn validate(snapshot: &Snapshot) -> Result<(), ApplyError> {
    unique(
        snapshot.mcp_servers.iter().map(|s| s.name.as_str()),
        "mcp server",
    )?;
    unique(
        snapshot.a2a_agents.iter().map(|a| a.name.as_str()),
        "a2a agent",
    )?;
    for server in &snapshot.mcp_servers {
        let what = format!("mcp server {:?}", server.name);
        if !valid_name(&server.name) {
            return Err(invalid(format!(
                "{what} name does not match ^[a-z][a-z0-9_-]{{0,62}}$"
            )));
        }
        if plain_http_uri(&server.url).is_none() {
            return Err(invalid(format!(
                "{what} url must be an absolute http(s) URL without userinfo"
            )));
        }
        if ReviewMode::from_pb(server.review_mode).is_none() {
            return Err(invalid(format!(
                "{what} has an unknown review_mode {}",
                server.review_mode
            )));
        }
        check_auth(server.auth.as_ref(), &what)?;
        compile_approved(server, &server.name)?;
    }
    for agent in &snapshot.a2a_agents {
        let what = format!("a2a agent {:?}", agent.name);
        if !valid_name(&agent.name) {
            return Err(invalid(format!(
                "{what} name does not match ^[a-z][a-z0-9_-]{{0,62}}$"
            )));
        }
        let url = plain_http_uri(&agent.url).ok_or_else(|| {
            invalid(format!(
                "{what} url must be an absolute http(s) URL without userinfo"
            ))
        })?;
        if !agent.card_url.is_empty() && plain_http_uri(&agent.card_url).is_none() {
            return Err(invalid(format!(
                "{what} card_url must be an absolute http(s) URL without userinfo"
            )));
        }
        if agent.card_url.is_empty() && card_uri(&url).is_none() {
            return Err(invalid(format!("{what} has an invalid url")));
        }
        check_auth(agent.auth.as_ref(), &what)?;
        if let Some(middleware) = &agent.middleware {
            if !middleware.plugin.is_empty() {
                compile_plugin(middleware, &format!("{what} middleware"))?;
            }
        }
        if !agent.card_json.is_empty() {
            if agent.card_json.len() > MAX_CARD_BYTES {
                return Err(invalid(format!("{what} card_json exceeds 64 KiB")));
            }
            let card: Value = serde_json::from_str(&agent.card_json)
                .map_err(|_| invalid(format!("{what} card_json must be a JSON object")))?;
            if !card.is_object() {
                return Err(invalid(format!("{what} card_json must be a JSON object")));
            }
        }
    }
    Ok(())
}

fn unique<'a>(names: impl Iterator<Item = &'a str>, kind: &str) -> Result<(), ApplyError> {
    let mut seen = HashSet::new();
    for name in names {
        if name.is_empty() {
            return Err(invalid(format!("{kind} with empty name")));
        }
        if !seen.insert(name) {
            return Err(invalid(format!("duplicate {kind} {name:?}")));
        }
    }
    Ok(())
}

fn invalid(reason: String) -> ApplyError {
    ApplyError::invalid(reason)
}

fn timeout(timeout_ms: u32) -> Duration {
    if timeout_ms == 0 {
        DEFAULT_TIMEOUT
    } else {
        Duration::from_millis(timeout_ms.into())
    }
}

/// `http(s)://authority` plus the well-known card path.
fn card_uri(url: &Uri) -> Option<Uri> {
    // Replacing path_and_query drops any query string of the JSON-RPC URL.
    let mut parts = url.clone().into_parts();
    parts.path_and_query = Some(WELL_KNOWN_CARD_PATH.parse().ok()?);
    Uri::from_parts(parts).ok()
}

fn params_object(text: &str, what: &str) -> Result<Value, ApplyError> {
    if text.len() > MAX_PARAMS_BYTES {
        return Err(invalid(format!("{what} params exceed 16 KiB")));
    }
    if text.is_empty() {
        return Ok(Value::Object(Default::default()));
    }
    let params: Value = serde_json::from_str(text)
        .map_err(|_| invalid(format!("{what} params must be a JSON object")))?;
    if !params.is_object() {
        return Err(invalid(format!("{what} params must be a JSON object")));
    }
    Ok(params)
}

fn check_plugin_name(name: &str, what: &str) -> Result<(), ApplyError> {
    if !valid_plugin_name(name) {
        return Err(invalid(format!(
            "{what} plugin name does not match ^[a-z][a-z0-9_]{{0,62}}$"
        )));
    }
    Ok(())
}

fn compile_plugin(plugin: &pb::PluginRef, what: &str) -> Result<Plugin, ApplyError> {
    check_plugin_name(&plugin.plugin, what)?;
    let params = params_object(&plugin.params_json, what)?;
    let digest = mcp_hash::hash_canonical(&mcp_hash::canonicalize(&params));
    Ok(Plugin {
        name: plugin.plugin.as_str().into(),
        params_json: if plugin.params_json.is_empty() {
            Arc::from("{}")
        } else {
            Arc::from(plugin.params_json.as_str())
        },
        digest,
    })
}

/// Checks `auth` and the built-in plugin's settings. Pure: it never reads the
/// environment nor opens a secret.
fn check_auth(auth: Option<&pb::UpstreamAuth>, what: &str) -> Result<(), ApplyError> {
    let Some(auth) = auth else {
        return Ok(());
    };
    let plugin = auth
        .plugin
        .as_ref()
        .ok_or_else(|| invalid(format!("{what} auth has no plugin")))?;
    check_plugin_name(&plugin.plugin, what)?;
    if !auth.secret_ref.is_empty() && !auth.secret_ciphertext.is_empty() {
        return Err(invalid(format!(
            "{what} sets both secret_ref and secret_ciphertext"
        )));
    }
    let builtin = BUILTIN_AUTH.contains(&plugin.plugin.as_str());
    if builtin {
        let params = params_object(&plugin.params_json, what)?;
        if !params.as_object().is_some_and(|p| p.is_empty()) {
            return Err(invalid(format!(
                "{what} auth {:?} takes no params",
                plugin.plugin
            )));
        }
    }
    match plugin.plugin.as_str() {
        "static_bearer" => {
            if auth.secret_ref.is_empty() && auth.secret_ciphertext.is_empty() {
                return Err(invalid(format!(
                    "{what} static_bearer needs secret_ref or secret_ciphertext"
                )));
            }
        }
        "none" | "passthrough" => {
            if !auth.secret_ref.is_empty() || !auth.secret_ciphertext.is_empty() {
                return Err(invalid(format!(
                    "{what} auth {:?} takes no secret",
                    plugin.plugin
                )));
            }
        }
        _ => {
            if !auth.secret_ref.is_empty() || !auth.secret_ciphertext.is_empty() {
                return Err(invalid(format!(
                    "{what} auth plugin {:?} cannot use secret_ref or secret_ciphertext",
                    plugin.plugin
                )));
            }
            compile_plugin(plugin, what)?;
        }
    }
    Ok(())
}

fn compile_auth(
    auth: Option<&pb::UpstreamAuth>,
    what: &str,
    encryption_secret: Option<&str>,
) -> Result<Auth, ApplyError> {
    let Some(auth) = auth else {
        return Ok(Auth::None);
    };
    check_auth(Some(auth), what)?;
    let plugin = auth.plugin.as_ref().expect("check_auth requires a plugin");
    match plugin.plugin.as_str() {
        "none" => Ok(Auth::None),
        "passthrough" => Ok(Auth::Passthrough),
        "static_bearer" => {
            let token = if !auth.secret_ciphertext.is_empty() {
                open_secret(encryption_secret, &auth.secret_ciphertext).map_err(|error| {
                    invalid(format!("{what} static_bearer secret_ciphertext: {error}"))
                })?
            } else {
                let name = &auth.secret_ref;
                // Do not retain VarError::NotUnicode: its Display quotes the value.
                std::env::var(name).map_err(|_| {
                    invalid(format!(
                        "{what} static_bearer secret_ref {name:?} is not set"
                    ))
                })?
            };
            let mut authorization =
                HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| {
                    invalid(format!(
                        "{what} static_bearer token is not a valid header value"
                    ))
                })?;
            authorization.set_sensitive(true);
            Ok(Auth::StaticBearer { authorization })
        }
        _ => Ok(Auth::Plugin(compile_plugin(plugin, what)?)),
    }
}

/// Opens a secret sealed by the control plane with [`SEALED_INFO`] (same
/// format as `sealed.rs`: `0x01 || 12-byte nonce || ciphertext || 16-byte tag`,
/// HKDF-SHA256 over `LUMEWARP_ENCRYPTION_KEY`).
///
/// Errors never carry key material or the plaintext.
pub fn open_secret(secret: Option<&str>, sealed: &[u8]) -> Result<String, SecretError> {
    let secret = secret.ok_or(SecretError::NoKey)?;
    if sealed.len() < 1 + aead::NONCE_LEN + aead::AES_256_GCM.tag_len() || sealed[0] != 1 {
        return Err(SecretError::Format);
    }
    let mut key = [0u8; 32];
    hkdf::Salt::new(hkdf::HKDF_SHA256, &[])
        .extract(secret.as_bytes())
        .expand(&[SEALED_INFO], &aead::AES_256_GCM)
        .and_then(|okm| okm.fill(&mut key))
        .map_err(|_| SecretError::Mismatch)?;
    let key = aead::LessSafeKey::new(
        aead::UnboundKey::new(&aead::AES_256_GCM, &key).map_err(|_| SecretError::Mismatch)?,
    );
    let (nonce, body) = sealed[1..].split_at(aead::NONCE_LEN);
    let nonce = aead::Nonce::try_assume_unique_for_key(nonce).map_err(|_| SecretError::Format)?;
    let mut buf = body.to_vec();
    let plain = key
        .open_in_place(nonce, aead::Aad::empty(), &mut buf)
        .map_err(|_| SecretError::Mismatch)?;
    String::from_utf8(plain.to_vec()).map_err(|_| SecretError::Mismatch)
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SecretError {
    #[error("LUMEWARP_ENCRYPTION_KEY is not set")]
    NoKey,
    #[error("unknown sealed format")]
    Format,
    #[error("does not open with LUMEWARP_ENCRYPTION_KEY")]
    Mismatch,
}

fn compile_approved(server: &pb::McpServer, name: &str) -> Result<Approved, ApplyError> {
    if server.approved.len() > MAX_APPROVED_DEFINITIONS {
        return Err(invalid(format!(
            "mcp server {name:?} has more than {MAX_APPROVED_DEFINITIONS} approved definitions"
        )));
    }
    let mut approved = Approved::default();
    for definition in &server.approved {
        let kind = pb::McpDefinitionKind::try_from(definition.kind).map_err(|_| {
            invalid(format!(
                "mcp server {name:?} approved definition has unknown kind {}",
                definition.kind
            ))
        })?;
        if kind == pb::McpDefinitionKind::Unspecified {
            return Err(invalid(format!(
                "mcp server {name:?} approved definition has unspecified kind"
            )));
        }
        if definition.name.is_empty() || definition.name.len() > MAX_NAME_BYTES {
            return Err(invalid(format!(
                "mcp server {name:?} approved definition has an invalid name"
            )));
        }
        let hash: [u8; 32] = definition.hash.as_slice().try_into().map_err(|_| {
            invalid(format!(
                "mcp server {name:?} approved definition {:?} needs a 32-byte hash",
                definition.name
            ))
        })?;
        approved.insert(kind, &definition.name, hash);
    }
    Ok(approved)
}

/// Seals `plaintext` the way the control plane does, with a fixed nonce, so
/// the round-trip test does not need the Python sealer.
#[cfg(test)]
fn seal(secret: &str, plaintext: &str) -> Vec<u8> {
    let mut key = [0u8; 32];
    hkdf::Salt::new(hkdf::HKDF_SHA256, &[])
        .extract(secret.as_bytes())
        .expand(&[SEALED_INFO], &aead::AES_256_GCM)
        .and_then(|okm| okm.fill(&mut key))
        .unwrap();
    let key = aead::LessSafeKey::new(aead::UnboundKey::new(&aead::AES_256_GCM, &key).unwrap());
    let mut nonce = [0u8; aead::NONCE_LEN];
    for (i, byte) in nonce.iter_mut().enumerate() {
        *byte = i as u8;
    }
    let mut sealed = vec![1u8];
    sealed.extend_from_slice(&nonce);
    let mut body = plaintext.as_bytes().to_vec();
    key.seal_in_place_append_tag(
        aead::Nonce::assume_unique_for_key(nonce),
        aead::Aad::empty(),
        &mut body,
    )
    .unwrap();
    sealed.extend_from_slice(&body);
    sealed
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumewarp_proto::config::v1::{
        McpDefinition, McpDefinitionKind, McpReviewMode, McpServer, PluginRef, UpstreamAuth,
    };

    fn server(name: &str, url: &str) -> McpServer {
        McpServer {
            id: format!("id-{name}"),
            name: name.into(),
            url: url.into(),
            auth: None,
            timeout_ms: 0,
            review_mode: 0,
            approved: Vec::new(),
        }
    }

    fn agent(name: &str, url: &str, card_url: &str) -> pb::A2aAgent {
        pb::A2aAgent {
            id: format!("id-{name}"),
            name: name.into(),
            url: url.into(),
            card_url: card_url.into(),
            auth: None,
            middleware: None,
            timeout_ms: 0,
            card_json: String::new(),
        }
    }

    fn snapshot(servers: Vec<McpServer>, agents: Vec<pb::A2aAgent>) -> Snapshot {
        Snapshot {
            mcp_servers: servers,
            a2a_agents: agents,
            ..Snapshot::default()
        }
    }

    fn hash(byte: u8) -> Vec<u8> {
        vec![byte; 32]
    }

    fn approved(kind: McpDefinitionKind, name: &str, byte: u8) -> McpDefinition {
        McpDefinition {
            kind: kind as i32,
            name: name.into(),
            hash: hash(byte),
        }
    }

    #[test]
    fn compiles_names_urls_timeouts_and_review_modes() {
        let mut mcp = server("s1", "https://mcp.example.test/mcp");
        mcp.timeout_ms = 2500;
        mcp.review_mode = McpReviewMode::Manual as i32;
        let mut a2a = agent("a1", "https://agent.example.test/rpc", "");
        a2a.timeout_ms = 0;
        let targets = Targets::compile(&snapshot(vec![mcp], vec![a2a]), None).unwrap();
        let mcp = targets.server("s1").unwrap();
        assert_eq!(mcp.name.as_ref(), "s1");
        assert_eq!(mcp.id.as_ref(), "id-s1");
        assert_eq!(
            mcp.url,
            "https://mcp.example.test/mcp".parse::<Uri>().unwrap()
        );
        assert_eq!(mcp.timeout, Duration::from_millis(2500));
        assert_eq!(mcp.review, ReviewMode::Manual);
        assert!(matches!(mcp.auth, Auth::None));
        let a2a = targets.agent("a1").unwrap();
        assert_eq!(a2a.timeout, DEFAULT_TIMEOUT);
        assert_eq!(
            a2a.card_url,
            "https://agent.example.test/.well-known/agent-card.json"
                .parse::<Uri>()
                .unwrap()
        );
        assert_eq!(targets.servers().count(), 1);
        assert_eq!(targets.agents().count(), 1);
        assert!(!targets.is_empty());
        assert!(Targets::empty().is_empty());
    }

    #[test]
    fn review_mode_unspecified_is_tofu() {
        assert_eq!(ReviewMode::from_pb(0), Some(ReviewMode::Tofu));
        assert_eq!(ReviewMode::from_pb(1), Some(ReviewMode::Tofu));
        assert_eq!(ReviewMode::from_pb(2), Some(ReviewMode::Manual));
        assert_eq!(ReviewMode::from_pb(3), Some(ReviewMode::Off));
        assert_eq!(ReviewMode::from_pb(4), None);
        assert_eq!(ReviewMode::from_pb(-1), None);
    }

    #[test]
    fn card_url_is_explicit_or_the_well_known_path_on_the_origin() {
        let explicit = agent(
            "a1",
            "https://agent.example.test:8443/v1/rpc?x=1",
            "https://cards.example.test/a1.json",
        );
        let targets = Targets::compile(&snapshot(vec![], vec![explicit]), None).unwrap();
        assert_eq!(
            targets.agent("a1").unwrap().card_url,
            "https://cards.example.test/a1.json".parse::<Uri>().unwrap()
        );
        let derived = agent("a1", "http://agent.example.test:8080/rpc", "");
        let targets = Targets::compile(&snapshot(vec![], vec![derived]), None).unwrap();
        assert_eq!(
            targets.agent("a1").unwrap().card_url,
            "http://agent.example.test:8080/.well-known/agent-card.json"
                .parse::<Uri>()
                .unwrap()
        );
    }

    #[test]
    fn names_are_validated_and_unique() {
        // Names: regex, duplicates, empty.
        let too_long = "a".repeat(64);
        for bad in [
            "",
            "S1",
            "1s",
            "s 1",
            "s.1",
            "s/1",
            "s1!",
            too_long.as_str(),
        ] {
            let mut s = server("s1", "https://x.test/mcp");
            s.name = bad.into();
            let error = validate(&snapshot(vec![s], vec![])).unwrap_err();
            assert!(
                error.reason.contains("name does not match")
                    || error.reason.contains("with empty name"),
                "{bad:?}: {}",
                error.reason
            );
        }
        let mut ok = server("s-1_ok", "https://x.test/mcp");
        ok.name = "a".repeat(63);
        assert!(validate(&snapshot(vec![ok], vec![])).is_ok());
        let error = validate(&snapshot(
            vec![
                server("s1", "https://x.test/mcp"),
                server("s1", "https://x.test/mcp2"),
            ],
            vec![],
        ))
        .unwrap_err();
        assert!(error.reason.contains("duplicate mcp server"));
        let error = validate(&snapshot(
            vec![],
            vec![
                agent("a1", "https://y.test/rpc", ""),
                agent("a1", "https://y.test/rpc", ""),
            ],
        ))
        .unwrap_err();
        assert!(error.reason.contains("duplicate a2a agent"));
        // A server and an agent may share a name: separate namespaces.
        assert!(validate(&snapshot(
            vec![server("same", "https://x.test/mcp")],
            vec![agent("same", "https://y.test/rpc", "")],
        ))
        .is_ok());
    }

    #[test]
    fn urls_must_be_absolute_http_without_userinfo() {
        for bad in ["", "/mcp", "ftp://x.test/mcp", "https://user:pw@x.test/mcp"] {
            let s = server("s1", bad);
            let error = validate(&snapshot(vec![s], vec![])).unwrap_err();
            assert!(error.reason.contains("url must be an absolute"), "{bad:?}");
        }
        // An origin without a path is a valid URL.
        assert!(validate(&snapshot(vec![server("s1", "https://x.test")], vec![])).is_ok());
        let error = validate(&snapshot(
            vec![],
            vec![agent("a1", "https://x.test/rpc", "user@x.test/card")],
        ))
        .unwrap_err();
        assert!(error.reason.contains("card_url must be an absolute"));
    }

    #[test]
    fn params_must_be_a_json_object_of_at_most_16_kib() {
        let object = |json: &str| -> PluginRef {
            PluginRef {
                plugin: "hmac".into(),
                params_json: json.into(),
            }
        };
        assert!(compile_plugin(&object(""), "test").is_ok());
        assert!(compile_plugin(&object("{}"), "test").is_ok());
        assert!(compile_plugin(&object(r#"{"key":"v"}"#), "test").is_ok());
        for bad in ["[]", "null", "\"text\"", "{"] {
            assert!(compile_plugin(&object(bad), "test").is_err(), "{bad:?}");
        }
        let at_limit = format!(r#"{{"k":"{}"}}"#, "x".repeat(MAX_PARAMS_BYTES - 8));
        assert_eq!(at_limit.len(), MAX_PARAMS_BYTES);
        assert!(compile_plugin(&object(&at_limit), "test").is_ok());
        let over = format!(r#"{{"k":"{}"}}"#, "x".repeat(MAX_PARAMS_BYTES));
        assert!(compile_plugin(&object(&over), "test").is_err());
        // Nested values are hashed canonically: key order does not change the digest.
        let a = compile_plugin(&object(r#"{"a":1,"b":2}"#), "test").unwrap();
        let b = compile_plugin(&object(r#"{"b":2,"a":1}"#), "test").unwrap();
        assert_eq!(a.digest, b.digest);
        assert_ne!(
            a.digest,
            compile_plugin(&object(r#"{"a":1,"b":3}"#), "test")
                .unwrap()
                .digest
        );
    }

    #[test]
    fn plugin_names_follow_the_manifest_regex() {
        let too_long = "a".repeat(64);
        for bad in ["", "Hmac", "h-mac", "1hmac", too_long.as_str()] {
            assert!(!valid_plugin_name(bad), "{bad:?}");
        }
        assert!(valid_plugin_name("hmac_signature"));
        let snapshot = snapshot(
            vec![McpServer {
                auth: Some(UpstreamAuth {
                    plugin: Some(PluginRef {
                        plugin: "Hmac".into(),
                        params_json: String::new(),
                    }),
                    secret_ref: String::new(),
                    secret_ciphertext: Vec::new(),
                }),
                ..server("s1", "https://x.test/mcp")
            }],
            vec![],
        );
        let error = validate(&snapshot).unwrap_err();
        assert!(error.reason.contains("plugin name does not match"));
    }

    #[test]
    fn static_bearer_opens_ciphertext_and_env_refs() {
        const SECRET: &str = "test-encryption-key-0123456789abcdef";
        let sealed = seal(SECRET, "sk-auth-123");
        let server_with = |auth: UpstreamAuth| McpServer {
            auth: Some(auth),
            ..server("s1", "https://x.test/mcp")
        };
        let mut bearer = server_with(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "static_bearer".into(),
                params_json: String::new(),
            }),
            secret_ref: String::new(),
            secret_ciphertext: sealed.clone(),
        });
        let targets = Targets::compile(
            &snapshot(vec![std::mem::take(&mut bearer)], vec![]),
            Some(SECRET),
        )
        .unwrap();
        let auth = &targets.server("s1").unwrap().auth;
        assert_eq!(auth.plugin_name(), "static_bearer");
        let header = auth.authorization().unwrap();
        assert_eq!(header.as_bytes(), b"Bearer sk-auth-123");
        assert!(header.is_sensitive());
        assert!(!format!("{auth:?}").contains("sk-auth-123"));
        // The wrong key never yields a token.
        let mut server_cipher = server_with(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "static_bearer".into(),
                params_json: String::new(),
            }),
            secret_ref: String::new(),
            secret_ciphertext: sealed.clone(),
        });
        let error = Targets::compile(
            &snapshot(vec![std::mem::take(&mut server_cipher)], vec![]),
            Some("another-key-0123456789abcdef012345"),
        )
        .unwrap_err();
        assert!(error.reason.contains("does not open"));
        assert!(!error.reason.contains("sk-auth-123"));
        // No key at all.
        let mut server_nokey = server_with(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "static_bearer".into(),
                params_json: String::new(),
            }),
            secret_ref: String::new(),
            secret_ciphertext: sealed,
        });
        let error = Targets::compile(
            &snapshot(vec![std::mem::take(&mut server_nokey)], vec![]),
            None,
        )
        .unwrap_err();
        assert!(error.reason.contains("not set"));
        // From the environment, like Upstream.api_key_ref.
        std::env::set_var("LUMEWARP_TEST_MCP_AUTH_TOKEN", "env-token");
        let mut server_env = server_with(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "static_bearer".into(),
                params_json: String::new(),
            }),
            secret_ref: "LUMEWARP_TEST_MCP_AUTH_TOKEN".into(),
            secret_ciphertext: Vec::new(),
        });
        let targets = Targets::compile(
            &snapshot(vec![std::mem::take(&mut server_env)], vec![]),
            None,
        )
        .unwrap();
        assert_eq!(
            targets
                .server("s1")
                .unwrap()
                .auth
                .authorization()
                .unwrap()
                .as_bytes(),
            b"Bearer env-token"
        );
        // A missing variable is INVALID_CONFIG, not a silent no-auth.
        let mut server_missing = server_with(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "static_bearer".into(),
                params_json: String::new(),
            }),
            secret_ref: "LUMEWARP_TEST_MCP_AUTH_TOKEN_MISSING".into(),
            secret_ciphertext: Vec::new(),
        });
        let error = Targets::compile(
            &snapshot(vec![std::mem::take(&mut server_missing)], vec![]),
            None,
        )
        .unwrap_err();
        assert!(error.reason.contains("is not set"));
    }

    #[test]
    fn auth_settings_are_checked() {
        let with_auth = |plugin: &str, params: &str, secret_ref: &str, ciphertext: Vec<u8>| {
            snapshot(
                vec![McpServer {
                    auth: Some(UpstreamAuth {
                        plugin: Some(PluginRef {
                            plugin: plugin.into(),
                            params_json: params.into(),
                        }),
                        secret_ref: secret_ref.into(),
                        secret_ciphertext: ciphertext,
                    }),
                    ..server("s1", "https://x.test/mcp")
                }],
                vec![],
            )
        };
        assert!(validate(&with_auth("none", "", "", Vec::new())).is_ok());
        assert!(validate(&with_auth("none", "{}", "", Vec::new())).is_ok());
        assert!(validate(&with_auth("passthrough", "", "", Vec::new())).is_ok());
        for (plugin, params, secret_ref, ciphertext) in [
            ("none", r#"{"a":1}"#, "", Vec::new()),
            ("passthrough", "", "SOME_ENV", Vec::new()),
            ("static_bearer", "", "", Vec::new()),
            ("static_bearer", r#"{"a":1}"#, "", Vec::new()),
            ("static_bearer", "", "A", vec![1, 2, 3]),
            ("custom_auth", "", "SOME_ENV", Vec::new()),
        ] {
            assert!(
                validate(&with_auth(plugin, params, secret_ref, ciphertext)).is_err(),
                "{plugin} {params} {secret_ref}"
            );
        }
        // A WASM auth plugin keeps its params and needs no secret.
        let targets = Targets::compile(
            &with_auth("hmac_signature", r#"{"skew":5}"#, "", Vec::new()),
            None,
        )
        .unwrap();
        let plugin = targets.server("s1").unwrap().auth.plugin().unwrap();
        assert_eq!(plugin.name.as_ref(), "hmac_signature");
        assert_eq!(plugin.params_json.as_ref(), r#"{"skew":5}"#);
    }

    #[test]
    fn approved_definitions_need_known_kind_name_and_32_byte_hash() {
        let with_approved = |approved: Vec<McpDefinition>| {
            snapshot(
                vec![McpServer {
                    approved,
                    ..server("s1", "https://x.test/mcp")
                }],
                vec![],
            )
        };
        assert!(validate(&with_approved(vec![approved(
            McpDefinitionKind::Tool,
            "echo",
            1
        )]))
        .is_ok());
        for bad in [
            McpDefinition {
                kind: 0,
                name: "echo".into(),
                hash: hash(1),
            },
            McpDefinition {
                kind: 9,
                name: "echo".into(),
                hash: hash(1),
            },
            McpDefinition {
                kind: McpDefinitionKind::Tool as i32,
                name: String::new(),
                hash: hash(1),
            },
            McpDefinition {
                kind: McpDefinitionKind::Tool as i32,
                name: "x".repeat(MAX_NAME_BYTES + 1),
                hash: hash(1),
            },
            McpDefinition {
                kind: McpDefinitionKind::Tool as i32,
                name: "echo".into(),
                hash: vec![1; 31],
            },
            McpDefinition {
                kind: McpDefinitionKind::Tool as i32,
                name: "echo".into(),
                hash: Vec::new(),
            },
        ] {
            assert!(
                validate(&with_approved(vec![bad.clone()])).is_err(),
                "{bad:?}"
            );
        }
        let mut too_many = Vec::new();
        for i in 0..=MAX_APPROVED_DEFINITIONS {
            let mut name = format!("t{i}");
            name.truncate(MAX_NAME_BYTES);
            too_many.push(approved(McpDefinitionKind::Tool, &name, (i % 251) as u8));
        }
        assert!(validate(&with_approved(too_many)).is_err());
    }

    #[test]
    fn approved_lookups_by_kind_name_and_hash() {
        let definitions = vec![
            approved(McpDefinitionKind::Tool, "echo", 1),
            approved(McpDefinitionKind::Tool, "echo", 2),
            approved(McpDefinitionKind::Tool, "add", 3),
            approved(McpDefinitionKind::Prompt, "greet", 4),
        ];
        let targets = Targets::compile(
            &snapshot(
                vec![McpServer {
                    approved: definitions,
                    review_mode: McpReviewMode::Tofu as i32,
                    ..server("s1", "https://x.test/mcp")
                }],
                vec![],
            ),
            None,
        )
        .unwrap();
        let set = targets.server("s1").unwrap().approved();
        assert_eq!(set.len(), 4);
        assert!(set.accepts(McpDefinitionKind::Tool, "echo", &[1u8; 32]));
        assert!(set.accepts(McpDefinitionKind::Tool, "echo", &[2u8; 32]));
        assert!(!set.accepts(McpDefinitionKind::Tool, "echo", &[3u8; 32]));
        // Kinds do not mix.
        assert!(!set.accepts(McpDefinitionKind::Prompt, "echo", &[1u8; 32]));
        assert!(set.has_name(McpDefinitionKind::Tool, "echo"));
        assert!(!set.has_name(McpDefinitionKind::Tool, "greet"));
        let mut tools: Vec<_> = set.names(McpDefinitionKind::Tool).collect();
        tools.sort();
        assert_eq!(tools, ["add", "echo"]);
        let prompts: Vec<_> = set.names(McpDefinitionKind::Prompt).collect();
        assert_eq!(prompts, ["greet"]);
        // Duplicates are counted once and keep both hashes.
        let dup = Targets::compile(
            &snapshot(
                vec![McpServer {
                    approved: vec![
                        approved(McpDefinitionKind::Tool, "echo", 1),
                        approved(McpDefinitionKind::Tool, "echo", 1),
                    ],
                    ..server("s1", "https://x.test/mcp")
                }],
                vec![],
            ),
            None,
        )
        .unwrap();
        assert_eq!(dup.server("s1").unwrap().approved().len(), 1);
        assert!(!dup.server("s1").unwrap().approved().is_empty());
    }

    #[test]
    fn middleware_is_optional_and_carries_its_plugin() {
        let with = |plugin: Option<PluginRef>| {
            snapshot(
                vec![],
                vec![pb::A2aAgent {
                    middleware: plugin.map(|plugin| pb::PluginRef {
                        plugin: plugin.plugin,
                        params_json: plugin.params_json,
                    }),
                    ..agent("a1", "https://x.test/rpc", "")
                }],
            )
        };
        let targets = Targets::compile(&with(None), None).unwrap();
        assert!(targets.agent("a1").unwrap().middleware.is_none());
        let targets = Targets::compile(
            &with(Some(PluginRef {
                plugin: "strip_artifacts".into(),
                params_json: r#"{"mime":["image/png"]}"#.into(),
            })),
            None,
        )
        .unwrap();
        let middleware = targets.agent("a1").unwrap().middleware.as_ref().unwrap();
        assert_eq!(middleware.name.as_ref(), "strip_artifacts");
        assert_eq!(middleware.params_json.as_ref(), r#"{"mime":["image/png"]}"#);
        let error = validate(&with(Some(PluginRef {
            plugin: "strip-artifacts".into(),
            params_json: String::new(),
        })))
        .unwrap_err();
        assert!(error.reason.contains("plugin name"));
        // An empty plugin name means "no middleware", which the proto allows.
        assert!(validate(&with(Some(PluginRef {
            plugin: String::new(),
            params_json: String::new(),
        })))
        .is_ok());
    }

    #[test]
    fn card_json_is_bounded_and_must_be_an_object() {
        for (card, ok) in [
            (String::new(), true),
            (r#"{"name":"a1"}"#.to_owned(), true),
            ("[]".to_owned(), false),
            ("{".to_owned(), false),
            (
                format!(r#"{{"x":"{}"}}"#, "y".repeat(MAX_CARD_BYTES)),
                false,
            ),
        ] {
            let snapshot = snapshot(
                vec![],
                vec![pb::A2aAgent {
                    card_json: card,
                    ..agent("a1", "https://x.test/rpc", "")
                }],
            );
            assert_eq!(validate(&snapshot).is_ok(), ok);
        }
    }

    #[test]
    fn passthrough_and_no_auth_have_no_header() {
        let server_with = |plugin: &str| McpServer {
            auth: Some(UpstreamAuth {
                plugin: Some(PluginRef {
                    plugin: plugin.into(),
                    params_json: String::new(),
                }),
                secret_ref: String::new(),
                secret_ciphertext: Vec::new(),
            }),
            ..server("s1", "https://x.test/mcp")
        };
        let targets =
            Targets::compile(&snapshot(vec![server_with("passthrough")], vec![]), None).unwrap();
        let auth = &targets.server("s1").unwrap().auth;
        assert_eq!(auth.plugin_name(), "passthrough");
        assert!(auth.authorization().is_none());
        assert!(auth.plugin().is_none());
        assert!(matches!(auth, Auth::Passthrough));
        let targets = Targets::compile(&snapshot(vec![server_with("none")], vec![]), None).unwrap();
        let auth = &targets.server("s1").unwrap().auth;
        assert_eq!(auth.plugin_name(), "none");
        assert!(auth.authorization().is_none());
        assert!(matches!(auth, Auth::None));
        // An absent auth message means the same as `none`.
        let targets = Targets::compile(
            &snapshot(vec![server("s1", "https://x.test/mcp")], vec![]),
            None,
        )
        .unwrap();
        assert!(matches!(targets.server("s1").unwrap().auth, Auth::None));
    }

    #[test]
    fn plugin_settings_text_is_kept_for_configure() {
        let plugin = compile_plugin(
            &PluginRef {
                plugin: "hmac_signature".into(),
                params_json: String::new(),
            },
            "test",
        )
        .unwrap();
        assert_eq!(plugin.params_json.as_ref(), "{}");
        let plugin = compile_plugin(
            &PluginRef {
                plugin: "hmac_signature".into(),
                params_json: r#"{"skew":5,"key":"abc"}"#.into(),
            },
            "test",
        )
        .unwrap();
        assert_eq!(plugin.params_json.as_ref(), r#"{"skew":5,"key":"abc"}"#);
    }

    #[test]
    fn capabilities_list_what_the_entities_need() {
        let mut s = server("s1", "https://x.test/mcp");
        s.auth = Some(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "hmac_signature".into(),
                params_json: String::new(),
            }),
            secret_ref: String::new(),
            secret_ciphertext: Vec::new(),
        });
        let mut agent = agent("a1", "https://y.test/rpc", "");
        agent.auth = Some(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "none".into(),
                params_json: String::new(),
            }),
            secret_ref: String::new(),
            secret_ciphertext: Vec::new(),
        });
        agent.middleware = Some(PluginRef {
            plugin: "strip_artifacts".into(),
            params_json: String::new(),
        });
        let caps = capabilities(&snapshot(vec![s], vec![agent]));
        assert_eq!(
            caps,
            [
                "a2a",
                "a2a_middleware:strip_artifacts",
                "auth:hmac_signature",
                "auth:none",
                "mcp",
            ]
        );
        assert!(capabilities(&snapshot(vec![], vec![])).is_empty());
    }

    #[test]
    fn sorted_listings_and_qualified_tool_names() {
        let targets = Targets::compile(
            &snapshot(
                vec![
                    server("zeta", "https://x.test/mcp"),
                    server("alpha", "https://x.test/mcp"),
                    server("mid", "https://x.test/mcp"),
                ],
                vec![
                    agent("b", "https://x.test/rpc", ""),
                    agent("a", "https://x.test/rpc", ""),
                ],
            ),
            None,
        )
        .unwrap();
        let names: Vec<_> = targets.servers().map(|s| s.name.as_ref()).collect();
        assert_eq!(names, ["alpha", "mid", "zeta"]);
        let names: Vec<_> = targets.agents().map(|a| a.name.as_ref()).collect();
        assert_eq!(names, ["a", "b"]);
        assert_eq!(
            targets.server("alpha").unwrap().qualified_tool("echo"),
            "alpha__echo"
        );
    }

    #[test]
    fn sealed_secret_round_trip_and_envelope_rules() {
        const SECRET: &str = "test-encryption-key-0123456789abcdef";
        let sealed = seal(SECRET, "token");
        assert_eq!(open_secret(Some(SECRET), &sealed).unwrap(), "token");
        let mut tampered = sealed.clone();
        *tampered.last_mut().unwrap() ^= 1;
        assert_eq!(
            open_secret(Some(SECRET), &tampered),
            Err(SecretError::Mismatch)
        );
        let mut versioned = sealed.clone();
        versioned[0] = 2;
        assert_eq!(
            open_secret(Some(SECRET), &versioned),
            Err(SecretError::Format)
        );
        assert_eq!(
            open_secret(Some(SECRET), &sealed[..10]),
            Err(SecretError::Format)
        );
        assert_eq!(open_secret(None, &sealed), Err(SecretError::NoKey));
    }
}

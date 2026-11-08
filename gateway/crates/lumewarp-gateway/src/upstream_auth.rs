//! Outbound authentication to MCP servers and A2A agents.
//!
//! The MCP and A2A proxies build one upstream request and call
//! [`authorize_mcp`] or [`authorize_a2a`]; it applies the header policy of plan
//! L9 in one place and adds the credentials of the target's configured auth:
//!
//! | `Auth` | Upstream credentials |
//! | --- | --- |
//! | `none` | nothing |
//! | `static_bearer` | `Authorization: Bearer <secret>`, opened when the snapshot was applied (`targets.rs`) |
//! | `passthrough` | the client's `Authorization`, unchanged; absent stays absent |
//! | `Plugin` | whatever a WASM `upstream-auth` plugin returns for this exact request |
//!
//! Client credentials never reach an upstream or a plugin: every call removes
//! `X-Lumewarp-Key`, `Cookie` and `Proxy-Authorization`, and `Authorization`
//! unless `passthrough` is selected. What a plugin returns is validated before
//! it is sent: a hop-by-hop header, one of the gateway's own headers ([`HOST_HEADERS`]),
//! a header in Lumewarp's `X-Lumewarp-*` namespace (except the sample scheme's
//! `X-Lumewarp-Signature`) or an invalid name/value refuses the request, so a
//! plugin can neither break framing nor put `X-Lumewarp-Key` back.
//!
//! A WASM adapter failure — `configure`, a plugin error, a trap, or the 2 s
//! `authorize` deadline — is [`Failure`]: the client gets 502 and the call
//! counts as an auth failure (`docs/plugins.md`, "Sandbox"). The detail is
//! logged, never returned: plugin error text can contain its settings.
//!
//! [`precompile`] is the apply-time hook: call it from `Targets::compile` so a
//! `configure` error refuses the version instead of failing the first request.
//! Configured authenticators are reused across snapshots whose plugin name,
//! settings and destination did not change, so a plugin's token cache survives
//! a snapshot swap (plan P9).
// The MCP/A2A proxies (W2) call this module; until they land the binary has no
// caller, which `-D warnings` would reject.
#![allow(dead_code)]

use hyper::{
    header::{HeaderMap, HeaderName, HeaderValue, AUTHORIZATION},
    Method, Uri,
};
use lumewarp_plugin_host::{
    authenticator::{self, Error as PluginError, Spec, Traffic},
    fetch,
};

use crate::{
    snapshot::ApplyError,
    targets::{A2aTarget, Auth, McpTarget, Plugin, Targets},
};

/// Client credentials that are removed from every outbound request (plan L9).
pub const CLIENT_CREDENTIALS: [&str; 4] = [
    "x-lumewarp-key",
    "cookie",
    "proxy-authorization",
    "authorization",
];

/// Largest number of headers one auth plugin may add.
pub const MAX_PLUGIN_HEADERS: usize = 64;

/// Largest total size of the headers one auth plugin may add.
pub const MAX_PLUGIN_HEADER_BYTES: usize = 16 * 1024;

/// Headers the gateway owns on every outbound MCP/A2A request: framing (`Host`,
/// `Content-Length`, the hop-by-hop set), session and protocol routing
/// (`Mcp-Session-Id`, `MCP-Protocol-Version`, `A2A-Version`, `Last-Event-ID`),
/// and the client's `Cookie`, which plan L9 removes from every upstream
/// request. A plugin may read them; setting one is an auth failure.
pub const HOST_HEADERS: [&str; 7] = [
    "host",
    "content-length",
    "cookie",
    "mcp-session-id",
    "mcp-protocol-version",
    "a2a-version",
    "last-event-id",
];

/// Why outbound authentication failed. The proxy answers 502.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failure {
    /// The plugin is not loaded, or its settings do not configure it.
    Configure,
    /// The plugin returned an error.
    Plugin,
    /// The plugin missed its `authorize` deadline.
    Timeout,
    /// The plugin trapped.
    Trap,
    /// The plugin returned a header the gateway must not send.
    Header,
}

impl Failure {
    /// A fixed label for logs and spans; never plugin text.
    pub fn label(self) -> &'static str {
        match self {
            Self::Configure => "configure",
            Self::Plugin => "plugin",
            Self::Timeout => "timeout",
            Self::Trap => "trap",
            Self::Header => "header",
        }
    }
}

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

/// Applies the header policy and an MCP server's outbound credentials.
///
/// `headers` are the headers the gateway intends to send upstream; the client's
/// credentials may still be among them. On return they are final.
pub async fn authorize_mcp(
    target: &McpTarget,
    url: &Uri,
    method: &Method,
    body: &[u8],
    headers: &mut HeaderMap,
) -> Result<(), Failure> {
    apply(
        Traffic::Mcp,
        &target.id,
        &target.name,
        &target.url,
        &target.auth,
        url,
        method,
        body,
        headers,
    )
    .await
}

/// Applies the header policy and an A2A agent's outbound credentials. `url` is
/// the URL actually being requested: the JSON-RPC endpoint or the agent card.
pub async fn authorize_a2a(
    target: &A2aTarget,
    url: &Uri,
    method: &Method,
    body: &[u8],
    headers: &mut HeaderMap,
) -> Result<(), Failure> {
    apply(
        Traffic::A2a,
        &target.id,
        &target.name,
        &target.url,
        &target.auth,
        url,
        method,
        body,
        headers,
    )
    .await
}

/// Configures outbound auth for every MCP server and A2A agent of one compiled
/// configuration, reusing unchanged targets. Call from `RoutingTable::compile`,
/// after validation: a refused plugin then NACKs the version (`INVALID_CONFIG`)
/// instead of failing the first request to that target.
///
/// The result is **staged**: nothing serves until [`publish`] is called, which
/// a later step can still skip when the version is refused (m1). A `configure`
/// that never returns is bounded by [`lumewarp_plugin_host::CONFIGURE_TIMEOUT`]
/// and refuses the version too (M4). This is apply-time work only.
pub fn precompile(targets: &Targets) -> Result<authenticator::Staged, ApplyError> {
    let mut specs = Vec::new();
    for server in targets.servers() {
        if let Auth::Plugin(plugin) = &server.auth {
            if !loaded(plugin) {
                continue;
            }
            specs.push(spec_of(
                Traffic::Mcp,
                &server.id,
                &server.name,
                &server.url,
                plugin,
            ));
        }
    }
    for agent in targets.agents() {
        if let Auth::Plugin(plugin) = &agent.auth {
            if !loaded(plugin) {
                continue;
            }
            specs.push(spec_of(
                Traffic::A2a,
                &agent.id,
                &agent.name,
                &agent.url,
                plugin,
            ));
        }
    }
    authenticator::global().stage(&specs).map_err(|refused| {
        // The plugin's own configure text stays out of the NACK, which the
        // control plane stores and shows; the log has it.
        let reason = match &refused.source {
            PluginError::Missing(plugin) => format!("auth plugin {plugin:?} is not loaded"),
            PluginError::Params(detail) => format!("outbound auth: {detail}"),
            PluginError::ConfigureTimeout => "configure timed out".to_owned(),
            other => {
                tracing::warn!(
                    target = %refused.target,
                    error = %other,
                    "outbound auth plugin could not be configured"
                );
                return ApplyError::invalid(format!(
                    "outbound auth for {:?} could not be configured (see the gateway log)",
                    refused.target
                ));
            }
        };
        tracing::warn!(target = %refused.target, %reason, "outbound auth refused");
        ApplyError::invalid(format!("outbound auth for {:?}: {reason}", refused.target))
    })
}

/// Makes a staged set the one serving. Called once a version is accepted; a
/// refused version never reaches it, so the previous set keeps serving (m1).
pub fn publish(staged: authenticator::Staged) {
    authenticator::global().publish(staged);
}

fn spec_of(traffic: Traffic, id: &str, name: &str, url: &Uri, plugin: &Plugin) -> Spec {
    Spec {
        traffic,
        id: target_key(id, name).to_owned(),
        name: name.to_owned(),
        url: url.to_string(),
        plugin: plugin.name.to_string(),
        params_json: plugin.params_json.to_string(),
        digest: plugin.digest,
    }
}

/// Whether this gateway loaded the plugin. A snapshot naming a plugin it does
/// not have is refused by the capability check in `snapshot::validate`
/// (`UNSUPPORTED_FEATURE`), so `precompile` skips it rather than repeating that
/// error: a `configure` failure is what must refuse the version here.
fn loaded(plugin: &Plugin) -> bool {
    lumewarp_plugin_host::global()
        .get(authenticator::KIND, &plugin.name)
        .is_some()
}

/// The auth cache identity of a target. A standalone YAML snapshot may leave the
/// id empty; the name is unique per kind and stands in for it.
fn target_key<'a>(id: &'a str, name: &'a str) -> &'a str {
    if id.is_empty() {
        name
    } else {
        id
    }
}

#[allow(clippy::too_many_arguments)]
async fn apply(
    traffic: Traffic,
    id: &str,
    name: &str,
    target_url: &Uri,
    auth: &Auth,
    url: &Uri,
    method: &Method,
    body: &[u8],
    headers: &mut HeaderMap,
) -> Result<(), Failure> {
    let credentials = match auth {
        Auth::None => Vec::new(),
        Auth::Passthrough => headers
            .get(AUTHORIZATION)
            .cloned()
            .map(|value| vec![(AUTHORIZATION, value)])
            .unwrap_or_default(),
        Auth::StaticBearer { authorization } => {
            vec![(AUTHORIZATION, authorization.clone())]
        }
        Auth::Plugin(plugin) => {
            match plugin_credentials(
                traffic, id, name, target_url, plugin, url, method, body, headers,
            )
            .await
            {
                Ok(credentials) => credentials,
                Err(failure) => {
                    // Nothing will be sent, but the client's credentials are
                    // stripped anyway so a caller cannot accidentally forward
                    // them after a failed authorization.
                    outbound(headers, Vec::new());
                    return Err(failure);
                }
            }
        }
    };
    outbound(headers, credentials);
    Ok(())
}

/// The one header policy of every outbound request (plan L9): drop the client's
/// credentials, then add the credential this auth mode supplies.
fn outbound(headers: &mut HeaderMap, credentials: Vec<(HeaderName, HeaderValue)>) {
    for name in CLIENT_CREDENTIALS {
        headers.remove(name);
    }
    for (name, value) in credentials {
        headers.insert(name, value);
    }
}

/// Asks the target's WASM plugin for credentials. The plugin sees the request as
/// it will be sent — except for the client's credentials, which it never sees.
#[allow(clippy::too_many_arguments)]
async fn plugin_credentials(
    traffic: Traffic,
    id: &str,
    name: &str,
    target_url: &Uri,
    plugin: &Plugin,
    url: &Uri,
    method: &Method,
    body: &[u8],
    headers: &HeaderMap,
) -> Result<Vec<(HeaderName, HeaderValue)>, Failure> {
    let target_url = target_url.to_string();
    let key = target_key(id, name);
    let configured = match authenticator::get(traffic, key) {
        Some(existing)
            if existing.matches_request(name, &target_url, &plugin.name, &plugin.digest) =>
        {
            existing
        }
        _ => authenticator::global()
            .get_or_configure(&Spec {
                traffic,
                id: key.to_owned(),
                name: name.to_owned(),
                url: target_url,
                plugin: plugin.name.to_string(),
                params_json: plugin.params_json.to_string(),
                digest: plugin.digest,
            })
            .map_err(|error| {
                tracing::warn!(%name, plugin = %plugin.name, error = %error, "outbound auth could not be configured");
                match error {
                    PluginError::Missing(_)
                    | PluginError::Params(_)
                    | PluginError::Configure(_)
                    | PluginError::ConfigureTimeout => Failure::Configure,
                    PluginError::Timeout => Failure::Timeout,
                    PluginError::Trap => Failure::Trap,
                    PluginError::Plugin(_) | PluginError::Canceled => Failure::Plugin,
                }
            })?,
    };
    let url = url.to_string();
    let request = authenticator::Outbound {
        method: method.as_str(),
        url: &url,
        headers: headers
            .iter()
            .filter(|(name, _)| !is_client_credential(name.as_str()))
            .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
            .collect(),
        body,
    };
    let result = configured
        .authorize(request, plugin.timeout)
        .await
        .map_err(|error| {
        let failure = match &error {
            PluginError::Timeout => Failure::Timeout,
            PluginError::Trap => Failure::Trap,
            PluginError::Missing(_)
            | PluginError::Params(_)
            | PluginError::Configure(_)
            | PluginError::ConfigureTimeout => Failure::Configure,
            PluginError::Plugin(_) | PluginError::Canceled => Failure::Plugin,
        };
        // The guest's message can contain its settings: it goes to the log at
        // debug level only, never into the response.
        tracing::warn!(%name, plugin = %plugin.name, kind = failure.label(), "outbound auth failed");
        tracing::debug!(%name, plugin = %plugin.name, error = %error, "outbound auth failure detail");
            failure
        })?;
    plugin_headers(&result.headers)
}

fn is_client_credential(name: &str) -> bool {
    CLIENT_CREDENTIALS
        .iter()
        .any(|credential| name.eq_ignore_ascii_case(credential))
}

/// Validates the headers a plugin returned for the upstream request (plan L9).
///
/// Refused: hop-by-hop headers (including anything a returned `Connection`
/// names), the gateway's own headers ([`HOST_HEADERS`]), anything in the
/// gateway's `X-Lumewarp-*` namespace (plan L9 has no exception), duplicate
/// names, invalid names or values, and more than [`MAX_PLUGIN_HEADERS`] /
/// [`MAX_PLUGIN_HEADER_BYTES`].
fn plugin_headers(raw: &[(String, Vec<u8>)]) -> Result<Vec<(HeaderName, HeaderValue)>, Failure> {
    if raw.len() > MAX_PLUGIN_HEADERS {
        return Err(Failure::Header);
    }
    let hop_by_hop = fetch::hop_by_hop(raw);
    let mut bytes = 0usize;
    let mut out: Vec<(HeaderName, HeaderValue)> = Vec::with_capacity(raw.len());
    for (name, value) in raw {
        bytes += name.len() + value.len();
        if bytes > MAX_PLUGIN_HEADER_BYTES {
            return Err(Failure::Header);
        }
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| Failure::Header)?;
        let lower = name.as_str();
        if hop_by_hop.contains(lower)
            || HOST_HEADERS.contains(&lower)
            || lower.starts_with("x-lumewarp-")
            || out.iter().any(|(existing, _)| *existing == name)
        {
            return Err(Failure::Header);
        }
        let mut value = HeaderValue::from_bytes(value).map_err(|_| Failure::Header)?;
        if name == AUTHORIZATION {
            value.set_sensitive(true);
        }
        out.push((name, value));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    use hyper::header::{CONTENT_TYPE, COOKIE, PROXY_AUTHORIZATION};
    use lumewarp_proto::config::v1 as pb;
    use sha2::{Digest, Sha256};
    use std::{path::Path, sync::OnceLock};

    const BODY: &[u8] = br#"{"jsonrpc":"2.0","id":1,"method":"ping"}"#;

    fn init_plugins() -> bool {
        static READY: OnceLock<bool> = OnceLock::new();
        *READY.get_or_init(|| {
            let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target");
            let production = root.join("plugins/auth");
            let fixtures = root.join("plugins-test/auth");
            if !production.exists() || !fixtures.exists() {
                eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
                return false;
            }
            let mut registry = lumewarp_plugin_host::Registry::default();
            if let Err(error) = registry
                .load_dir(&production, 2)
                .and_then(|()| registry.load_dir(&fixtures, 2))
            {
                eprintln!("SKIP: {error:#}");
                return false;
            }
            if lumewarp_plugin_host::install(registry).is_err() {
                eprintln!("SKIP: plugins already installed");
                return false;
            }
            true
        })
    }

    fn mcp(url: &str, auth: Option<pb::UpstreamAuth>) -> pb::McpServer {
        pb::McpServer {
            id: format!("id-{}", url.replace([':', '/', '.'], "-")),
            name: "s1".into(),
            url: url.into(),
            auth,
            ..Default::default()
        }
    }

    fn a2a(url: &str, auth: Option<pb::UpstreamAuth>) -> pb::A2aAgent {
        pb::A2aAgent {
            id: "agent-1".into(),
            name: "a1".into(),
            url: url.into(),
            auth,
            ..Default::default()
        }
    }

    fn snapshot(servers: Vec<pb::McpServer>, agents: Vec<pb::A2aAgent>) -> pb::Snapshot {
        pb::Snapshot {
            mcp_servers: servers,
            a2a_agents: agents,
            ..Default::default()
        }
    }

    fn builtin(name: &str) -> pb::UpstreamAuth {
        pb::UpstreamAuth {
            plugin: Some(pb::PluginRef {
                plugin: name.into(),
                params_json: String::new(),
                ..Default::default()
            }),
            secret_ref: String::new(),
            secret_ciphertext: Vec::new(),
        }
    }

    fn plugin(name: &str, params: serde_json::Value) -> pb::UpstreamAuth {
        pb::UpstreamAuth {
            plugin: Some(pb::PluginRef {
                plugin: name.into(),
                params_json: params.to_string(),
                ..Default::default()
            }),
            secret_ref: String::new(),
            secret_ciphertext: Vec::new(),
        }
    }

    fn compiled(servers: Vec<pb::McpServer>, agents: Vec<pb::A2aAgent>) -> Targets {
        Targets::compile(&snapshot(servers, agents), None).expect("compile")
    }

    fn client_headers() -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, "Bearer client".parse().unwrap());
        headers.insert("x-lumewarp-key", "lw-key".parse().unwrap());
        headers.insert(COOKIE, "session=1".parse().unwrap());
        headers.insert(PROXY_AUTHORIZATION, "Basic cHJveHk=".parse().unwrap());
        headers.insert(CONTENT_TYPE, "application/json".parse().unwrap());
        headers
    }

    fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
        headers.get(name).map(|value| value.to_str().unwrap())
    }

    fn hmac_sha256(key: &[u8], message: &[u8]) -> String {
        // HMAC-SHA256, the algorithm `bench/mock-upstream` verifies.
        const BLOCK: usize = 64;
        let mut key = if key.len() > BLOCK {
            Sha256::digest(key).to_vec()
        } else {
            key.to_vec()
        };
        key.resize(BLOCK, 0);
        let mut inner = key.iter().map(|byte| byte ^ 0x36).collect::<Vec<_>>();
        inner.extend_from_slice(message);
        let mut outer = key.iter().map(|byte| byte ^ 0x5c).collect::<Vec<_>>();
        outer.extend_from_slice(&Sha256::digest(&inner));
        hex::encode(Sha256::digest(&outer))
    }

    #[tokio::test]
    async fn none_sends_nothing_and_strips_the_client_credentials() {
        let targets = compiled(
            vec![mcp("https://mcp.example.test/mcp", Some(builtin("none")))],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        assert_eq!(header(&headers, "authorization"), None);
        assert_eq!(header(&headers, "x-lumewarp-key"), None);
        assert_eq!(header(&headers, "cookie"), None);
        assert_eq!(header(&headers, "proxy-authorization"), None);
        // Everything else the proxy decided to forward stays.
        assert_eq!(header(&headers, "content-type"), Some("application/json"));
    }

    #[tokio::test]
    async fn passthrough_forwards_the_clients_authorization_only() {
        let targets = compiled(
            vec![mcp(
                "https://mcp.example.test/mcp",
                Some(builtin("passthrough")),
            )],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        assert_eq!(header(&headers, "authorization"), Some("Bearer client"));
        assert_eq!(header(&headers, "x-lumewarp-key"), None);
        assert_eq!(header(&headers, "cookie"), None);
        assert_eq!(header(&headers, "proxy-authorization"), None);
        // Absent upstream credential stays absent: nothing is invented.
        let mut headers = HeaderMap::new();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        assert_eq!(header(&headers, "authorization"), None);
    }

    #[tokio::test]
    async fn static_bearer_replaces_the_clients_authorization() {
        std::env::set_var("LUMEWARP_TEST_STATIC_BEARER", "sk-server-token");
        let mut auth = builtin("static_bearer");
        auth.secret_ref = "LUMEWARP_TEST_STATIC_BEARER".into();
        let targets = compiled(
            vec![mcp("https://mcp.example.test/mcp", Some(auth))],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        assert_eq!(
            header(&headers, "authorization"),
            Some("Bearer sk-server-token")
        );
        assert_eq!(header(&headers, "x-lumewarp-key"), None);
        assert_eq!(header(&headers, "cookie"), None);
        assert_eq!(header(&headers, "proxy-authorization"), None);
        assert!(headers.get(AUTHORIZATION).unwrap().is_sensitive());
    }

    #[tokio::test]
    async fn a_plugin_supplies_the_credentials_it_returns() {
        if !init_plugins() {
            return;
        }
        let targets = compiled(
            vec![mcp(
                "https://mcp.example.test/mcp",
                Some(plugin(
                    "auth_fixture",
                    serde_json::json!({
                        "mode": "fixed",
                        "headers": ["authorization: Bearer plugin", "x-custom: v"],
                    }),
                )),
            )],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        assert_eq!(header(&headers, "authorization"), Some("Bearer plugin"));
        assert_eq!(header(&headers, "x-custom"), Some("v"));
        assert_eq!(header(&headers, "x-lumewarp-key"), None);
        assert_eq!(header(&headers, "cookie"), None);
        // A plugin never saw the client's credentials: its headers are all it
        // could copy, so nothing of the client's survives.
        assert_eq!(header(&headers, "proxy-authorization"), None);
        assert!(headers.get(AUTHORIZATION).unwrap().is_sensitive());
    }

    #[tokio::test]
    async fn a_plugin_never_sees_the_clients_credentials() {
        if !init_plugins() {
            return;
        }
        for (name, expected) in [
            ("x-lumewarp-key", "absent"),
            ("authorization", "absent"),
            ("cookie", "absent"),
            ("proxy-authorization", "absent"),
            ("content-type", "application/json"),
        ] {
            let targets = compiled(
                vec![mcp(
                    "https://mcp.example.test/mcp",
                    Some(plugin(
                        "auth_fixture",
                        serde_json::json!({"mode": "echo", "echo_header": name}),
                    )),
                )],
                vec![],
            );
            let target = targets.server("s1").unwrap();
            let mut headers = client_headers();
            authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
                .await
                .unwrap();
            assert_eq!(header(&headers, "x-echo"), Some(expected), "{name}");
        }
    }

    #[tokio::test]
    async fn a_forbidden_plugin_header_is_an_auth_failure() {
        if !init_plugins() {
            return;
        }
        for forbidden in [
            "x-lumewarp-key: lw-key",
            "x-lumewarp-request-id: spoofed",
            "x-lumewarp-anything: v",
            "host: evil.example",
            "content-length: 0",
            "cookie: session=stolen",
            "mcp-session-id: other-session",
            "mcp-protocol-version: 2025-06-18",
            "a2a-version: 2.0",
            "last-event-id: 7",
            "connection: keep-alive",
            "transfer-encoding: chunked",
            "x: a\nb",
        ] {
            let targets = compiled(
                vec![mcp(
                    "https://mcp.example.test/mcp",
                    Some(plugin(
                        "auth_fixture",
                        serde_json::json!({
                            "mode": "fixed",
                            "headers": [forbidden],
                        }),
                    )),
                )],
                vec![],
            );
            let target = targets.server("s1").unwrap();
            let mut headers = client_headers();
            let error = authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
                .await
                .unwrap_err();
            assert_eq!(error, Failure::Header, "{forbidden:?}");
        }
    }

    #[tokio::test]
    async fn a_plugin_naming_a_header_in_connection_is_refused() {
        if !init_plugins() {
            return;
        }
        let targets = compiled(
            vec![mcp(
                "https://mcp.example.test/mcp",
                Some(plugin(
                    "auth_fixture",
                    serde_json::json!({
                        "mode": "fixed",
                        "headers": ["connection: x-nominated", "x-nominated: v"],
                    }),
                )),
            )],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = HeaderMap::new();
        assert_eq!(
            authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
                .await
                .unwrap_err(),
            Failure::Header
        );
    }

    #[tokio::test]
    async fn a_plugin_error_is_a_failure_not_a_silent_no_auth() {
        if !init_plugins() {
            return;
        }
        let targets = compiled(
            vec![mcp(
                "https://mcp.example.test/mcp",
                Some(plugin("auth_fixture", serde_json::json!({"mode": "error"}))),
            )],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        let error = authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap_err();
        assert_eq!(error, Failure::Plugin);
        // Nothing half-applied: the client's credentials are gone and no
        // plugin header was added.
        assert_eq!(header(&headers, "authorization"), None);
    }

    #[tokio::test]
    async fn hmac_signature_output_verifies_like_the_mock() {
        if !init_plugins() {
            return;
        }
        std::env::set_var("LUMEWARP_PLUGIN_SECRET_HMAC_FIXTURE", "lumewarp-dev-secret");
        let targets = compiled(
            vec![mcp(
                "https://mcp.example.test/mcp",
                Some(plugin(
                    "hmac_signature",
                    serde_json::json!({"secret_name": "HMAC_FIXTURE"}),
                )),
            )],
            vec![],
        );
        let target = targets.server("s1").unwrap();
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, BODY, &mut headers)
            .await
            .unwrap();
        let signature = header(&headers, "x-hmac-signature").expect("signed");
        let (time, mac) = signature.split_once(",v1=").expect("t=...,v1=...");
        let time: u64 = time.strip_prefix("t=").expect("t=").parse().unwrap();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        assert!(now.abs_diff(time) < 60_000, "the signature is fresh");
        // Exactly the canonical string of bench/mock-upstream/HMAC.md, over the
        // bytes actually sent upstream.
        let body_hash = hex::encode(Sha256::digest(BODY));
        let canonical = format!("{time}.POST./mcp.{body_hash}");
        assert_eq!(
            mac,
            hmac_sha256(b"lumewarp-dev-secret", canonical.as_bytes())
        );
        // Body-dependent results are never cached.
        let mut headers = client_headers();
        authorize_mcp(target, &target.url, &Method::POST, b"{}", &mut headers)
            .await
            .unwrap();
        let second = header(&headers, "x-hmac-signature").unwrap();
        assert_ne!(signature, second);
    }

    #[tokio::test]
    async fn an_a2a_card_fetch_uses_the_agent_credentials_and_target_url() {
        if !init_plugins() {
            return;
        }
        let targets = compiled(
            vec![],
            vec![a2a(
                "https://agent.example.test/rpc",
                Some(plugin(
                    "auth_fixture",
                    serde_json::json!({"mode": "fixed", "headers": ["authorization: Bearer agent"]}),
                )),
            )],
        );
        let target = targets.agent("a1").unwrap();
        let mut headers = HeaderMap::new();
        authorize_a2a(target, &target.card_url, &Method::GET, b"", &mut headers)
            .await
            .unwrap();
        assert_eq!(header(&headers, "authorization"), Some("Bearer agent"));
    }

    #[tokio::test]
    async fn precompile_accepts_good_settings_and_refuses_bad_ones() {
        if !init_plugins() {
            return;
        }
        let mut second = mcp("https://other.example.test/mcp", Some(builtin("none")));
        second.name = "s2".into();
        let good = compiled(
            vec![
                mcp(
                    "https://mcp.example.test/mcp",
                    Some(plugin(
                        "auth_fixture",
                        serde_json::json!({"mode": "fixed", "cache_ms": 1}),
                    )),
                ),
                second,
            ],
            vec![a2a(
                "https://agent.example.test/rpc",
                Some(builtin("passthrough")),
            )],
        );
        precompile(&good).unwrap();
        // A configure error refuses the configuration with a reason that does
        // not carry the plugin's own text. `Targets::compile` calls the hook,
        // so a bad plugin never reaches a routing table.
        let bad = snapshot(
            vec![mcp(
                "https://bad.example.test/mcp",
                Some(plugin("auth_fixture", serde_json::json!({"mode": "nope"}))),
            )],
            vec![],
        );
        let error = Targets::compile(&bad, None).unwrap_err();
        // The NACK names the target and the rule, never the plugin's text.
        assert!(error.reason.contains("s1"), "{}", error.reason);
        assert!(!error.reason.contains("nope"), "{}", error.reason);
        assert!(!error.reason.contains("unknown mode"), "{}", error.reason);
    }

    #[tokio::test]
    async fn a_target_without_an_id_is_keyed_by_name() {
        let mut target = mcp("https://mcp.example.test/mcp", Some(builtin("none")));
        target.id = String::new();
        target.name = "no-id".into();
        let targets = compiled(vec![target], vec![]);
        let server = targets.server("no-id").unwrap();
        assert_eq!(target_key(&server.id, &server.name), "no-id");
        assert_eq!(target_key("id-1", "s1"), "id-1");
    }
}

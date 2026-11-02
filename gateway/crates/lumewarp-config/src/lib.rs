//! Local gateway settings and the standalone YAML format.
//!
//! The gateway compiles only from the proto `Snapshot`
//! (`proto/lumewarp/config/v1/snapshot.proto`). Standalone YAML is parsed here
//! and converted to a `Snapshot` with the same semantics the control plane
//! produces; structural validation (references, enums, duplicates) happens
//! once, on the snapshot, in the gateway.

use std::{collections::HashSet, net::SocketAddr, path::Path};

use lumewarp_proto::config::v1 as pb;
use serde::{Deserialize, Deserializer};

pub use lumewarp_proto::config::v1::Snapshot;

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("reading {path}: {source}")]
    Io {
        path: String,
        source: std::io::Error,
    },
    #[error("parsing config: {0}")]
    Parse(#[from] serde_yaml::Error),
    #[error("invalid config: {0}")]
    Invalid(String),
}

/// Whether `/v1/*` requests must present a virtual key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthMode {
    /// No key required; every request is anonymous. Standalone only.
    Open,
    /// A valid key is required. Zero keys means every request is rejected.
    RequireKey,
}

impl AuthMode {
    /// Standalone YAML is open iff it configures no keys at all.
    pub fn standalone(snapshot: &Snapshot) -> Self {
        if snapshot.api_keys.is_empty() {
            Self::Open
        } else {
            Self::RequireKey
        }
    }
}

/// Process-local settings that never travel in a snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalSettings {
    pub listen: SocketAddr,
    pub admin_listen: SocketAddr,
    pub shutdown_grace_ms: u64,
    pub header_read_timeout_ms: u64,
    pub body_read_timeout_ms: u64,
    pub retry_budget_percent: u32,
}

/// The standalone YAML document.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub server: Server,
    #[serde(default)]
    pub upstreams: Vec<Upstream>,
    #[serde(default)]
    pub models: Vec<Model>,
    #[serde(default)]
    pub aliases: Vec<Alias>,
    #[serde(default)]
    pub routers: Vec<Router>,
    #[serde(default)]
    pub teams: Vec<Team>,
    #[serde(default)]
    pub keys: Vec<Key>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Server {
    #[serde(default = "default_listen")]
    pub listen: SocketAddr,
    #[serde(default = "default_admin_listen")]
    pub admin_listen: SocketAddr,
    #[serde(default = "default_shutdown_grace_ms")]
    pub shutdown_grace_ms: u64,
    #[serde(default = "default_read_timeout_ms")]
    pub header_read_timeout_ms: u64,
    #[serde(default = "default_read_timeout_ms")]
    pub body_read_timeout_ms: u64,
    #[serde(default = "default_retry_budget_percent")]
    pub retry_budget_percent: u32,
    /// Shorthand: sha256 hex hashes of keys with no team that may call every name.
    #[serde(default)]
    pub api_keys: Vec<String>,
}

impl Default for Server {
    fn default() -> Self {
        Self {
            listen: default_listen(),
            admin_listen: default_admin_listen(),
            shutdown_grace_ms: default_shutdown_grace_ms(),
            header_read_timeout_ms: default_read_timeout_ms(),
            body_read_timeout_ms: default_read_timeout_ms(),
            retry_budget_percent: default_retry_budget_percent(),
            api_keys: Vec::new(),
        }
    }
}

fn default_shutdown_grace_ms() -> u64 {
    25_000
}
fn default_retry_budget_percent() -> u32 {
    20
}
fn default_read_timeout_ms() -> u64 {
    30_000
}

fn default_admin_listen() -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], 9464))
}

fn default_listen() -> SocketAddr {
    SocketAddr::from(([0, 0, 0, 0], 4000))
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Upstream {
    pub name: String,
    pub kind: UpstreamKind,
    pub base_url: String,
    /// Name of the env var holding the bearer token; never the token itself.
    #[serde(default)]
    pub api_key_env: Option<String>,
    #[serde(default = "default_timeout_ms")]
    pub timeout_ms: u32,
    #[serde(default)]
    pub stream_usage_mode: StreamUsageMode,
    #[serde(default)]
    pub health_check: Option<HealthCheck>,
    #[serde(default)]
    pub circuit_breaker: Option<CircuitBreaker>,
    #[serde(default)]
    pub metrics_url: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HealthCheck {
    #[serde(default)]
    pub path: String,
    pub interval_ms: u32,
    pub timeout_ms: u32,
    #[serde(default)]
    pub unhealthy_threshold: u32,
    #[serde(default)]
    pub healthy_threshold: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CircuitBreaker {
    #[serde(default)]
    pub failure_threshold: u32,
    #[serde(default)]
    pub open_ms: u32,
}

pub fn default_timeout_ms() -> u32 {
    600_000
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpstreamKind {
    Openai,
    Vllm,
    OpenaiCompatible,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StreamUsageMode {
    #[default]
    IncludeUsage,
    Passthrough,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Model {
    pub name: String,
    pub upstream: String,
    pub upstream_model: String,
    /// Absent = price unknown (usage metered, cost unknown); explicit zeros = free.
    #[serde(default)]
    pub price: Option<Price>,
}

fn default_weight() -> u32 {
    1
}

/// USD per one million tokens as decimals (e.g. `"0.15"` or `0.15`), parsed
/// from the YAML text and converted exactly to nano-USD, never through
/// floating point. At most nine fractional digits.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Price {
    #[serde(deserialize_with = "usd")]
    pub input_usd_per_mtok: u64,
    #[serde(deserialize_with = "usd")]
    pub output_usd_per_mtok: u64,
}

/// Another callable name for a model or a router.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Alias {
    pub name: String,
    pub target: String,
}

/// One model of a router or traffic split, with this router's settings.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouterMember {
    pub model: String,
    #[serde(default = "default_weight")]
    pub weight: u32,
    #[serde(default)]
    pub params: serde_json::Map<String, serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Router {
    pub name: String,
    #[serde(default)]
    pub members: Vec<RouterMember>,
    /// `weighted`, `least_in_flight`, `lowest_latency`, `vllm_load` or the
    /// name of a compiled-in strategy.
    #[serde(default = "default_strategy")]
    pub strategy: String,
    #[serde(default)]
    pub params: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    pub retry: Option<RetryPolicy>,
    /// Model names.
    #[serde(default)]
    pub fallbacks: Vec<String>,
    #[serde(default)]
    pub traffic_split: Vec<TrafficSplit>,
    #[serde(default)]
    pub sticky: StickyBy,
    #[serde(default)]
    pub sticky_header: String,
}

fn default_strategy() -> String {
    "weighted".into()
}

fn params_json(params: &serde_json::Map<String, serde_json::Value>) -> String {
    if params.is_empty() {
        String::new()
    } else {
        serde_json::to_string(params).expect("JSON object")
    }
}

fn wire_members(members: &[RouterMember]) -> Vec<pb::RouterMember> {
    members
        .iter()
        .map(|m| pb::RouterMember {
            model: m.model.clone(),
            weight: m.weight,
            params_json: params_json(&m.params),
        })
        .collect()
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetryPolicy {
    #[serde(default)]
    pub max_attempts: u32,
    #[serde(default)]
    pub retry_on_status: Vec<u32>,
    #[serde(default)]
    pub total_timeout_ms: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrafficSplit {
    pub name: String,
    pub members: Vec<RouterMember>,
    pub weight: u32,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StickyBy {
    #[default]
    None,
    Header,
    Team,
    Key,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RateLimits {
    #[serde(default)]
    pub requests_per_minute: u32,
    #[serde(default)]
    pub tokens_per_minute: u64,
    #[serde(default)]
    pub max_parallel_requests: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Team {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    /// Shared by every key of the team (limiter scope `team:<id>`).
    #[serde(default)]
    pub rate_limits: Option<RateLimits>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KeyStatus {
    #[default]
    Active,
    Revoked,
    Suspended,
    BudgetExceeded,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Key {
    pub key_id: String,
    /// Lowercase sha256 hex of the full secret key.
    pub key_sha256: String,
    #[serde(default)]
    pub team_id: String,
    #[serde(default)]
    pub status: KeyStatus,
    /// 0 = never expires.
    #[serde(default)]
    pub expires_at_unix_ms: i64,
    /// Fail-closed: without this or `allowed_models` the key may call nothing.
    #[serde(default)]
    pub allow_all_models: bool,
    #[serde(default)]
    pub allowed_models: Vec<String>,
    /// Limiter scope `key:<key_id>`.
    #[serde(default)]
    pub rate_limits: Option<RateLimits>,
}

/// Parses `"12.345"` USD into nano-USD without going through floating point.
pub fn parse_usd_nano(s: &str) -> Result<u64, String> {
    let err = || {
        format!("invalid USD amount {s:?}: expected a decimal string like \"0.15\" with at most 9 fractional digits")
    };
    let (whole, frac) = s.split_once('.').unwrap_or((s, ""));
    if whole.is_empty()
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || !frac.bytes().all(|b| b.is_ascii_digit())
        || frac.len() > 9
        || (s.contains('.') && frac.is_empty())
    {
        return Err(err());
    }
    let whole: u64 = whole.parse().map_err(|_| err())?;
    let mut nanos = 0u64;
    for (i, digit) in frac.bytes().enumerate() {
        nanos += u64::from(digit - b'0') * 10u64.pow(8 - i as u32);
    }
    whole
        .checked_mul(1_000_000_000)
        .and_then(|n| n.checked_add(nanos))
        .ok_or_else(err)
}

fn usd<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
    let s = String::deserialize(deserializer).map_err(|_| {
        serde::de::Error::custom("prices must be decimal USD amounts, e.g. \"0.15\"")
    })?;
    parse_usd_nano(&s).map_err(serde::de::Error::custom)
}

fn rate_limit_scope(prefix: &str, id: &str, limits: &RateLimits) -> pb::RateLimitScope {
    pb::RateLimitScope {
        id: format!("{prefix}:{id}"),
        limit: Some(pb::RateLimit {
            requests_per_minute: limits.requests_per_minute,
            tokens_per_minute: limits.tokens_per_minute,
            max_parallel_requests: limits.max_parallel_requests,
        }),
    }
}

fn parse_hash(hex: &str, field: &str) -> Result<Vec<u8>, ConfigError> {
    if hex.len() != 64
        || !hex
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err(ConfigError::Invalid(format!(
            "{field} must contain 64-character lowercase sha256 hex hashes"
        )));
    }
    Ok((0..32)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).expect("validated hex"))
        .collect())
}

impl Config {
    pub fn from_yaml(s: &str) -> Result<Self, ConfigError> {
        Ok(serde_yaml::from_str(s)?)
    }

    /// Reads a standalone YAML file and converts it.
    pub fn load(path: &Path) -> Result<(LocalSettings, Snapshot), ConfigError> {
        let s = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
            path: path.display().to_string(),
            source,
        })?;
        Self::parse(&s)
    }

    /// Parses YAML text and converts it to local settings plus a snapshot.
    pub fn parse(s: &str) -> Result<(LocalSettings, Snapshot), ConfigError> {
        Self::from_yaml(s)?.into_parts()
    }

    pub fn into_parts(self) -> Result<(LocalSettings, Snapshot), ConfigError> {
        let settings = self.local_settings()?;
        let snapshot = self.to_snapshot()?;
        Ok((settings, snapshot))
    }

    fn local_settings(&self) -> Result<LocalSettings, ConfigError> {
        let server = &self.server;
        if server.retry_budget_percent > 20 {
            return Err(ConfigError::Invalid(
                "server.retry_budget_percent must be in 0..=20".into(),
            ));
        }
        if server.listen == server.admin_listen {
            return Err(ConfigError::Invalid(
                "server.admin_listen must differ from server.listen".into(),
            ));
        }
        for (name, value) in [
            ("shutdown_grace_ms", server.shutdown_grace_ms),
            ("header_read_timeout_ms", server.header_read_timeout_ms),
            ("body_read_timeout_ms", server.body_read_timeout_ms),
        ] {
            if value == 0 {
                return Err(ConfigError::Invalid(format!(
                    "server.{name} must be greater than zero"
                )));
            }
        }
        Ok(LocalSettings {
            listen: server.listen,
            admin_listen: server.admin_listen,
            shutdown_grace_ms: server.shutdown_grace_ms,
            header_read_timeout_ms: server.header_read_timeout_ms,
            body_read_timeout_ms: server.body_read_timeout_ms,
            retry_budget_percent: server.retry_budget_percent,
        })
    }

    /// Converts to a version-0 snapshot. References and duplicates are
    /// checked by the gateway's snapshot validation, not here.
    pub fn to_snapshot(&self) -> Result<Snapshot, ConfigError> {
        let upstreams = self
            .upstreams
            .iter()
            .map(|u| pb::Upstream {
                name: u.name.clone(),
                kind: match u.kind {
                    UpstreamKind::Openai => pb::UpstreamKind::Openai,
                    UpstreamKind::Vllm => pb::UpstreamKind::Vllm,
                    UpstreamKind::OpenaiCompatible => pb::UpstreamKind::OpenaiCompatible,
                } as i32,
                base_url: u.base_url.clone(),
                api_key_ref: u.api_key_env.clone().unwrap_or_default(),
                timeout_ms: u.timeout_ms,
                stream_usage_mode: match u.stream_usage_mode {
                    StreamUsageMode::IncludeUsage => pb::StreamUsageMode::IncludeUsage,
                    StreamUsageMode::Passthrough => pb::StreamUsageMode::Passthrough,
                } as i32,
                health_check: u.health_check.as_ref().map(|h| pb::HealthCheck {
                    path: h.path.clone(),
                    interval_ms: h.interval_ms,
                    timeout_ms: h.timeout_ms,
                    unhealthy_threshold: h.unhealthy_threshold,
                    healthy_threshold: h.healthy_threshold,
                }),
                circuit_breaker: u.circuit_breaker.as_ref().map(|c| pb::CircuitBreaker {
                    failure_threshold: c.failure_threshold,
                    open_ms: c.open_ms,
                }),
                metrics_url: u.metrics_url.clone(),
            })
            .collect();
        let models = self
            .models
            .iter()
            .map(|d| pb::Model {
                name: d.name.clone(),
                upstream: d.upstream.clone(),
                upstream_model: d.upstream_model.clone(),
                price: d.price.as_ref().map(|p| pb::Price {
                    input_nano_usd_per_mtok: p.input_usd_per_mtok,
                    output_nano_usd_per_mtok: p.output_usd_per_mtok,
                }),
            })
            .collect();
        let aliases = self
            .aliases
            .iter()
            .map(|a| pb::Alias {
                name: a.name.clone(),
                target: a.target.clone(),
            })
            .collect();
        let routers = self
            .routers
            .iter()
            .map(|r| pb::Router {
                name: r.name.clone(),
                members: wire_members(&r.members),
                strategy: r.strategy.clone(),
                params_json: params_json(&r.params),
                retry: r.retry.as_ref().map(|p| pb::RetryPolicy {
                    max_attempts: p.max_attempts,
                    retry_on_status: p.retry_on_status.clone(),
                    total_timeout_ms: p.total_timeout_ms,
                }),
                fallbacks: r.fallbacks.clone(),
                traffic_split: r
                    .traffic_split
                    .iter()
                    .map(|s| pb::TrafficSplit {
                        name: s.name.clone(),
                        members: wire_members(&s.members),
                        weight: s.weight,
                    })
                    .collect(),
                sticky: match r.sticky {
                    StickyBy::None => pb::StickyBy::None,
                    StickyBy::Header => pb::StickyBy::Header,
                    StickyBy::Team => pb::StickyBy::Team,
                    StickyBy::Key => pb::StickyBy::Key,
                } as i32,
                sticky_header: r.sticky_header.clone(),
            })
            .collect();
        let teams: Vec<pb::Team> = self
            .teams
            .iter()
            .map(|t| pb::Team {
                id: t.id.clone(),
                name: t.name.clone().unwrap_or_else(|| t.id.clone()),
            })
            .collect();
        let mut api_keys = Vec::new();
        let mut shorthand = HashSet::new();
        for hex in &self.server.api_keys {
            let hash = parse_hash(hex, "server.api_keys")?;
            // P0 accepted duplicate shorthand entries; keep that, they are one key.
            if !shorthand.insert(hex.as_str()) {
                continue;
            }
            api_keys.push(pb::ApiKey {
                // The hash is not secret; the full value keeps ids unique and
                // stable across reorderings.
                key_id: format!("api_keys:{hex}"),
                key_hash: hash,
                status: pb::KeyStatus::Active as i32,
                policy: Some(pb::EffectivePolicy {
                    allow_all_models: true,
                    ..Default::default()
                }),
                ..Default::default()
            });
        }
        for key in &self.keys {
            let team = self.teams.iter().find(|t| t.id == key.team_id);
            if !key.team_id.is_empty() && team.is_none() {
                return Err(ConfigError::Invalid(format!(
                    "key {:?} references unknown team {:?}",
                    key.key_id, key.team_id
                )));
            }
            let mut scopes = Vec::new();
            if let Some(limits) = team.and_then(|t| t.rate_limits.as_ref()) {
                scopes.push(rate_limit_scope("team", &key.team_id, limits));
            }
            if let Some(limits) = &key.rate_limits {
                scopes.push(rate_limit_scope("key", &key.key_id, limits));
            }
            api_keys.push(pb::ApiKey {
                key_id: key.key_id.clone(),
                key_hash: parse_hash(&key.key_sha256, "keys[].key_sha256")?,
                team_id: key.team_id.clone(),
                status: match key.status {
                    KeyStatus::Active => pb::KeyStatus::Active,
                    KeyStatus::Revoked => pb::KeyStatus::Revoked,
                    KeyStatus::Suspended => pb::KeyStatus::Suspended,
                    KeyStatus::BudgetExceeded => pb::KeyStatus::BudgetExceeded,
                } as i32,
                expires_at_unix_ms: key.expires_at_unix_ms,
                policy: Some(pb::EffectivePolicy {
                    allow_all_models: key.allow_all_models,
                    allowed_models: key.allowed_models.clone(),
                    rate_limit_scopes: scopes,
                    budget_window_end_unix_ms: 0,
                }),
                ..Default::default()
            });
        }
        Ok(Snapshot {
            version: 0,
            upstreams,
            models,
            aliases,
            routers,
            api_keys,
            teams,
            created_at_unix_ms: 0,
            required_capabilities: {
                let mut caps = Vec::new();
                for (cap, used) in [
                    ("retry", self.routers.iter().any(|r| r.retry.is_some())),
                    (
                        "fallbacks",
                        self.routers.iter().any(|r| !r.fallbacks.is_empty()),
                    ),
                    (
                        "health_check",
                        self.upstreams.iter().any(|u| u.health_check.is_some()),
                    ),
                    (
                        "circuit_breaker",
                        self.upstreams.iter().any(|u| u.circuit_breaker.is_some()),
                    ),
                    (
                        "traffic_split",
                        self.routers.iter().any(|r| {
                            !r.traffic_split.is_empty()
                                || r.sticky != StickyBy::None
                                || !r.sticky_header.is_empty()
                        }),
                    ),
                    (
                        "vllm_load",
                        self.upstreams.iter().any(|u| !u.metrics_url.is_empty()),
                    ),
                ] {
                    if used {
                        caps.push(cap.into());
                    }
                }
                for r in &self.routers {
                    let cap = format!("strategy:{}", r.strategy);
                    if !caps.contains(&cap) {
                        caps.push(cap);
                    }
                }
                caps
            },
            scope: String::new(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const EXAMPLE: &str = include_str!("../../../config.example.yaml");
    const BENCH: &str = include_str!("../../../../bench/lumewarp/config.yaml");

    #[test]
    fn custom_strategy_yaml_preserves_object_and_emits_capability() {
        let (_, snapshot) = Config::parse(
            "routers:\n  - name: custom\n    strategy: header_pin\n    params: {headers: [x-pin], value: 2}\n",
        ).unwrap();
        assert_eq!(snapshot.routers[0].strategy, "header_pin");
        assert_eq!(snapshot.required_capabilities, ["strategy:header_pin"]);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&snapshot.routers[0].params_json).unwrap(),
            serde_json::json!({"headers": ["x-pin"], "value": 2})
        );
        assert!(Config::parse("routers: [{name: custom, params: [1]}]").is_err());
    }

    #[test]
    fn resilience_yaml_compiles_presence_based_capabilities() {
        let (settings, s) = Config::parse(
            r#"
server: {retry_budget_percent: 10}
upstreams:
  - name: u
    kind: vllm
    base_url: http://localhost/v1
    health_check: {interval_ms: 1000, timeout_ms: 100}
    circuit_breaker: {}
models:
  - {name: d, upstream: u, upstream_model: actual}
  - {name: backup, upstream: u, upstream_model: actual}
routers:
  - name: public
    members: [{model: d}]
    retry: {}
    fallbacks: [backup]
"#,
        )
        .unwrap();
        assert_eq!(settings.retry_budget_percent, 10);
        assert_eq!(
            s.required_capabilities,
            [
                "retry",
                "fallbacks",
                "health_check",
                "circuit_breaker",
                "strategy:weighted"
            ]
        );
        assert_eq!(s.routers[0].retry.as_ref().unwrap().max_attempts, 0);
        assert!(s.upstreams[0].circuit_breaker.is_some());
        assert!(Config::parse("server: {retry_budget_percent: 21}").is_err());
        assert!(Config::parse("{}")
            .unwrap()
            .1
            .required_capabilities
            .is_empty());
    }

    #[test]
    fn w6_yaml_fields_compile_presence_based_capabilities() {
        let (_, s) = Config::parse(
            r#"
upstreams:
  - {name: u, kind: vllm, base_url: 'http://localhost/v1', metrics_url: 'http://localhost/metrics'}
routers:
  - name: public
    strategy: vllm_load
    traffic_split: [{name: stable, members: [{model: d}], weight: 1}]
    sticky: header
    sticky_header: x-session
"#,
        )
        .unwrap();
        assert_eq!(s.routers[0].traffic_split[0].name, "stable");
        assert_eq!(s.routers[0].sticky, pb::StickyBy::Header as i32);
        assert_eq!(
            s.required_capabilities,
            ["traffic_split", "vllm_load", "strategy:vllm_load"]
        );
    }

    #[test]
    fn example_and_bench_configs_convert() {
        let quickstart = include_str!("../../../../README.md")
            .split_once("```yaml\n")
            .expect("README has a YAML quickstart")
            .1
            .split_once("```")
            .expect("quickstart code block is closed")
            .0;
        for yaml in [EXAMPLE, BENCH, quickstart] {
            let (_, snapshot) = Config::parse(yaml).unwrap();
            assert!(!snapshot.routers.is_empty());
            assert_eq!(snapshot.version, 0);
        }
        let (_, snapshot) = Config::parse(BENCH).unwrap();
        assert_eq!(AuthMode::standalone(&snapshot), AuthMode::Open);
        let (_, example) = Config::parse(EXAMPLE).unwrap();
        assert_eq!(AuthMode::standalone(&example), AuthMode::Open);
        // The commented-out `keys:` block must stay valid YAML for this schema.
        let mut in_keys = false;
        let uncommented: String = EXAMPLE
            .lines()
            .map(|line| {
                in_keys |= line == "# keys:";
                match line.strip_prefix("# ") {
                    Some(rest) if in_keys => format!("{rest}\n"),
                    _ if in_keys && line == "#" => "\n".into(),
                    _ => format!("{line}\n"),
                }
            })
            .collect();
        let (_, example) = Config::parse(&uncommented).unwrap();
        assert_eq!(AuthMode::standalone(&example), AuthMode::RequireKey);
        assert_eq!(example.api_keys.len(), 2);
        assert_eq!(example.api_keys[0].team_id, "team-research");
    }

    #[test]
    fn converts_upstreams_and_routers() {
        let (_, s) = Config::parse(
            r#"
upstreams:
  - { name: u, kind: vllm, base_url: "http://localhost:8000/v1", api_key_env: K, stream_usage_mode: passthrough }
models:
  - { name: d, upstream: u, upstream_model: m, price: { input_usd_per_mtok: "0.15", output_usd_per_mtok: "12" } }
  - { name: e, upstream: u, upstream_model: m }
routers:
  - { name: pool, members: [{model: d, weight: 3, params: {zone: a}}, {model: e}], strategy: least_in_flight }
aliases:
  - { name: nick, target: pool }
"#,
        )
        .unwrap();
        assert_eq!(s.upstreams[0].kind, pb::UpstreamKind::Vllm as i32);
        assert_eq!(s.upstreams[0].api_key_ref, "K");
        assert_eq!(s.upstreams[0].timeout_ms, 600_000);
        assert_eq!(
            s.upstreams[0].stream_usage_mode,
            pb::StreamUsageMode::Passthrough as i32
        );
        assert_eq!(
            s.models[0].price,
            Some(pb::Price {
                input_nano_usd_per_mtok: 150_000_000,
                output_nano_usd_per_mtok: 12_000_000_000,
            })
        );
        assert_eq!(s.models[1].price, None);
        assert_eq!(s.routers[0].strategy, "least_in_flight");
        assert_eq!(s.routers[0].members[0].weight, 3);
        assert_eq!(s.routers[0].members[0].params_json, r#"{"zone":"a"}"#);
        assert_eq!(s.routers[0].members[1].weight, 1, "default member weight");
        assert_eq!(s.routers[0].members[1].params_json, "");
        assert_eq!(s.aliases[0].target, "pool");
        assert_eq!(s.required_capabilities, ["strategy:least_in_flight"]);
        // The removed fields are errors, not silently ignored.
        for removed in [
            "models: [{name: d, upstream: u, upstream_model: m, weight: 3}]",
            "routers: [{name: r, models: [d]}]",
            "routers: [{name: r, custom_strategy: x}]",
            "routers: [{name: r, strategy_params: {}}]",
            "routers: [{name: r, traffic_split: [{name: s, models: [d], weight: 1}]}]",
        ] {
            assert!(Config::parse(removed).is_err(), "{removed}");
        }
    }

    #[test]
    fn usd_parsing_is_exact() {
        assert_eq!(parse_usd_nano("0"), Ok(0));
        assert_eq!(parse_usd_nano("0.000000001"), Ok(1));
        assert_eq!(parse_usd_nano("0.1"), Ok(100_000_000));
        assert_eq!(parse_usd_nano("2.5"), Ok(2_500_000_000));
        assert_eq!(parse_usd_nano("18446744073.709551615"), Ok(u64::MAX));
        for bad in [
            "",
            ".5",
            "1.",
            "-1",
            "1e3",
            "0.0000000001",
            "18446744074",
            " 1",
            "1,5",
        ] {
            assert!(parse_usd_nano(bad).is_err(), "{bad}");
        }
        // Plain YAML numbers are read from their source text, never via f64.
        let plain = r#"
models:
  - { name: d, upstream: u, upstream_model: m, price: { input_usd_per_mtok: 0.10, output_usd_per_mtok: 1.000000001 } }
"#;
        let (_, s) = Config::parse(plain).unwrap();
        assert_eq!(
            s.models[0].price,
            Some(pb::Price {
                input_nano_usd_per_mtok: 100_000_000,
                output_nano_usd_per_mtok: 1_000_000_001,
            })
        );
        for bad in ["1e-3", "-1", "true", "[1]"] {
            let yaml = format!("models:\n  - {{ name: d, upstream: u, upstream_model: m, price: {{ input_usd_per_mtok: {bad}, output_usd_per_mtok: '1' }} }}");
            assert!(Config::parse(&yaml).is_err(), "{bad}");
        }
    }

    #[test]
    fn converts_teams_keys_and_shorthand() {
        let a = "a".repeat(64);
        let b = "b".repeat(64);
        let yaml = format!(
            r#"
server:
  api_keys: ['{a}', '{a}']
teams:
  - {{ id: t1, rate_limits: {{ requests_per_minute: 60 }} }}
  - {{ id: t2, name: Two }}
keys:
  - key_id: k1
    key_sha256: '{b}'
    team_id: t1
    status: revoked
    expires_at_unix_ms: 5
    allowed_models: [m1]
    rate_limits: {{ max_parallel_requests: 2 }}
  - {{ key_id: k2, key_sha256: '{}' }}
"#,
            "c".repeat(64)
        );
        let (_, s) = Config::parse(&yaml).unwrap();
        assert_eq!(s.teams[0].name, "t1");
        assert_eq!(s.teams[1].name, "Two");
        assert_eq!(s.api_keys.len(), 3, "duplicate shorthand collapses");
        let shorthand = &s.api_keys[0];
        assert_eq!(shorthand.key_hash, vec![0xaa; 32]);
        assert_eq!(shorthand.key_id, format!("api_keys:{a}"));
        assert!(shorthand.policy.as_ref().unwrap().allow_all_models);
        let k1 = &s.api_keys[1];
        assert_eq!(k1.team_id, "t1");
        assert_eq!(k1.status, pb::KeyStatus::Revoked as i32);
        assert_eq!(k1.expires_at_unix_ms, 5);
        let policy = k1.policy.as_ref().unwrap();
        assert!(!policy.allow_all_models);
        assert_eq!(policy.allowed_models, ["m1"]);
        let ids: Vec<_> = policy
            .rate_limit_scopes
            .iter()
            .map(|s| s.id.as_str())
            .collect();
        assert_eq!(ids, ["team:t1", "key:k1"]);
        assert_eq!(
            policy.rate_limit_scopes[0]
                .limit
                .unwrap()
                .requests_per_minute,
            60
        );
        // Fail-closed default: no allow_all_models and no list = deny-all.
        let k2 = s.api_keys[2].policy.as_ref().unwrap();
        assert!(!k2.allow_all_models && k2.allowed_models.is_empty());
        assert_eq!(s.api_keys[2].status, pb::KeyStatus::Active as i32);
    }

    #[test]
    fn shorthand_ids_use_the_full_hash() {
        let a = "a".repeat(64);
        let b = format!("{}{}", "a".repeat(16), "b".repeat(48));
        let (_, s) = Config::parse(&format!("server:\n  api_keys: ['{a}', '{b}', '{a}']")).unwrap();
        let ids: Vec<_> = s.api_keys.iter().map(|k| k.key_id.clone()).collect();
        assert_eq!(ids, [format!("api_keys:{a}"), format!("api_keys:{b}")]);
    }

    #[test]
    fn rejects_bad_key_fields() {
        for key in ["x".repeat(64), "A".repeat(64), "0".repeat(63)] {
            assert!(Config::parse(&format!("server:\n  api_keys: ['{key}']")).is_err());
            assert!(
                Config::parse(&format!("keys:\n  - {{key_id: k, key_sha256: '{key}'}}")).is_err()
            );
        }
        assert!(Config::parse(&format!("server:\n  api_keys: ['{}']", "a".repeat(64))).is_ok());
        let unknown_team = format!(
            "keys:\n  - {{key_id: k, key_sha256: '{}', team_id: nope}}",
            "a".repeat(64)
        );
        assert!(Config::parse(&unknown_team)
            .unwrap_err()
            .to_string()
            .contains("unknown team"));
    }

    #[test]
    fn admin_listener_defaults_and_validation() {
        let (settings, _) = Config::parse("").unwrap();
        assert_eq!(settings.admin_listen, "127.0.0.1:9464".parse().unwrap());
        assert!(Config::parse("server: {listen: '127.0.0.1:9464'}").is_err());
    }

    #[test]
    fn validates_timeouts() {
        let (settings, _) = Config::parse("").unwrap();
        assert_eq!(settings.shutdown_grace_ms, 25_000);
        assert_eq!(settings.header_read_timeout_ms, 30_000);
        assert_eq!(settings.body_read_timeout_ms, 30_000);
        for name in [
            "shutdown_grace_ms",
            "header_read_timeout_ms",
            "body_read_timeout_ms",
        ] {
            assert!(Config::parse(&format!("server: {{{name}: 0}}"))
                .unwrap_err()
                .to_string()
                .contains(name));
            assert!(Config::parse(&format!("server: {{{name}: 1}}")).is_ok());
        }
    }

    #[test]
    fn rejects_unknown_fields() {
        assert!(matches!(
            Config::parse("bogus: 1"),
            Err(ConfigError::Parse(_))
        ));
        assert!(matches!(
            Config::parse("keys: [{key_id: k, key_sha256: x, bogus: 1}]"),
            Err(ConfigError::Parse(_))
        ));
    }
}

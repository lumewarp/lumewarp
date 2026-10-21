//! Config snapshot schema shared by standalone YAML mode and (from P1) the
//! control-plane gRPC stream.
//!
//! These types mirror `proto/lumewarp/config/v1/snapshot.proto`. The proto is
//! the source of truth; once the gRPC stream lands these will be generated
//! from it rather than hand-written.

use std::{collections::HashSet, net::SocketAddr, path::Path};

use serde::Deserialize;

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

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub server: Server,
    #[serde(default)]
    pub upstreams: Vec<Upstream>,
    #[serde(default)]
    pub deployments: Vec<Deployment>,
    #[serde(default)]
    pub routes: Vec<ModelRoute>,
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
    // TODO(P1): mirror in snapshot.proto
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
            api_keys: Vec::new(),
        }
    }
}

fn default_shutdown_grace_ms() -> u64 {
    25_000
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
}

fn default_timeout_ms() -> u32 {
    600_000
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpstreamKind {
    Openai,
    Vllm,
    OpenaiCompatible,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Deployment {
    pub name: String,
    pub upstream: String,
    pub upstream_model: String,
    #[serde(default = "default_weight")]
    pub weight: u32,
}

fn default_weight() -> u32 {
    1
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelRoute {
    pub model: String,
    pub deployments: Vec<String>,
    #[serde(default)]
    pub strategy: RoutingStrategy,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RoutingStrategy {
    #[default]
    Weighted,
    LeastInFlight,
    LowestLatency,
}

impl Config {
    pub fn from_yaml(s: &str) -> Result<Self, ConfigError> {
        let config: Config = serde_yaml::from_str(s)?;
        config.validate()?;
        Ok(config)
    }

    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let s = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
            path: path.display().to_string(),
            source,
        })?;
        Self::from_yaml(&s)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.server.listen == self.server.admin_listen {
            return Err(ConfigError::Invalid(
                "server.admin_listen must differ from server.listen".into(),
            ));
        }
        for (name, value) in [
            ("shutdown_grace_ms", self.server.shutdown_grace_ms),
            ("header_read_timeout_ms", self.server.header_read_timeout_ms),
            ("body_read_timeout_ms", self.server.body_read_timeout_ms),
        ] {
            if value == 0 {
                return Err(ConfigError::Invalid(format!(
                    "server.{name} must be greater than zero"
                )));
            }
        }
        for key in &self.server.api_keys {
            if key.len() != 64
                || !key
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            {
                return Err(ConfigError::Invalid(
                    "server.api_keys must contain 64-character lowercase sha256 hex hashes".into(),
                ));
            }
        }
        let upstreams = unique_names(self.upstreams.iter().map(|u| u.name.as_str()), "upstream")?;
        let deployments = unique_names(
            self.deployments.iter().map(|d| d.name.as_str()),
            "deployment",
        )?;
        unique_names(self.routes.iter().map(|r| r.model.as_str()), "route")?;

        for d in &self.deployments {
            if !upstreams.contains(d.upstream.as_str()) {
                return Err(ConfigError::Invalid(format!(
                    "deployment {:?} references unknown upstream {:?}",
                    d.name, d.upstream
                )));
            }
        }
        for r in &self.routes {
            if r.model == "unknown" {
                return Err(ConfigError::Invalid(
                    "route alias 'unknown' is reserved for metrics".into(),
                ));
            }
            if r.strategy == RoutingStrategy::LowestLatency {
                return Err(ConfigError::Invalid(
                    "lowest_latency is not supported yet".into(),
                ));
            }
            if r.deployments.is_empty() {
                return Err(ConfigError::Invalid(format!(
                    "route {:?} has no deployments",
                    r.model
                )));
            }
            if let Some(d) = r
                .deployments
                .iter()
                .find(|d| !deployments.contains(d.as_str()))
            {
                return Err(ConfigError::Invalid(format!(
                    "route {:?} references unknown deployment {:?}",
                    r.model, d
                )));
            }
        }
        Ok(())
    }
}

fn unique_names<'a>(
    names: impl Iterator<Item = &'a str>,
    kind: &str,
) -> Result<HashSet<&'a str>, ConfigError> {
    let mut seen = HashSet::new();
    for name in names {
        if !seen.insert(name) {
            return Err(ConfigError::Invalid(format!("duplicate {kind} {name:?}")));
        }
    }
    Ok(seen)
}

#[cfg(test)]
mod tests {
    use super::*;

    const EXAMPLE: &str = include_str!("../../../config.example.yaml");

    #[test]
    fn example_config_parses() {
        let config = Config::from_yaml(EXAMPLE).unwrap();
        assert!(!config.routes.is_empty());
    }

    #[test]
    fn rejects_unknown_deployment() {
        let yaml = r#"
upstreams:
  - { name: u, kind: vllm, base_url: "http://localhost:8000/v1" }
deployments:
  - { name: d, upstream: u, upstream_model: m }
routes:
  - { model: m, deployments: [missing] }
"#;
        assert!(matches!(
            Config::from_yaml(yaml),
            Err(ConfigError::Invalid(_))
        ));
    }

    #[test]
    fn rejects_lowest_latency() {
        let yaml = EXAMPLE.replace(
            "deployments: [llama-vllm]",
            "deployments: [llama-vllm]\n    strategy: lowest_latency",
        );
        assert!(Config::from_yaml(&yaml)
            .unwrap_err()
            .to_string()
            .contains("not supported yet"));
    }

    #[test]
    fn validates_api_key_hashes() {
        for key in ["x".repeat(64), "A".repeat(64), "0".repeat(63)] {
            assert!(Config::from_yaml(&format!("server:\n  api_keys: ['{key}']")).is_err());
        }
        assert!(Config::from_yaml(&format!("server:\n  api_keys: ['{}']", "a".repeat(64))).is_ok());
    }

    #[test]
    fn admin_listener_defaults_and_validation() {
        let config = Config::from_yaml("").unwrap();
        assert_eq!(
            config.server.admin_listen,
            "127.0.0.1:9464".parse().unwrap()
        );
        assert!(Config::from_yaml("server: {listen: '127.0.0.1:9464'}").is_err());
    }

    #[test]
    fn validates_timeouts_and_reserved_alias() {
        let config = Config::from_yaml("").unwrap();
        assert_eq!(config.server.shutdown_grace_ms, 25_000);
        assert_eq!(config.server.header_read_timeout_ms, 30_000);
        assert_eq!(config.server.body_read_timeout_ms, 30_000);
        for name in [
            "shutdown_grace_ms",
            "header_read_timeout_ms",
            "body_read_timeout_ms",
        ] {
            assert!(Config::from_yaml(&format!("server: {{{name}: 0}}"))
                .unwrap_err()
                .to_string()
                .contains(name));
            assert!(Config::from_yaml(&format!("server: {{{name}: 1}}")).is_ok());
        }
        let yaml = EXAMPLE.replace("model: llama-3.1-8b", "model: unknown");
        assert!(Config::from_yaml(&yaml)
            .unwrap_err()
            .to_string()
            .contains("reserved"));
    }

    #[test]
    fn rejects_unknown_fields() {
        assert!(matches!(
            Config::from_yaml("bogus: 1"),
            Err(ConfigError::Parse(_))
        ));
    }
}

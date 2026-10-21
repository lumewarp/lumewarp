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
}

impl Default for Server {
    fn default() -> Self {
        Self {
            listen: default_listen(),
        }
    }
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
        let upstreams = unique_names(self.upstreams.iter().map(|u| u.name.as_str()), "upstream")?;
        let deployments =
            unique_names(self.deployments.iter().map(|d| d.name.as_str()), "deployment")?;
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
            if r.deployments.is_empty() {
                return Err(ConfigError::Invalid(format!(
                    "route {:?} has no deployments",
                    r.model
                )));
            }
            if let Some(d) = r.deployments.iter().find(|d| !deployments.contains(d.as_str())) {
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
        assert!(matches!(Config::from_yaml(yaml), Err(ConfigError::Invalid(_))));
    }

    #[test]
    fn rejects_unknown_fields() {
        assert!(matches!(
            Config::from_yaml("bogus: 1"),
            Err(ConfigError::Parse(_))
        ));
    }
}

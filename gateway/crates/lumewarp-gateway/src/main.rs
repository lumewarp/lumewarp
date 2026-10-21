mod auth;
mod error;
mod metrics;
mod proxy;
mod router;
mod server;
mod telemetry;

use std::{path::PathBuf, sync::Arc};

use anyhow::Context;
use arc_swap::ArcSwap;
use clap::Parser;
use lumewarp_config::Config;
use sha2::{Digest, Sha256};

#[derive(Parser)]
#[command(name = "lumewarp", version, about = "Lumewarp LLM gateway")]
struct Args {
    /// Standalone YAML config.
    #[arg(long, short, env = "LUMEWARP_CONFIG")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let telemetry = tokio::task::spawn_blocking(telemetry::init).await??;
    let args = Args::parse();
    let yaml = std::fs::read_to_string(&args.config)
        .with_context(|| format!("loading {}", args.config.display()))?;
    let config = Config::from_yaml(&yaml)?;
    let router = router::Router::compile(&config).context("compiling router snapshot")?;
    router
        .metrics
        .set_yaml_hash(&hex::encode(Sha256::digest(yaml.as_bytes()))[..12]);
    let snapshot = Arc::new(ArcSwap::from_pointee(router));
    let result = server::serve(&config.server, snapshot, telemetry.is_some()).await;
    if let Some(provider) = telemetry {
        match tokio::task::spawn_blocking(move || provider.shutdown()).await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => tracing::warn!(%err, "OTLP exporter shutdown failed"),
            Err(err) => tracing::warn!(%err, "OTLP exporter shutdown task failed"),
        }
    }
    result
}

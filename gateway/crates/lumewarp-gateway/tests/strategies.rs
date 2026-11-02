//! `lumewarp strategies --json` is the same manifest the control plane has
//! committed, so built-ins validate there without any gateway connected.
#![cfg(not(feature = "strategy-priority-failover"))]

use std::{path::Path, process::Command};

#[test]
fn manifest_matches_the_control_plane_copy() {
    let output = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
        .args(["strategies", "--json"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let committed = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../control-plane/src/lumewarp_control_plane/compiler/builtin_strategies.json");
    let committed = std::fs::read_to_string(committed).unwrap();
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        committed,
        "regenerate with: cargo run -p lumewarp-gateway -- strategies --json \
         > control-plane/src/lumewarp_control_plane/compiler/builtin_strategies.json"
    );
}

#[test]
fn the_subcommand_needs_no_configuration() {
    let output = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
        .arg("strategies")
        .env_remove("LUMEWARP_CONFIG")
        .env_remove("LUMEWARP_CONTROL_PLANE")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let listing = String::from_utf8(output.stdout).unwrap();
    assert!(listing.contains("least_in_flight"), "{listing}");
}

/// Standalone mode has no control plane to validate first: `configure` refuses
/// at load, built-ins included, and the process never starts serving.
#[test]
fn standalone_yaml_is_validated_by_configure_at_load() {
    let run = |routers: &str| {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.yaml");
        let yaml = format!(
            "upstreams: [{{name: u, kind: vllm, base_url: 'http://127.0.0.1:9/v1'}}]\n\
             models: [{{name: m, upstream: u, upstream_model: x}}]\n\
             routers: [{routers}]\n"
        );
        std::fs::write(&path, yaml).unwrap();
        Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(&path)
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .output()
            .unwrap()
    };
    let refused = run("{name: r, strategy: weighted, members: [{model: m, params: {zone: a}}]}");
    assert!(!refused.status.success());
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(stderr.contains("strategy configuration failed"), "{stderr}");
    assert!(!stderr.contains("zone"), "{stderr}");

    let refused = run("{name: r, strategy: not_compiled_in, members: [{model: m}]}");
    assert!(!refused.status.success());
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(stderr.contains("not_compiled_in"), "{stderr}");
}

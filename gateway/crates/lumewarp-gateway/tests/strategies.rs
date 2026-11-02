//! `lumewarp strategies --json` is the manifest the control plane validates
//! against. The committed copy holds the built-ins, so they validate without a
//! gateway connected; a gateway built with a strategy feature reports that
//! crate too. The built-ins are compared with the committed file and the custom
//! entries with the features compiled into this test binary, so `cargo test`
//! passes with none, one or every strategy feature enabled.

use std::{collections::BTreeSet, path::Path, process::Command};

use serde_json::Value;

fn manifest() -> Value {
    let output = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
        .args(["strategies", "--json"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "manifest is not JSON: {error}\n{}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn committed_manifest() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../control-plane/src/lumewarp_control_plane/compiler/builtin_strategies.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn strategy_names(manifest: &Value) -> BTreeSet<&str> {
    manifest["strategies"]
        .as_array()
        .expect("strategies array")
        .iter()
        .map(|strategy| strategy["name"].as_str().expect("strategy name"))
        .collect()
}

#[test]
fn builtins_match_the_control_plane_copy() {
    let manifest = manifest();
    let committed = committed_manifest();
    // Custom entries are not in the committed copy; the next test covers them.
    let builtin_names = strategy_names(&committed);
    let builtins: Vec<&Value> = manifest["strategies"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|strategy| builtin_names.contains(strategy["name"].as_str().unwrap()))
        .collect();
    assert_eq!(
        manifest["api_version"], committed["api_version"],
        "the binary and the committed manifest report different API versions"
    );
    assert_eq!(
        Value::Array(builtins.into_iter().cloned().collect()),
        committed["strategies"],
        "built-in descriptors changed; regenerate with: cargo run -p lumewarp-gateway -- \
         strategies --json > control-plane/src/lumewarp_control_plane/compiler/builtin_strategies.json"
    );
}

/// One line per custom strategy feature in `crates/lumewarp-gateway/Cargo.toml`,
/// in the manifest's order (sorted by name). Adding a strategy adds a line here.
fn compiled_custom_strategies() -> Vec<&'static str> {
    let mut names = Vec::new();
    if cfg!(feature = "strategy-cheapest-first") {
        names.push("cheapest_first");
    }
    if cfg!(feature = "strategy-priority-failover") {
        names.push("priority_failover");
    }
    names
}

#[test]
fn custom_strategies_are_exactly_the_compiled_features() {
    let manifest = manifest();
    let committed = committed_manifest();
    let builtin_names = strategy_names(&committed);
    let custom: Vec<&Value> = manifest["strategies"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|strategy| !builtin_names.contains(strategy["name"].as_str().unwrap()))
        .collect();
    let custom_names: Vec<&str> = custom
        .iter()
        .map(|strategy| strategy["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        custom_names,
        compiled_custom_strategies(),
        "a custom entry must come from a selected strategy feature, and every selected \
         feature must add one"
    );
    for strategy in custom {
        let name = strategy["name"].as_str().unwrap();
        assert_eq!(
            strategy["digest"].as_str().map(str::len),
            Some(64),
            "{name}: digest"
        );
        assert!(
            strategy["params_schema"].is_object(),
            "{name}: params_schema"
        );
        assert!(
            strategy["member_schema"].is_object(),
            "{name}: member_schema"
        );
    }
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

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

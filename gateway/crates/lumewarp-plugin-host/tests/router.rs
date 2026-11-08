//! The router strategy adapter against the real wasm components in
//! `target/plugins/router`, built by `bash gateway/plugins/build.sh --tests`.
//!
//! Each test skips with a message when the fixtures are absent, so
//! `cargo test -p lumewarp-plugin-host` stays green on a machine that has not
//! built them; CI builds first.

use lumewarp_plugin_host::{types, Registry, RouterInstance};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    sync::atomic::Ordering,
    time::{Duration, Instant},
};

/// What the gateway passes for `Router.plugin_timeout_ms` in these tests.
const TIMEOUT: Duration = Duration::from_secs(60);

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins/router")
}
fn load(dir: &Path) -> Option<Registry> {
    if !dir.exists() {
        eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
        return None;
    }
    let mut registry = Registry::default();
    registry.load_dir(dir, 1).unwrap();
    Some(registry)
}
fn request() -> types::RequestInfo {
    types::RequestInfo {
        requested: "r".into(),
        team_id: String::new(),
        key_id: String::new(),
        estimated_input_tokens: None,
        headers: Vec::new(),
    }
}
fn candidate(index: u32) -> types::Candidate {
    types::Candidate {
        index,
        member_index: index,
        weight: 1,
        in_flight: 0,
        latency_ewma_us: None,
        load: None,
    }
}
fn four_members() -> types::RouterInfo {
    types::RouterInfo {
        name: "r".into(),
        params_json: "{}".into(),
        members: (0..4)
            .map(|i| types::MemberInfo {
                model: format!("m{i}"),
                weight: 1,
                split: None,
                params_json: format!("{{\"input_price_nano_usd_per_mtok\":{i}}}"),
            })
            .collect(),
    }
}

#[tokio::test]
async fn cheapest_matches_python_pick_table() {
    let Some(registry) = load(&fixtures()) else {
        return;
    };
    let table: Value = serde_json::from_str(include_str!(
        "../../../plugins/router/cheapest-first/testdata/pick-table.json"
    ))
    .unwrap();
    for case in table["cases"].as_array().unwrap() {
        let members = case["members"].as_array().unwrap();
        let info = types::RouterInfo {
            name: "r".into(),
            params_json: case["params"].to_string(),
            members: members
                .iter()
                .map(|m| types::MemberInfo {
                    model: m["model"].as_str().unwrap().into(),
                    weight: 1,
                    split: None,
                    params_json: m["params"].to_string(),
                })
                .collect(),
        };
        let instance =
            RouterInstance::configure(registry.get(1, "cheapest_first").unwrap().clone(), info)
                .unwrap();
        let candidates: Vec<_> = case["eligible"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .filter(|(_, v)| v.as_bool().unwrap())
            .map(|(i, _)| {
                let mut c = candidate(i as u32);
                c.in_flight = case["in_flight"][i].as_u64().unwrap() as u32;
                c
            })
            .collect();
        let picked = instance.pick(&request(), &candidates, 42, TIMEOUT).await;
        assert_eq!(
            picked.map(|i| members[i as usize]["model"].clone()),
            if case["pick"].is_null() {
                None
            } else {
                Some(case["pick"].clone())
            },
            "{}",
            case["name"]
        );
    }
}

#[tokio::test]
async fn other_worlds_load_without_request_adapters() {
    let root = fixtures().join("../../plugins-test");
    if !root.join("auth/noop-test.wasm").exists() {
        eprintln!("SKIP: build with bash gateway/plugins/build.sh --tests");
        return;
    }
    let mut registry = Registry::default();
    registry.load_dir(&root.join("auth"), 2).unwrap();
    registry.load_dir(&root.join("a2a-middleware"), 3).unwrap();
    assert!(registry.get(2, "noop_test").is_some());
    assert!(registry.get(3, "noop_test").is_some());
}

#[test]
fn an_unchanged_router_reuses_its_pool_and_its_instances() {
    let dir = fixtures().join("../../target/plugins/router");
    let Some(registry) = load(&dir) else { return };
    let plugin = registry.get(1, "cheapest_first").unwrap().clone();
    let first = RouterInstance::configure(plugin.clone(), four_members()).unwrap();
    let second = RouterInstance::configure(plugin.clone(), four_members()).unwrap();
    assert!(
        std::sync::Arc::ptr_eq(first.pool(), second.pool()),
        "an unchanged router keeps the warm pool across applies"
    );
    let mut changed = four_members();
    changed.params_json = r#"{"max_input_price_nano_usd_per_mtok":2}"#.into();
    let third = RouterInstance::configure(plugin, changed).unwrap();
    assert!(!std::sync::Arc::ptr_eq(first.pool(), third.pool()));
}

/// The warm pick benchmark from T1, re-run after the async rewrite.
#[tokio::test]
#[ignore = "manual ns/op measurement; run with --ignored --nocapture"]
async fn benchmark_four_candidates() {
    let registry = load(&fixtures()).expect("build fixtures");
    let plugin = registry.get(1, "cheapest_first").unwrap();
    let instance = RouterInstance::configure(plugin.clone(), four_members()).unwrap();
    let candidates: Vec<_> = (0..4).map(candidate).collect();
    let request = request();
    for _ in 0..100 {
        std::hint::black_box(instance.pick(&request, &candidates, 1, TIMEOUT).await);
    }
    let start = Instant::now();
    let n = 20_000;
    for _ in 0..n {
        std::hint::black_box(instance.pick(&request, &candidates, 1, TIMEOUT).await);
    }
    println!(
        "wasm pick (4 candidates): {} ns/op; timeouts={}",
        start.elapsed().as_nanos() / n as u128,
        plugin.timeouts.load(Ordering::Relaxed)
    );
}

/// p99 of the picks right after a snapshot apply, with real concurrency: before
/// F1 each apply rebuilt the pool and only one instance was warm; an unchanged
/// router now keeps its whole warm pool.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
#[ignore = "manual latency measurement; run with --ignored --nocapture"]
async fn benchmark_cold_after_apply() {
    let registry = load(&fixtures()).expect("build fixtures");
    let plugin = registry.get(1, "cheapest_first").unwrap();
    let candidates: Vec<_> = (0..4).map(candidate).collect();
    let mut samples = Vec::new();
    for _ in 0..20 {
        // What `RoutingTable::compile` does for one unchanged router.
        let instance =
            std::sync::Arc::new(RouterInstance::configure(plugin.clone(), four_members()).unwrap());
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let instance = std::sync::Arc::clone(&instance);
                let candidates = candidates.clone();
                tokio::spawn(async move {
                    let start = Instant::now();
                    std::hint::black_box(instance.pick(&request(), &candidates, 1, TIMEOUT).await);
                    start.elapsed()
                })
            })
            .collect();
        for handle in handles {
            samples.push(handle.await.unwrap());
        }
    }
    samples.sort();
    let p50 = samples[samples.len() / 2];
    let p99 = samples[samples.len() * 99 / 100];
    println!(
        "cold router after apply (8 concurrent picks): p50 {:.1} us, p99 {:.1} us, max {:.1} us",
        p50.as_secs_f64() * 1e6,
        p99.as_secs_f64() * 1e6,
        samples.last().unwrap().as_secs_f64() * 1e6
    );
}

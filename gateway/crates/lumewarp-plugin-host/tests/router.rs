use lumewarp_plugin_host::{types, Registry, RouterInstance};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    sync::atomic::Ordering,
    time::Instant,
};
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
#[test]
fn cheapest_matches_python_pick_table() {
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
        // A busy CI scheduler may preempt the calling thread inside its 1 ms
        // wall deadline. Retry only when that interruption was counted.
        let plugin = registry.get(1, "cheapest_first").unwrap();
        let mut picked = None;
        for _ in 0..10 {
            let before = plugin.timeouts.load(Ordering::Relaxed);
            picked = instance.pick(&request(), &candidates, 42);
            if before == plugin.timeouts.load(Ordering::Relaxed) {
                break;
            }
        }
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
#[test]
fn other_worlds_load_without_request_adapters() {
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
fn epoch_interrupt_is_counted_and_falls_through() {
    let dir = fixtures().join("../../plugins-test/router");
    let Some(registry) = load(&dir) else { return };
    let plugin = registry.get(1, "loop_test").unwrap();
    let instance = RouterInstance::configure(
        plugin.clone(),
        types::RouterInfo {
            name: "r".into(),
            params_json: "{}".into(),
            members: Vec::new(),
        },
    )
    .unwrap();
    let start = Instant::now();
    assert_eq!(
        instance.pick(&request(), &[candidate(0)], 1).or(Some(7)),
        Some(7)
    );
    assert!(start.elapsed().as_millis() < 100);
    assert_eq!(plugin.timeouts.load(Ordering::Relaxed), 1);
    assert_eq!(plugin.traps.load(Ordering::Relaxed), 0);
}
#[test]
#[ignore = "manual ns/op measurement; run with --ignored --nocapture"]
fn benchmark_four_candidates() {
    let registry = load(&fixtures()).expect("build fixtures");
    let plugin = registry.get(1, "cheapest_first").unwrap();
    let instance = RouterInstance::configure(
        plugin.clone(),
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
        },
    )
    .unwrap();
    let candidates: Vec<_> = (0..4).map(candidate).collect();
    let request = request();
    for _ in 0..100 {
        std::hint::black_box(instance.pick(&request, &candidates, 1));
    }
    let start = Instant::now();
    let n = 100_000;
    for _ in 0..n {
        std::hint::black_box(instance.pick(&request, &candidates, 1));
    }
    println!(
        "wasm pick (4 candidates): {} ns/op; timeouts={}",
        start.elapsed().as_nanos() / n,
        plugin.timeouts.load(Ordering::Relaxed)
    );
}

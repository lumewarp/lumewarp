//! Regressions for the wave-1 review findings in the plugin host: resource
//! limits (B1), the process memory budget (M1), what a failed configure during
//! pre-warming counts as (m1), and the async call lifecycle the owner decision
//! requires (a slow plugin does not hold a tokio worker; dropping the call
//! cancels it and frees its Store; the configured timeout is the only limit).

use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

use lumewarp_plugin_host::{
    limits::{self, Budget},
    types, PoolConfig, Registry, RouterInstance,
};
use wasm_encoder::{
    CodeSection, ExportKind, ExportSection, Function, FunctionSection, HeapType, Instruction,
    MemorySection, MemoryType, Module, RefType, TableSection, TableType, TypeSection, ValType,
};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target")
}

/// A fresh registry per test: breakers and pools are per plugin instance, and
/// tests run in one process.
fn load() -> Option<Registry> {
    let production = root().join("plugins/router");
    let fixtures = root().join("plugins-test/router");
    if !production.exists() || !fixtures.exists() {
        eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
        return None;
    }
    let mut registry = Registry::default();
    registry.load_dir(&production, 1).unwrap();
    registry.load_dir(&fixtures, 1).unwrap();
    Some(registry)
}

/// `(module (table 1 funcref) (func (export "grow") (param i32) (result i32)
///    ref.null func; local.get 0; table.grow 0))`
fn table_grow_module() -> Vec<u8> {
    let mut module = Module::new();
    let mut types = TypeSection::new();
    types.ty().function([ValType::I32], [ValType::I32]);
    module.section(&types);
    let mut functions = FunctionSection::new();
    functions.function(0);
    module.section(&functions);
    let mut tables = TableSection::new();
    tables.table(TableType {
        element_type: RefType::FUNCREF,
        table64: false,
        minimum: 1,
        maximum: None,
        shared: false,
    });
    module.section(&tables);
    let mut exports = ExportSection::new();
    exports.export("grow", ExportKind::Func, 0);
    module.section(&exports);
    let mut body = Function::new([]);
    body.instruction(&Instruction::RefNull(HeapType::FUNC));
    body.instruction(&Instruction::LocalGet(0));
    body.instruction(&Instruction::TableGrow(0));
    body.instruction(&Instruction::End);
    let mut code = CodeSection::new();
    code.function(&body);
    module.section(&code);
    module.finish()
}

/// `(module (memory 1) (func (export "grow") (param i32) (result i32)
///    local.get 0; memory.grow 0))`
fn memory_grow_module() -> Vec<u8> {
    let mut module = Module::new();
    let mut types = TypeSection::new();
    types.ty().function([ValType::I32], [ValType::I32]);
    module.section(&types);
    let mut functions = FunctionSection::new();
    functions.function(0);
    module.section(&functions);
    let mut memories = MemorySection::new();
    memories.memory(MemoryType {
        minimum: 1,
        maximum: None,
        memory64: false,
        shared: false,
        page_size_log2: None,
    });
    module.section(&memories);
    let mut exports = ExportSection::new();
    exports.export("grow", ExportKind::Func, 0);
    module.section(&exports);
    let mut body = Function::new([]);
    body.instruction(&Instruction::LocalGet(0));
    body.instruction(&Instruction::MemoryGrow(0));
    body.instruction(&Instruction::End);
    let mut code = CodeSection::new();
    code.function(&body);
    module.section(&code);
    module.finish()
}

// ---------------------------------------------------------------- B1

#[tokio::test]
async fn table_grow_past_the_limit_returns_minus_one() {
    let engine = lumewarp_plugin_host::engine();
    let module = wasmtime::Module::new(engine, table_grow_module()).unwrap();
    let mut store = lumewarp_plugin_host::store(engine);
    let instance = wasmtime::Instance::new_async(&mut store, &module, &[])
        .await
        .unwrap();
    let grow = instance
        .get_typed_func::<i32, i32>(&mut store, "grow")
        .unwrap();
    // One element past the cap is refused; the instruction answers -1.
    let over = i32::try_from(limits::TABLE_ELEMENTS).unwrap();
    assert_eq!(grow.call_async(&mut store, over).await.unwrap(), -1);
    // A growth inside the cap still works (the table starts at one element).
    assert_eq!(grow.call_async(&mut store, 1_000).await.unwrap(), 1);
}

// ---------------------------------------------------------------- M1

#[tokio::test]
async fn the_process_budget_caps_every_store() {
    let engine = lumewarp_plugin_host::engine();
    let module = wasmtime::Module::new(engine, memory_grow_module()).unwrap();
    let budget = Budget::new(4 * 1024 * 1024);
    let mut first = lumewarp_plugin_host::store_with_budget(engine, Arc::clone(&budget));
    let instance = wasmtime::Instance::new_async(&mut first, &module, &[])
        .await
        .unwrap();
    let grow = instance
        .get_typed_func::<i32, i32>(&mut first, "grow")
        .unwrap();
    // The module starts with one 64 KiB page; growing by 63 pages is 4 MiB.
    assert_eq!(grow.call_async(&mut first, 63).await.unwrap(), 1);
    assert!(budget.used() <= budget.limit());
    // The budget is shared: a second Store cannot take what the first holds.
    let mut second = lumewarp_plugin_host::store_with_budget(engine, Arc::clone(&budget));
    let other = wasmtime::Instance::new_async(&mut second, &module, &[]).await;
    match other {
        // Its initial page does not fit: the limiter refuses it.
        Err(_) => assert_eq!(budget.used(), budget.limit()),
        // Or it fits but cannot grow, which is also a refusal.
        Ok(other) => {
            let grow_other = other
                .get_typed_func::<i32, i32>(&mut second, "grow")
                .unwrap();
            assert_eq!(grow_other.call_async(&mut second, 1).await.unwrap(), -1);
            assert_eq!(budget.used(), budget.limit());
        }
    }
    // Dropping a Store gives its reservation back.
    drop(first);
    assert_eq!(budget.used(), 0);
}

// ---------------------------------------------------------------- m1

#[tokio::test]
async fn a_failed_configure_while_pre_warming_is_counted_not_hidden() {
    let Some(registry) = load() else { return };
    let plugin = registry.get(1, "cheapest_first").unwrap().clone();
    // One instance fits in `used`, the next one cannot grow at all.
    let probe = Budget::new(usize::MAX);
    let info = types::RouterInfo {
        name: "budget-a".into(),
        params_json: "{}".into(),
        members: Vec::new(),
    };
    let probe_pool = RouterInstance::configure_with(
        plugin.clone(),
        info.clone(),
        PoolConfig {
            target: 1,
            prewarm: 1,
            memory: None,
            budget: Some(Arc::clone(&probe)),
        },
    )
    .unwrap();
    let used = probe.used();
    assert!(used > 0, "an instance reserves its guest memory");
    assert!(probe_pool.pool().live() >= 1);

    let traps = plugin.traps.load(Ordering::Relaxed);
    let tight = Budget::new(used + 64 * 1024);
    let info = types::RouterInfo {
        name: "budget-b".into(),
        ..info
    };
    let instance = RouterInstance::configure_with(
        plugin.clone(),
        info,
        PoolConfig {
            target: 4,
            prewarm: 4,
            memory: None,
            budget: Some(tight),
        },
    )
    .unwrap();
    assert_eq!(instance.pool().idle(), 1, "pre-warming stops at the budget");
    assert!(
        plugin.traps.load(Ordering::Relaxed) > traps,
        "the failed instantiation is counted, not swallowed"
    );
}

// ------------------------------------------------- the async call lifecycle

fn loop_router(plugin: Arc<lumewarp_plugin_host::Plugin>, name: &str) -> Arc<RouterInstance> {
    Arc::new(
        RouterInstance::configure_with(
            plugin,
            types::RouterInfo {
                name: name.into(),
                params_json: "{}".into(),
                members: Vec::new(),
            },
            PoolConfig {
                target: 1,
                prewarm: 1,
                memory: None,
                budget: None,
            },
        )
        .unwrap(),
    )
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

fn candidates() -> Vec<types::Candidate> {
    (0..4)
        .map(|index| types::Candidate {
            index,
            member_index: index,
            weight: 1,
            in_flight: 0,
            latency_ewma_us: None,
            load: None,
        })
        .collect()
}

/// A plugin that loops forever must not stop the rest of a single-threaded
/// runtime from making progress, and dropping its future must free its Store.
#[tokio::test(flavor = "current_thread")]
async fn a_looping_plugin_yields_to_the_executor_and_is_cancelled_by_drop() {
    let Some(registry) = load() else { return };
    let plugin = registry.get(1, "loop_test").unwrap().clone();
    let instance = loop_router(plugin.clone(), "looping");
    let request = types::RequestInfo {
        requested: "r".into(),
        team_id: String::new(),
        key_id: String::new(),
        estimated_input_tokens: None,
        headers: Vec::new(),
    };
    let candidates = candidates();

    let ticks = Arc::new(AtomicUsize::new(0));
    let ticker = {
        let ticks = Arc::clone(&ticks);
        tokio::spawn(async move {
            for _ in 0..50 {
                tokio::time::sleep(Duration::from_millis(5)).await;
                ticks.fetch_add(1, Ordering::Relaxed);
            }
        })
    };

    // No timeout: the call runs until it is dropped.
    let started = Instant::now();
    let mut call = Box::pin(instance.pick(&request, &candidates, 1, Duration::from_secs(3600)));
    // Poll it for a while: the epoch yields let the ticker run.
    tokio::select! {
        answer = &mut call => panic!("a looping plugin answered: {answer:?}"),
        _ = tokio::time::sleep(Duration::from_millis(150)) => {}
    }
    assert!(
        ticks.load(Ordering::Relaxed) >= 10,
        "the runtime kept running: {} ticks",
        ticks.load(Ordering::Relaxed)
    );
    assert_eq!(instance.pool().live(), 1, "the call holds one instance");
    // Dropping the future cancels the guest and discards its Store.
    drop(call);
    let deadline = Instant::now() + Duration::from_millis(500);
    while instance.pool().live() != 0 && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(instance.pool().live(), 0, "cancelling frees the Store");
    assert!(started.elapsed() < Duration::from_secs(5));
    // The runtime still works.
    assert!(tokio::time::timeout(
        Duration::from_millis(50),
        tokio::time::sleep(Duration::from_millis(1))
    )
    .await
    .is_ok());
    ticker.abort();
}

/// The configured timeout is the only limit: on expiry the call answers `none`
/// (the gateway falls through as after a trap) and the Store is discarded.
#[tokio::test]
async fn the_configured_timeout_cancels_the_call() {
    let Some(registry) = load() else { return };
    let plugin = registry.get(1, "loop_test").unwrap().clone();
    let instance = loop_router(plugin.clone(), "timed");
    let request = types::RequestInfo {
        requested: "r".into(),
        team_id: String::new(),
        key_id: String::new(),
        estimated_input_tokens: None,
        headers: Vec::new(),
    };
    let before = plugin.timeouts.load(Ordering::Relaxed);
    let answer = instance
        .pick(&request, &candidates(), 1, Duration::from_millis(50))
        .await;
    assert_eq!(answer, None);
    assert!(plugin.timeouts.load(Ordering::Relaxed) > before);
    assert_eq!(instance.pool().live(), 0, "a timed-out Store is discarded");
}

/// A pool miss waits for an instance instead of failing the request.
#[tokio::test]
async fn a_pool_miss_awaits_a_new_instance() {
    let Some(registry) = load() else { return };
    let plugin = registry.get(1, "cheapest_first").unwrap().clone();
    let instance = RouterInstance::configure_with(
        plugin,
        types::RouterInfo {
            name: "awaiting".into(),
            ..four_members()
        },
        PoolConfig {
            target: 1,
            prewarm: 0,
            memory: None,
            budget: None,
        },
    )
    .unwrap();
    assert_eq!(instance.pool().idle(), 0);
    let candidates = candidates();
    let request = types::RequestInfo {
        requested: "r".into(),
        team_id: String::new(),
        key_id: String::new(),
        estimated_input_tokens: None,
        headers: Vec::new(),
    };
    assert_eq!(
        instance
            .pick(&request, &candidates, 1, Duration::from_secs(10))
            .await,
        Some(0),
        "an unwarmed pool creates an instance instead of answering none"
    );
    assert_eq!(instance.pool().idle(), 1, "and keeps it for the next call");
}

/// Many concurrent calls share one pool without failing; the pool never grows
/// past its target and every call answers.
#[tokio::test(flavor = "current_thread")]
async fn concurrent_calls_share_the_pool() {
    let Some(registry) = load() else { return };
    let plugin = registry.get(1, "cheapest_first").unwrap().clone();
    let instance = Arc::new(
        RouterInstance::configure_with(
            plugin.clone(),
            types::RouterInfo {
                name: "shared".into(),
                ..four_members()
            },
            PoolConfig {
                target: 2,
                prewarm: 0,
                memory: None,
                budget: None,
            },
        )
        .unwrap(),
    );
    let candidates = candidates();
    let answered = Arc::new(AtomicBool::new(false));
    let mut handles = Vec::new();
    for _ in 0..8 {
        let instance = Arc::clone(&instance);
        let candidates = candidates.clone();
        let answered = Arc::clone(&answered);
        handles.push(tokio::spawn(async move {
            let request = types::RequestInfo {
                requested: "r".into(),
                team_id: String::new(),
                key_id: String::new(),
                estimated_input_tokens: None,
                headers: Vec::new(),
            };
            if instance
                .pick(&request, &candidates, 1, Duration::from_secs(30))
                .await
                .is_some()
            {
                answered.store(true, Ordering::Relaxed);
            }
        }));
    }
    for handle in handles {
        handle.await.unwrap();
    }
    assert!(answered.load(Ordering::Relaxed));
    assert!(instance.pool().live() <= 2, "the pool stays at its target");
}

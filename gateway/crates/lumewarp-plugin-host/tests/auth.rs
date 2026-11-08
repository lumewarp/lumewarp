//! The `upstream-auth` adapter against the real wasm components in
//! `target/plugins-test/auth`, built by `bash gateway/plugins/build.sh --tests`.
//!
//! Each test skips with a message when the fixtures are absent, so
//! `cargo test -p lumewarp-plugin-host` stays green on a machine that has not
//! built them; CI builds first.

use std::{
    net::SocketAddr,
    path::Path,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, OnceLock,
    },
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::Full;
use hyper::{body::Incoming, service::service_fn, Request, Response};
use hyper_util::rt::TokioIo;
use lumewarp_plugin_host::{
    authenticator::{self, Authenticator, Cache, Error, Outbound, Spec, Traffic},
    Registry,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;

/// What the gateway passes for `PluginRef.timeout_ms` in these tests.
const TIMEOUT: Duration = Duration::from_secs(60);

fn init() -> bool {
    static READY: OnceLock<bool> = OnceLock::new();
    *READY.get_or_init(|| {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/auth");
        if !dir.exists() {
            eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
            return false;
        }
        let mut registry = Registry::default();
        if let Err(error) = registry.load_dir(&dir, 2) {
            eprintln!("SKIP: {error:#}");
            return false;
        }
        if lumewarp_plugin_host::install(registry).is_err() {
            eprintln!("SKIP: plugins already installed");
            return false;
        }
        true
    })
}

fn digest(params: &str) -> [u8; 32] {
    Sha256::digest(params.as_bytes()).into()
}

fn spec(traffic: Traffic, id: &str, params: serde_json::Value) -> Spec {
    let params_json = params.to_string();
    Spec {
        traffic,
        id: id.to_owned(),
        name: format!("target-{id}"),
        url: "https://upstream.example.test/mcp".to_owned(),
        plugin: "auth_fixture".to_owned(),
        digest: digest(&params_json),
        params_json,
    }
}

fn request() -> Outbound<'static> {
    Outbound {
        method: "POST",
        url: "https://upstream.example.test/mcp",
        headers: vec![("content-type".to_owned(), b"application/json".to_vec())],
        body: b"{}",
    }
}

fn header<'a>(headers: &'a authenticator::Headers, name: &str) -> &'a str {
    headers
        .headers
        .iter()
        .find(|(header, _)| header.eq_ignore_ascii_case(name))
        .map(|(_, value)| std::str::from_utf8(value).unwrap())
        .unwrap_or("<absent>")
}

async fn configured(traffic: Traffic, id: &str, params: serde_json::Value) -> Arc<Authenticator> {
    Cache::new()
        .get_or_configure(&spec(traffic, id, params))
        .expect("configure")
}

/// A local HTTP server that answers every request with `body` after `delay`.
async fn serve(delay: Duration, body: &'static str) -> (SocketAddr, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let requests = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&requests);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let counter = Arc::clone(&counter);
            tokio::spawn(async move {
                let service = service_fn(move |_: Request<Incoming>| {
                    let counter = Arc::clone(&counter);
                    async move {
                        counter.fetch_add(1, Ordering::Relaxed);
                        tokio::time::sleep(delay).await;
                        Ok::<_, std::convert::Infallible>(Response::new(Full::new(
                            Bytes::from_static(body.as_bytes()),
                        )))
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (addr, requests)
}

#[tokio::test]
async fn a_cached_result_is_reused_within_cache_ms() {
    if !init() {
        return;
    }
    let auth = configured(
        Traffic::Mcp,
        "cache-hit",
        serde_json::json!({
            "mode": "fixed",
            "cache_ms": 60_000,
            "headers": ["authorization: Bearer plugin", "x-custom: v"],
        }),
    )
    .await;
    let first = auth.authorize(request(), TIMEOUT).await.unwrap();
    let second = auth.authorize(request(), TIMEOUT).await.unwrap();
    // `x-calls` counts the guest's own calls: one, then a cache hit.
    assert_eq!(header(&first, "x-calls"), "1");
    assert_eq!(header(&second, "x-calls"), "1");
    assert_eq!(header(&first, "authorization"), "Bearer plugin");
    assert_eq!(header(&second, "x-custom"), "v");
    assert_eq!(first.cache_ms, 60_000);
}

#[tokio::test]
async fn a_cached_result_expires_after_cache_ms() {
    if !init() {
        return;
    }
    let auth = configured(
        Traffic::Mcp,
        "cache-expiry",
        serde_json::json!({"mode": "fixed", "cache_ms": 20}),
    )
    .await;
    assert_eq!(
        header(
            &auth.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "1"
    );
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(
        header(
            &auth.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "2"
    );
}

#[tokio::test]
async fn cache_ms_zero_is_never_reused() {
    if !init() {
        return;
    }
    let auth = configured(
        Traffic::Mcp,
        "no-cache",
        serde_json::json!({"mode": "fixed", "cache_ms": 0}),
    )
    .await;
    assert_eq!(
        header(
            &auth.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "1"
    );
    assert_eq!(
        header(
            &auth.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "2"
    );
}

#[tokio::test]
async fn a_token_refresh_is_not_stampeded() {
    if !init() {
        return;
    }
    let auth = configured(
        Traffic::Mcp,
        "single-flight",
        serde_json::json!({"mode": "fixed", "cache_ms": 60_000, "delay_ms": 200}),
    )
    .await;
    let mut handles = Vec::new();
    for _ in 0..8 {
        let auth = Arc::clone(&auth);
        handles.push(tokio::spawn(async move {
            auth.authorize(request(), TIMEOUT).await.unwrap()
        }));
    }
    for handle in handles {
        assert_eq!(header(&handle.await.unwrap(), "x-calls"), "1");
    }
}

/// The configured timeout is the only limit for an auth plugin too; it drops the
/// call, discards the Store and is counted.
#[tokio::test]
async fn the_configured_timeout_cancels_an_auth_call() {
    if !init() {
        return;
    }
    let plugin = lumewarp_plugin_host::global()
        .get(2, "auth_fixture")
        .expect("fixture loaded")
        .clone();
    let before = plugin.timeouts.load(Ordering::Relaxed);
    let auth = configured(
        Traffic::A2a,
        "loop-timeout",
        serde_json::json!({"mode": "loop"}),
    )
    .await;
    let error = auth
        .authorize(request(), Duration::from_millis(50))
        .await
        .unwrap_err();
    assert!(matches!(error, Error::Timeout), "{error:?}");
    assert!(plugin.timeouts.load(Ordering::Relaxed) > before);
}

/// A looping auth plugin must not hold the tokio worker: other tasks keep
/// running on a single-threaded runtime, and dropping the call cancels it and
/// frees its Store.
#[tokio::test(flavor = "current_thread")]
async fn a_looping_auth_plugin_yields_and_is_cancelled_by_drop() {
    if !init() {
        return;
    }
    let plugin = lumewarp_plugin_host::global()
        .get(2, "auth_fixture")
        .expect("fixture loaded")
        .clone();
    let auth = configured(
        Traffic::Mcp,
        "loop-drop",
        serde_json::json!({"mode": "loop"}),
    )
    .await;
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
    let mut call = Box::pin(auth.authorize(request(), Duration::from_secs(3600)));
    tokio::select! {
        answer = &mut call => panic!("a looping plugin answered: {answer:?}"),
        _ = tokio::time::sleep(Duration::from_millis(150)) => {}
    }
    assert!(
        ticks.load(Ordering::Relaxed) >= 10,
        "the runtime kept running: {} ticks",
        ticks.load(Ordering::Relaxed)
    );
    let held = auth.live();
    drop(call);
    let deadline = Instant::now() + Duration::from_millis(500);
    while auth.live() != held - 1 && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(auth.live(), held - 1, "cancelling frees the Store");
    ticker.abort();
    let _ = plugin;
}

#[tokio::test]
async fn http_fetch_outside_allowed_origins_never_connects() {
    if !init() {
        return;
    }
    let (addr, requests) = serve(Duration::ZERO, "token").await;
    let auth = configured(
        Traffic::Mcp,
        "fetch-denied",
        serde_json::json!({
            "mode": "fetch",
            "url": format!("http://127.0.0.1:{}/token", addr.port()),
            "allowed_origins": ["https://auth.example.test"],
        }),
    )
    .await;
    let error = auth.authorize(request(), TIMEOUT).await.unwrap_err();
    let message = error.to_string();
    assert!(message.contains("allowed_origins"), "{message}");
    assert_eq!(
        requests.load(Ordering::Relaxed),
        0,
        "no connection was made"
    );
}

#[tokio::test]
async fn http_fetch_reaches_an_allowed_origin() {
    if !init() {
        return;
    }
    let (addr, requests) = serve(Duration::ZERO, "token-123").await;
    let auth = configured(
        Traffic::Mcp,
        "fetch-allowed",
        serde_json::json!({
            "mode": "fetch",
            "url": format!("http://127.0.0.1:{}/token", addr.port()),
            "allowed_origins": [format!("http://127.0.0.1:{}", addr.port())],
        }),
    )
    .await;
    let headers = auth.authorize(request(), TIMEOUT).await.unwrap();
    assert_eq!(header(&headers, "x-fetch-status"), "200");
    assert_eq!(header(&headers, "x-fetch-body"), "token-123");
    assert_eq!(requests.load(Ordering::Relaxed), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn a_slow_http_fetch_does_not_block_a_single_threaded_runtime() {
    if !init() {
        return;
    }
    let (addr, _) = serve(Duration::from_millis(300), "slow-token").await;
    let auth = configured(
        Traffic::Mcp,
        "fetch-slow",
        serde_json::json!({
            "mode": "fetch",
            "url": format!("http://127.0.0.1:{}/token", addr.port()),
            "allowed_origins": [format!("http://127.0.0.1:{}", addr.port())],
        }),
    )
    .await;
    let ticks = Arc::new(AtomicUsize::new(0));
    let ticker = {
        let ticks = Arc::clone(&ticks);
        tokio::spawn(async move {
            for _ in 0..10 {
                tokio::time::sleep(Duration::from_millis(20)).await;
                ticks.fetch_add(1, Ordering::Relaxed);
            }
        })
    };
    let headers = auth.authorize(request(), TIMEOUT).await.unwrap();
    assert_eq!(header(&headers, "x-fetch-body"), "slow-token");
    let elapsed_ticks = ticks.load(Ordering::Relaxed);
    assert!(
        elapsed_ticks >= 5,
        "only {elapsed_ticks} ticks ran while http-fetch was pending"
    );
    ticker.abort();
}

#[tokio::test]
async fn secrets_come_only_from_the_plugin_prefix() {
    if !init() {
        return;
    }
    std::env::set_var("LUMEWARP_PLUGIN_SECRET_FIXTURE_KEY", "s3cr3t");
    let allowed = configured(
        Traffic::Mcp,
        "secret-allowed",
        serde_json::json!({"mode": "secret", "secret_name": "FIXTURE_KEY"}),
    )
    .await;
    assert_eq!(
        header(
            &allowed.authorize(request(), TIMEOUT).await.unwrap(),
            "x-secret"
        ),
        "s3cr3t"
    );
    // `PATH` is set in every process, but `secret()` only reads the prefix.
    let refused = configured(
        Traffic::Mcp,
        "secret-refused",
        serde_json::json!({"mode": "secret", "secret_name": "PATH"}),
    )
    .await;
    assert_eq!(
        header(
            &refused.authorize(request(), TIMEOUT).await.unwrap(),
            "x-secret"
        ),
        "none"
    );
}

#[tokio::test]
async fn unchanged_settings_keep_the_configured_plugin() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    let first_spec = spec(
        Traffic::Mcp,
        "reuse",
        serde_json::json!({"mode": "fixed", "cache_ms": 60_000}),
    );
    let first = cache.get_or_configure(&first_spec).unwrap();
    assert_eq!(
        header(
            &first.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "1"
    );
    // A snapshot that did not change the target reuses the instance, so the
    // cached token (and the guest's counter) survives.
    cache.replace(std::slice::from_ref(&first_spec)).unwrap();
    let second = cache.get(Traffic::Mcp, "reuse").expect("still configured");
    assert!(Arc::ptr_eq(&first, &second));
    assert_eq!(
        header(
            &second.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "1"
    );
    // Different settings are a different target: a fresh instance, counter 1.
    let mut changed = first_spec.clone();
    changed.params_json = serde_json::json!({"mode": "fixed", "cache_ms": 1}).to_string();
    changed.digest = digest(&changed.params_json);
    cache.replace(&[changed]).unwrap();
    let third = cache.get(Traffic::Mcp, "reuse").unwrap();
    assert!(!Arc::ptr_eq(&first, &third));
    assert_eq!(
        header(
            &third.authorize(request(), TIMEOUT).await.unwrap(),
            "x-calls"
        ),
        "1"
    );
    // A target that disappeared releases its entry.
    cache.replace(&[]).unwrap();
    assert!(cache.get(Traffic::Mcp, "reuse").is_none());
}

#[tokio::test]
async fn duplicate_target_ids_refuse_the_configuration() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    let one = spec(
        Traffic::Mcp,
        "duplicate",
        serde_json::json!({"mode": "fixed"}),
    );
    let mut other = one.clone();
    other.name = "another".to_owned();
    let error = cache.replace(&[one, other]).unwrap_err();
    assert!(error.to_string().contains("share the id"), "{error}");
    // The refused configuration left no entry behind.
    assert!(cache.get(Traffic::Mcp, "duplicate").is_none());
}

#[tokio::test]
async fn a_bad_configure_refuses_and_does_not_pollute_the_cache() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    // The apply path refuses the version: `configure` runs there, under the
    // configure budget.
    let bad = spec(
        Traffic::Mcp,
        "bad-mode",
        serde_json::json!({"mode": "nope"}),
    );
    let refused = cache.stage(std::slice::from_ref(&bad)).unwrap_err();
    assert!(
        matches!(refused.source, Error::Configure(_)),
        "{:?}",
        refused.source
    );
    assert!(cache.get(Traffic::Mcp, "bad-mode").is_none());
    // The request path configures lazily (no `block_on` on a worker): the same
    // bad settings surface as a 502 on the first call instead.
    let lazy = cache.get_or_configure(&bad).unwrap();
    let error = lazy.authorize(request(), TIMEOUT).await.unwrap_err();
    assert!(matches!(error, Error::Configure(_)), "{error:?}");
    // A missing plugin is its own error, not a configure one.
    let mut missing = spec(Traffic::Mcp, "missing", serde_json::json!({}));
    missing.plugin = "not_loaded".to_owned();
    assert!(matches!(
        cache.get_or_configure(&missing).unwrap_err(),
        Error::Missing(_)
    ));
    // Malformed settings are refused before the sandbox.
    assert!(matches!(
        cache
            .get_or_configure(&spec(
                Traffic::Mcp,
                "bad-origins",
                serde_json::json!({"mode": "fixed", "allowed_origins": "https://a.test"})
            ))
            .unwrap_err(),
        Error::Params(_)
    ));
}

// ------------------------------------------------ review-w2-gw M4 and m1

/// An apply-time `configure` that never returns is refused once the configure
/// budget elapses, and nothing of that target is published (M4).
#[tokio::test]
async fn a_configure_that_never_returns_refuses_the_version() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    let started = Instant::now();
    let refused = cache
        .stage_within(
            &[spec(
                Traffic::Mcp,
                "stuck-configure",
                serde_json::json!({"mode": "stuck_configure"}),
            )],
            Duration::from_millis(150),
        )
        .unwrap_err();
    assert!(
        matches!(refused.source, Error::ConfigureTimeout),
        "{:?}",
        refused.source
    );
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(cache.get(Traffic::Mcp, "stuck-configure").is_none());
}

/// A staged set does not serve until the version is accepted (m1).
#[tokio::test]
async fn a_staged_set_serves_only_after_publish() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    let target = spec(
        Traffic::Mcp,
        "staged",
        serde_json::json!({"mode": "fixed", "cache_ms": 0}),
    );
    let staged = cache.stage(std::slice::from_ref(&target)).unwrap();
    assert_eq!(staged.len(), 1);
    assert!(
        cache.get(Traffic::Mcp, "staged").is_none(),
        "a staged set must not serve before the version is accepted"
    );
    cache.publish(staged);
    assert!(cache.get(Traffic::Mcp, "staged").is_some());
}

/// The request-path fallback configures lazily: no instance, no `block_on`
/// pre-warm on a tokio worker (m1).
#[test]
fn the_request_path_configures_without_prewarming() {
    if !init() {
        return;
    }
    let cache = Cache::new();
    let auth = cache
        .get_or_configure(&spec(
            Traffic::Mcp,
            "lazy",
            serde_json::json!({"mode": "fixed", "cache_ms": 0}),
        ))
        .unwrap();
    assert_eq!(
        auth.idle(),
        0,
        "the request path must not warm a pool synchronously"
    );
}

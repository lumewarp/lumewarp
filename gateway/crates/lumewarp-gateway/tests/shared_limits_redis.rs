//! End-to-end shared-limits test: two standalone gateways, one Redis, one key.
//!
//! The direct store/script tests live inside the crate (`shared_limits::
//! redis_store::tests`) because this package is a binary with no library
//! target; this file drives the real processes the way an operator would.
//! It skips unless `LUMEWARP_TEST_REDIS_URL` is set (CI runs a redis:7
//! service and sets it).

mod common;

use std::{
    net::TcpListener as StdListener,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

use bytes::Bytes;
use common::{bounded, client, Behavior, Mock};
use http_body_util::Full;
use hyper::Request;
use sha2::{Digest, Sha256};

static NEXT: AtomicU64 = AtomicU64::new(0);

fn redis_url() -> Option<String> {
    std::env::var("LUMEWARP_TEST_REDIS_URL")
        .ok()
        .filter(|url| !url.trim().is_empty())
}

fn secret_hash() -> String {
    hex::encode(Sha256::digest(b"shared-secret"))
}

struct Process {
    child: Child,
    addr: String,
    dir: PathBuf,
}

impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn start(upstream: &str, redis: &str, namespace: &str) -> Process {
    let listen = StdListener::bind("127.0.0.1:0").unwrap();
    let admin = StdListener::bind("127.0.0.1:0").unwrap();
    let addr = listen.local_addr().unwrap();
    let admin_addr = admin.local_addr().unwrap();
    drop((listen, admin));
    let dir = std::env::temp_dir().join(format!(
        "lumewarp-shared-test-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let yaml = format!(
        "server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n\
         upstreams:\n  - {{name: u, kind: openai_compatible, base_url: 'http://{upstream}/v1'}}\n\
         models:\n  - {{type: llm, name: d, upstream: u, upstream_model: actual}}\n\
         routers:\n  - {{name: public, members: [{{model: d}}]}}\n\
         keys:\n  - {{key_id: k, key_sha256: '{}', allow_all_models: true, rate_limits: {{requests_per_minute: 10}}}}\n",
        secret_hash()
    );
    let config = dir.join("config.yaml");
    std::fs::write(&config, yaml).unwrap();
    let stdout = std::fs::File::create(dir.join("stdout.log")).unwrap();
    let stderr = std::fs::File::create(dir.join("stderr.log")).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
        .arg("--config")
        .arg(&config)
        .arg("--shared-limits-redis-url")
        .arg(redis)
        .arg("--shared-limits-namespace")
        .arg(namespace)
        .arg("--shared-limits-sync-ms")
        .arg("10")
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .env("RUST_LOG", "error")
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .unwrap();
    Process {
        child,
        addr: format!("http://{addr}"),
        dir,
    }
}

async fn wait_ready(process: &Process) {
    let client = client();
    bounded(async {
        loop {
            let request = Request::builder()
                .uri(format!("{}/healthz", process.addr))
                .body(Full::new(Bytes::new()))
                .unwrap();
            if let Ok(response) = client.request(request).await {
                if response.status() == 200 {
                    return;
                }
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await;
}

async fn post(process: &Process) -> u16 {
    let request = Request::builder()
        .method("POST")
        .uri(format!("{}/v1/chat/completions", process.addr))
        .header("authorization", "Bearer shared-secret")
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(b"{\"model\":\"public\"}")))
        .unwrap();
    let response = bounded(client().request(request)).await.unwrap();
    let status = response.status().as_u16();
    // Drain the body so the connection is reusable and the proxy finishes.
    let _ = bounded(http_body_util::BodyExt::collect(response.into_body())).await;
    status
}

async fn cleanup(url: &str, namespace: &str) {
    let Ok(client) = redis::Client::open(url) else {
        return;
    };
    let Ok(mut connection) = client.get_multiplexed_async_connection().await else {
        return;
    };
    let pattern = format!("{{lw:{namespace}}}:*");
    let mut cursor = 0u64;
    loop {
        let Ok((next, keys)) = redis::cmd("SCAN")
            .arg(cursor)
            .arg("MATCH")
            .arg(&pattern)
            .arg("COUNT")
            .arg(1000)
            .query_async::<(u64, Vec<String>)>(&mut connection)
            .await
        else {
            return;
        };
        if !keys.is_empty() {
            let _: Result<i64, _> = redis::cmd("DEL")
                .arg(&keys)
                .query_async(&mut connection)
                .await;
        }
        cursor = next;
        if cursor == 0 {
            return;
        }
    }
}

#[tokio::test]
async fn shared_limits_two_gateways_share_one_window() {
    let Some(url) = redis_url() else {
        return;
    };
    let mock = Mock::start(Behavior::Json).await;
    let namespace = format!(
        "e2e-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    );
    let upstream = mock.addr.to_string();
    let a = start(&upstream, &url, &namespace);
    let b = start(&upstream, &url, &namespace);
    wait_ready(&a).await;
    wait_ready(&b).await;
    // Give both gateways one sync interval to learn about the other before the
    // burst, so the fair share (not the local limit) decides.
    tokio::time::sleep(Duration::from_millis(50)).await;

    let mut admitted = 0;
    let mut limited = 0;
    for i in 0..30 {
        let status = post(if i % 2 == 0 { &a } else { &b }).await;
        match status {
            200 => admitted += 1,
            429 => limited += 1,
            other => panic!("unexpected status {other}"),
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // One shared window of 10, with the design's at-most-n over-admission.
    assert!(
        (10..=12).contains(&admitted),
        "admitted {admitted}, limited {limited}"
    );
    assert!(limited > 0, "the shared window must eventually reject");
    cleanup(&url, &namespace).await;
}

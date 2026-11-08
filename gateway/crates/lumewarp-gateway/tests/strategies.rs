//! Startup plugin descriptors and standalone validation.
use serde_json::Value;
use std::{path::PathBuf, process::Command};
mod common;

#[tokio::test]
async fn looping_plugin_times_out_and_request_is_served_by_fallback() {
    use bytes::Bytes;
    use http_body_util::{BodyExt, Full};
    use hyper::Request;
    use std::time::Duration;
    let plugin_dir =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/router");
    if !plugin_dir.join("loop-test.wasm").exists() {
        eprintln!("SKIP: build loop fixture with bash gateway/plugins/build.sh --tests");
        return;
    }
    let upstream = common::Mock::start(common::Behavior::Json).await;
    let listen = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listen.local_addr().unwrap();
    let admin = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let admin_addr = admin.local_addr().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    std::fs::write(&path, format!("server: {{listen: '{addr}', admin_listen: '{admin_addr}'}}\nupstreams: [{{name: u, kind: vllm, base_url: 'http://{}/prefix/v1'}}]\nmodels: [{{name: m, upstream: u, upstream_model: actual}}]\nrouters: [{{name: public, strategy: loop_test, plugin_timeout_ms: 500, members: [{{model: m}}], fallbacks: [m], retry: {{max_attempts: 2}}}}]\n", upstream.addr)).unwrap();
    drop(listen);
    drop(admin);
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let mut process = Child(
        Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .env("LUMEWARP_ROUTER_PLUGIN_DIR", plugin_dir)
            .env_remove("LUMEWARP_AUTH_PLUGIN_DIR")
            .env_remove("LUMEWARP_A2A_MIDDLEWARE_DIR")
            .spawn()
            .unwrap(),
    );
    let client = common::client();
    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            assert!(process.0.try_wait().unwrap().is_none(), "gateway exited");
            let req = Request::builder()
                .uri(format!("http://{admin_addr}/readyz"))
                .body(Full::new(Bytes::new()))
                .unwrap();
            if let Ok(response) = client.request(req).await {
                if response.status() == 200 {
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    let request = Request::builder()
        .method("POST")
        .uri(format!("http://{addr}/v1/chat/completions"))
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(
            br#"{"model":"public","messages":[{"role":"user","content":"hello"}]}"#,
        )))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    response.into_body().collect().await.unwrap();
    let request = Request::builder()
        .uri(format!("http://{admin_addr}/metrics"))
        .body(Full::new(Bytes::new()))
        .unwrap();
    let metrics = client
        .request(request)
        .await
        .unwrap()
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes();
    let metrics = String::from_utf8_lossy(&metrics);
    assert!(metrics.contains("lumewarp_plugin_calls_total{kind=\"router-strategy\",plugin=\"loop_test\",result=\"timeout\"} 1"), "{metrics}");
}

/// A WASM strategy that answers an ineligible or out-of-range index is counted
/// as a trap, and the request takes the router's fall-through paths instead of
/// being sent to the wrong model (review M1).
#[tokio::test]
async fn an_ineligible_pick_answer_is_a_trap_and_the_fallback_serves() {
    use bytes::Bytes;
    use http_body_util::{BodyExt, Full};
    use hyper::Request;
    use std::time::Duration;
    let plugin_dir =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/router");
    if !plugin_dir.join("misbehave-test.wasm").exists() {
        eprintln!("SKIP: build the fixture with bash gateway/plugins/build.sh --tests");
        return;
    }
    let upstream = common::Mock::start(common::Behavior::Json).await;
    let listen = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listen.local_addr().unwrap();
    let admin = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let admin_addr = admin.local_addr().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    // The router has one member (position 0); the strategy answers 7, which is
    // not an eligible candidate, so the fallback model has to serve.
    std::fs::write(&path, format!("server: {{listen: '{addr}', admin_listen: '{admin_addr}'}}\nupstreams: [{{name: u, kind: vllm, base_url: 'http://{}/prefix/v1'}}]\nmodels: [{{name: m, upstream: u, upstream_model: actual}}]\nrouters: [{{name: public, strategy: misbehave_test, plugin_timeout_ms: 2000, params: {{mode: bad_index, index: 7}}, members: [{{model: m}}], fallbacks: [m]}}]\n", upstream.addr)).unwrap();
    drop(listen);
    drop(admin);
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let mut process = Child(
        Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .env("LUMEWARP_ROUTER_PLUGIN_DIR", plugin_dir)
            .env_remove("LUMEWARP_AUTH_PLUGIN_DIR")
            .env_remove("LUMEWARP_A2A_MIDDLEWARE_DIR")
            .spawn()
            .unwrap(),
    );
    let client = common::client();
    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            assert!(process.0.try_wait().unwrap().is_none(), "gateway exited");
            let req = Request::builder()
                .uri(format!("http://{admin_addr}/readyz"))
                .body(Full::new(Bytes::new()))
                .unwrap();
            if let Ok(response) = client.request(req).await {
                if response.status() == 200 {
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    let request = Request::builder()
        .method("POST")
        .uri(format!("http://{addr}/v1/chat/completions"))
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(
            br#"{"model":"public","messages":[{"role":"user","content":"hello"}]}"#,
        )))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200, "the fallback serves");
    response.into_body().collect().await.unwrap();
    let metrics = client
        .request(
            Request::builder()
                .uri(format!("http://{admin_addr}/metrics"))
                .body(Full::new(Bytes::new()))
                .unwrap(),
        )
        .await
        .unwrap()
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes();
    let metrics = String::from_utf8_lossy(&metrics);
    assert!(
        metrics.contains(
            "lumewarp_plugin_calls_total{kind=\"router-strategy\",plugin=\"misbehave_test\",result=\"trap\"} 1"
        ),
        "{metrics}"
    );
}

fn command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_lumewarp"));
    for var in [
        "LUMEWARP_ROUTER_PLUGIN_DIR",
        "LUMEWARP_AUTH_PLUGIN_DIR",
        "LUMEWARP_A2A_MIDDLEWARE_DIR",
        "LUMEWARP_CONFIG",
        "LUMEWARP_CONTROL_PLANE",
    ] {
        command.env_remove(var);
    }
    command.args(["plugins", "--json"]);
    command
}
#[test]
fn builtin_descriptors_need_no_configuration() {
    let output = command().output().unwrap();
    assert!(output.status.success(), "{output:?}");
    let descriptors: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(descriptors.len(), 7);
    for name in [
        "weighted",
        "least_in_flight",
        "lowest_latency",
        "vllm_load",
        "none",
        "static_bearer",
        "passthrough",
    ] {
        let d = descriptors.iter().find(|d| d["name"] == name).unwrap();
        assert_eq!(d["builtin"], true);
        assert_eq!(d["wasm_sha256"], "");
        assert_eq!(d["digest"].as_str().unwrap().len(), 64);
    }
}
fn fixture() -> Option<PathBuf> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../target/plugins/router/cheapest-first.wasm");
    if !path.exists() {
        eprintln!("SKIP: build fixtures with bash gateway/plugins/build.sh");
        return None;
    }
    Some(path)
}
#[test]
fn wrong_kind_and_duplicate_are_fatal() {
    let Some(fixture) = fixture() else { return };
    let dir = tempfile::tempdir().unwrap();
    std::fs::copy(&fixture, dir.path().join("a.wasm")).unwrap();
    let wrong = command()
        .env("LUMEWARP_AUTH_PLUGIN_DIR", dir.path())
        .output()
        .unwrap();
    assert!(!wrong.status.success());
    let error = String::from_utf8_lossy(&wrong.stderr);
    assert!(
        error.contains("a.wasm") && error.contains("expected auth world"),
        "{error}"
    );
    std::fs::copy(fixture, dir.path().join("b.wasm")).unwrap();
    let duplicate = command()
        .env("LUMEWARP_ROUTER_PLUGIN_DIR", dir.path())
        .output()
        .unwrap();
    assert!(!duplicate.status.success());
    assert!(
        String::from_utf8_lossy(&duplicate.stderr).contains("duplicate plugin name cheapest_first")
    );
}
#[test]
fn missing_directory_is_fatal() {
    let dir = tempfile::tempdir().unwrap();
    assert!(!command()
        .env("LUMEWARP_ROUTER_PLUGIN_DIR", dir.path().join("missing"))
        .output()
        .unwrap()
        .status
        .success());
}
#[test]
fn builtin_member_params_fail_without_leaking_values() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    std::fs::write(&path, "upstreams: [{name: u, kind: vllm, base_url: 'http://127.0.0.1:9/v1'}]\nmodels: [{name: m, upstream: u, upstream_model: x}]\nrouters: [{name: r, strategy: weighted, members: [{model: m, params: {zone: private}}]}]\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
        .arg("--config")
        .arg(path)
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("strategy configuration failed"), "{stderr}");
    assert!(!stderr.contains("private"));
}

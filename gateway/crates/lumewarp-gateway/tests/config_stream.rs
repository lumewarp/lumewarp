//! Control-plane mode against an in-process fake `ConfigService`. The gateway
//! runs as a real child process; the fake drives the protocol step by step.

mod common;
#[path = "common/fake_cp.rs"]
mod fake_cp;

use std::{
    net::{SocketAddr, TcpListener as StdListener},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

use bytes::Bytes;
use common::{bounded, bytes, client, Behavior, Mock, Prometheus, TestClient};
use fake_cp::{Conn, FakeCp};
use http_body_util::Full;
use hyper::Request;
use lumewarp_proto::config::v1::{
    config_update::Update, gateway_message::Msg, Alias, ApiKey, ConfigUpdate, Delta,
    EffectivePolicy, KeyStatus, Model, NackCode, PluginKind, Router, RouterMember, Snapshot,
    Upstream, UpstreamKind,
};
use prost::Message;
use sha2::{Digest, Sha256};

const SCOPE: &str = "scope-a";
const TOKEN_ENV: &str = "LUMEWARP_G2_TEST_TOKEN";

fn key(id: &str, secret: &str) -> ApiKey {
    ApiKey {
        key_id: id.into(),
        key_hash: Sha256::digest(secret.as_bytes()).to_vec(),
        team_id: String::new(),
        status: KeyStatus::Active as i32,
        expires_at_unix_ms: 0,
        policy: Some(EffectivePolicy {
            allow_all_models: true,
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn snapshot(version: u64, keys: &[(&str, &str)]) -> Snapshot {
    Snapshot {
        version,
        upstreams: vec![Upstream {
            name: "u".into(),
            kind: UpstreamKind::Vllm as i32,
            base_url: "http://127.0.0.1:1/v1".into(),
            timeout_ms: 1000,
            ..Default::default()
        }],
        models: vec![Model {
            r#type: 1,
            name: "d".into(),
            upstream: "u".into(),
            upstream_model: "actual".into(),
            price: None,
            ..Default::default()
        }],
        routers: vec![Router {
            name: "m1".into(),
            members: vec![RouterMember {
                model: "d".into(),
                weight: 1,
                ..Default::default()
            }],
            strategy: "weighted".into(),
            ..Default::default()
        }],
        required_capabilities: vec!["strategy:weighted".into()],
        api_keys: keys.iter().map(|(id, secret)| key(id, secret)).collect(),
        created_at_unix_ms: 1,
        scope: SCOPE.into(),
        ..Default::default()
    }
}

fn digest(version: u64) -> Vec<u8> {
    format!("opaque-digest-{version}").into_bytes()
}

fn full(snapshot: Snapshot) -> ConfigUpdate {
    ConfigUpdate {
        target_digest: digest(snapshot.version),
        update: Some(Update::Full(snapshot)),
    }
}

fn delta(base: u64, version: u64, add: &[(&str, &str)]) -> ConfigUpdate {
    ConfigUpdate {
        target_digest: digest(version),
        update: Some(Update::Delta(Delta {
            base_version: base,
            version,
            upsert_api_keys: add.iter().map(|(id, secret)| key(id, secret)).collect(),
            required_capabilities: vec!["strategy:weighted".into()],
            created_at_unix_ms: 2,
            ..Default::default()
        })),
    }
}

/// The persisted file is `magic || payload || digest || len(digest) u32 BE || sha256(all before)`.
fn on_disk(dir: &Path) -> Option<(u64, Vec<u8>)> {
    let data = std::fs::read(dir.join("snapshot.pb")).ok()?;
    let (body, checksum) = data.split_at(data.len().checked_sub(32)?);
    assert_eq!(Sha256::digest(body).as_slice(), checksum, "checksum");
    let (rest, len) = body.split_at(body.len() - 4);
    let rest = rest.strip_prefix(b"LWSNAP02").expect("magic");
    let len = u32::from_be_bytes(len.try_into().unwrap()) as usize;
    let (payload, digest) = rest.split_at(rest.len() - len);
    Some((Snapshot::decode(payload).unwrap().version, digest.to_vec()))
}

fn on_disk_version(dir: &Path) -> Option<u64> {
    on_disk(dir).map(|(version, _)| version)
}

struct Gw {
    child: Child,
    addr: SocketAddr,
    admin: SocketAddr,
    http: TestClient,
    dir: PathBuf,
}

impl Gw {
    fn spawn(control_plane: &str, state: &Path, work: &Path, name: &str) -> Self {
        Self::spawn_with_env(control_plane, state, work, name, &[])
    }

    fn spawn_with_env(
        control_plane: &str,
        state: &Path,
        work: &Path,
        name: &str,
        vars: &[(&str, &str)],
    ) -> Self {
        let listen = StdListener::bind("127.0.0.1:0").unwrap();
        let admin = StdListener::bind("127.0.0.1:0").unwrap();
        let (addr, admin_addr) = (listen.local_addr().unwrap(), admin.local_addr().unwrap());
        let local = work.join(format!("{name}.yaml"));
        std::fs::write(
            &local,
            format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n"),
        )
        .unwrap();
        drop((listen, admin));
        let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .args(["--control-plane", control_plane])
            .args(["--gateway-token-env", TOKEN_ENV])
            .arg("--state-dir")
            .arg(state)
            .args(["--scope", SCOPE])
            .arg("--local-config")
            .arg(local)
            .env(TOKEN_ENV, "gw-token")
            .envs(vars.iter().copied())
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env("RUST_LOG", "error")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        Self {
            child,
            addr,
            admin: admin_addr,
            http: client(),
            dir: state.to_owned(),
        }
    }

    async fn get(&self, addr: SocketAddr, path: &str, bearer: Option<&str>) -> (u16, String) {
        let mut request = Request::builder().uri(format!("http://{addr}{path}"));
        if let Some(secret) = bearer {
            request = request.header("authorization", format!("Bearer {secret}"));
        }
        let request = request.body(Full::new(Bytes::new())).unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            match bounded(self.http.request(request.clone())).await {
                Ok(response) => {
                    let status = response.status().as_u16();
                    let body = String::from_utf8_lossy(&bytes(response).await).into_owned();
                    return (status, body);
                }
                Err(err) if tokio::time::Instant::now() > deadline => {
                    panic!("gateway unreachable: {err}")
                }
                Err(_) => tokio::time::sleep(Duration::from_millis(20)).await,
            }
        }
    }

    /// `/v1/models` status for a key secret.
    async fn models(&self, secret: &str) -> u16 {
        self.get(self.addr, "/v1/models", Some(secret)).await.0
    }

    async fn readyz(&self) -> u16 {
        self.get(self.admin, "/readyz", None).await.0
    }

    async fn metrics(&self) -> Prometheus {
        let (status, body) = self.get(self.admin, "/metrics", None).await;
        assert_eq!(status, 200);
        Prometheus::parse(&body)
    }

    async fn wait_models(&self, secret: &str, want: u16) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while self.models(secret).await != want {
            assert!(
                tokio::time::Instant::now() < deadline,
                "models never became {want}"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
}

impl Drop for Gw {
    fn drop(&mut self) {
        let _ = std::fs::set_permissions(&self.dir, std::fs::Permissions::from_mode(0o700));
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The generated `<host>-<8 lowercase hex>` id from `Hello.gateway_id`.
fn assert_generated_gateway_id(id: &str) {
    assert!(!id.is_empty() && id.len() <= 100, "{id}");
    assert!(
        id.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')),
        "{id}"
    );
    let (host, suffix) = id.rsplit_once('-').expect("a host and a suffix");
    assert!(!host.is_empty(), "{id}");
    assert_eq!(suffix.len(), 8, "{id}");
    assert!(
        suffix
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()),
        "{id}"
    );
}

async fn hello(conn: &mut Conn) -> lumewarp_proto::config::v1::Hello {
    match conn.recv().await.msg {
        Some(Msg::Hello(hello)) => hello,
        other => panic!("expected Hello, got {other:?}"),
    }
}

/// Receives the next ACK and checks the version is already on disk.
async fn ack(conn: &mut Conn, version: u64, state: &Path) {
    match conn.recv().await.msg {
        Some(Msg::Ack(ack)) => {
            assert_eq!(ack.version, version);
            assert_eq!(
                on_disk(state),
                Some((version, digest(version))),
                "ACK before the snapshot and its digest were durable"
            );
        }
        other => panic!("expected Ack({version}), got {other:?}"),
    }
}

async fn recv_nack(conn: &mut Conn) -> lumewarp_proto::config::v1::Nack {
    match conn.recv().await.msg {
        Some(Msg::Nack(nack)) => nack,
        other => panic!("expected Nack, got {other:?}"),
    }
}

struct Env {
    _tmp: tempfile::TempDir,
    state: PathBuf,
    work: PathBuf,
}

fn env() -> Env {
    let tmp = tempfile::tempdir().unwrap();
    let work = tmp.path().to_owned();
    let state = work.join("state");
    Env {
        _tmp: tmp,
        state,
        work,
    }
}

#[tokio::test]
async fn full_then_delta_with_durable_acks() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    assert_eq!(conn.authorization.as_deref(), Some("Bearer gw-token"));
    let hello = hello(&mut conn).await;
    assert_generated_gateway_id(&hello.gateway_id);
    assert_eq!(hello.have_version, 0);
    assert!(hello.have_digest.is_empty());
    assert_eq!(hello.scope, "");
    assert!(!hello.gateway_version.is_empty());
    // The binary links every strategy feature; this test only relies on the
    // built-ins. `tests/strategies.rs` covers the custom entries.
    let builtins = ["least_in_flight", "lowest_latency", "vllm_load", "weighted"];
    let routers: Vec<_> = hello
        .plugins
        .iter()
        .filter(|d| d.kind == PluginKind::RouterStrategy as i32)
        .collect();
    let names: Vec<_> = routers
        .iter()
        .map(|d| d.name.as_str())
        .filter(|name| builtins.contains(name))
        .collect();
    assert_eq!(names, builtins);
    let vllm = routers
        .iter()
        .find(|d| d.name == "vllm_load")
        .expect("vllm_load is built in");
    assert!(vllm.builtin);
    let info = vllm.router.as_ref().expect("router metadata");
    assert_eq!(info.needs, ["vllm_load"]);
    assert!(!info.uses_weight);
    assert_eq!(hello.plugin_abi, 1);
    assert_eq!(vllm.digest.len(), 64);
    assert_eq!(
        info.member_schema_json,
        r#"{"additionalProperties":false,"type":"object"}"#
    );
    assert_eq!(gw.readyz().await, 503);

    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;
    assert_eq!(gw.readyz().await, 200);
    assert_eq!(gw.models("secret-1").await, 200);
    assert_eq!(gw.models("secret-2").await, 401);

    conn.send(delta(1, 2, &[("k2", "secret-2")]));
    ack(&mut conn, 2, &env.state).await;
    assert_eq!(gw.models("secret-1").await, 200);
    assert_eq!(gw.models("secret-2").await, 200);

    let metrics = gw.metrics().await;
    assert_eq!(metrics.value("lumewarp_config_version", &[]), 2.0);
    assert_eq!(metrics.value("lumewarp_config_stream_connected", &[]), 1.0);
    let mode = std::fs::metadata(&env.state).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o700);
    let mode = std::fs::metadata(env.state.join("snapshot.pb"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[tokio::test]
async fn base_mismatch_nack_then_full() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;

    conn.send(delta(5, 6, &[("k2", "secret-2")]));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::BaseMismatch);
    assert_eq!((nack.version, nack.have_version), (6, 1));
    assert_eq!(gw.models("secret-2").await, 401);

    conn.send(full(snapshot(6, &[("k1", "secret-1"), ("k2", "secret-2")])));
    ack(&mut conn, 6, &env.state).await;
    assert_eq!(gw.models("secret-2").await, 200);
    let metrics = gw.metrics().await;
    assert_eq!(
        metrics.value(
            "lumewarp_config_apply_failures_total",
            &[("code", "base_mismatch")]
        ),
        1.0
    );
}

#[tokio::test]
async fn invalid_snapshot_is_nacked_without_swap() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;

    let mut bad = snapshot(2, &[("k2", "secret-2")]);
    bad.routers[0].members[0].model = "missing".into();
    conn.send(full(bad));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::InvalidConfig);
    assert_eq!((nack.version, nack.have_version), (2, 1));
    assert!(nack.reason.contains("missing"), "{}", nack.reason);

    let mut unsupported = snapshot(3, &[]);
    unsupported.required_capabilities = vec!["from-the-future".into()];
    conn.send(full(unsupported));
    assert_eq!(nack_code(&mut conn).await, NackCode::UnsupportedFeature);

    // An unresolvable upstream secret is also INVALID_CONFIG.
    let mut unresolved = snapshot(4, &[]);
    unresolved.upstreams[0].api_key_ref = "LUMEWARP_G2_NO_SUCH_VAR".into();
    conn.send(full(unresolved));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::InvalidConfig);

    assert_eq!(on_disk_version(&env.state), Some(1));
    assert_eq!(gw.models("secret-1").await, 200);
    assert_eq!(gw.models("secret-2").await, 401);
    let metrics = gw.metrics().await;
    assert_eq!(metrics.value("lumewarp_config_version", &[]), 1.0);
    assert_eq!(
        metrics.value(
            "lumewarp_config_apply_failures_total",
            &[("code", "invalid_config")]
        ),
        2.0
    );
}

#[tokio::test]
async fn mixed_model_types_nack_without_replacing_the_active_snapshot() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-types");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;
    let mut mixed = snapshot(2, &[]);
    let mut embedding = mixed.models[0].clone();
    embedding.name = "embedding".into();
    embedding.r#type = lumewarp_proto::config::v1::ModelType::Embedding as i32;
    mixed.models.push(embedding);
    mixed.routers[0].members.push(RouterMember {
        model: "embedding".into(),
        weight: 1,
        ..Default::default()
    });
    mixed.required_capabilities.push("model_types".into());
    conn.send(full(mixed));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::InvalidConfig);
    assert_eq!(nack.have_version, 1);
    assert!(nack.reason.contains("one model type"), "{}", nack.reason);
    assert_eq!(on_disk_version(&env.state), Some(1));
    assert_eq!(gw.models("secret-1").await, 200);
}

async fn nack_code(conn: &mut Conn) -> NackCode {
    recv_nack(conn).await.code()
}

#[tokio::test]
async fn persist_failure_nacks_and_keeps_serving() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;

    std::fs::set_permissions(&env.state, std::fs::Permissions::from_mode(0o500)).unwrap();
    if std::fs::File::create(env.state.join("probe")).is_ok() {
        return; // permissions are not enforced (e.g. running as root)
    }
    conn.send(delta(1, 2, &[("k2", "secret-2")]));
    let failed = recv_nack(&mut conn).await;
    assert_eq!(failed.code(), NackCode::PersistFailed);
    assert_eq!((failed.version, failed.have_version), (2, 1));
    assert!(!failed.reason.contains(env.state.to_str().unwrap()));
    assert_eq!(gw.models("secret-1").await, 200);
    assert_eq!(gw.models("secret-2").await, 401, "must not swap");
    assert_eq!(on_disk_version(&env.state), Some(1));

    // Control plane retries the same version once the disk recovers.
    std::fs::set_permissions(&env.state, std::fs::Permissions::from_mode(0o700)).unwrap();
    conn.send(delta(1, 2, &[("k2", "secret-2")]));
    ack(&mut conn, 2, &env.state).await;
    assert_eq!(gw.models("secret-2").await, 200);
}

#[tokio::test]
async fn restart_with_cache_serves_with_control_plane_down() {
    let env = env();
    let mut cp = FakeCp::start().await;
    {
        let _gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
        let mut conn = cp.accept().await;
        hello(&mut conn).await;
        conn.send(full(snapshot(7, &[("k1", "secret-1")])));
        ack(&mut conn, 7, &env.state).await;
    }
    drop(cp); // control plane is gone

    let dead = {
        let probe = StdListener::bind("127.0.0.1:0").unwrap();
        format!("http://{}", probe.local_addr().unwrap())
    };
    let gw = Gw::spawn(&dead, &env.state, &env.work, "gw-2");
    gw.wait_models("secret-1", 200).await;
    // The persisted digest is offered once a control plane is reachable.
    let mut cp = FakeCp::start().await;
    let gw3 = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-3");
    let mut conn = cp.accept().await;
    let resumed = hello(&mut conn).await;
    assert_eq!((resumed.have_version, resumed.have_digest), (7, digest(7)));
    drop(gw3);
    assert_eq!(gw.readyz().await, 200);
    let metrics = gw.metrics().await;
    assert_eq!(metrics.value("lumewarp_config_version", &[]), 7.0);
    assert_eq!(metrics.value("lumewarp_config_stream_connected", &[]), 0.0);
}

#[tokio::test]
async fn cold_boot_without_cache_is_not_ready_until_full() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    assert_eq!(gw.readyz().await, 503);
    let (status, body) = gw.get(gw.addr, "/v1/models", Some("secret-1")).await;
    assert_eq!(status, 503);
    assert!(body.contains("not_ready"), "{body}");

    // A delta cannot bootstrap a gateway with no base.
    conn.send(delta(4, 5, &[("k1", "secret-1")]));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::BaseMismatch);
    assert_eq!(nack.have_version, 0);
    assert_eq!(gw.readyz().await, 503);

    conn.send(full(snapshot(2, &[("k1", "secret-1")])));
    ack(&mut conn, 2, &env.state).await;
    assert_eq!(gw.readyz().await, 200);
    assert_eq!(gw.models("secret-1").await, 200);
}

/// A file written before the catalog change: same trailer, no magic, routers
/// with the old field numbers. Protobuf would decode it into a snapshot with
/// empty routers, so only the format check keeps it from being a resume base.
fn previous_format_file(version: u64) -> Vec<u8> {
    let mut file = vec![0x08, version as u8];
    // routers: Router { name: "m1", models: ["d"], strategy: WEIGHTED }
    file.extend_from_slice(&[
        0x22, 0x09, 0x0a, 0x02, b'm', b'1', 0x12, 0x01, b'd', 0x18, 0x01,
    ]);
    let digest = digest(version);
    file.extend_from_slice(&digest);
    file.extend_from_slice(&(digest.len() as u32).to_be_bytes());
    let checksum = Sha256::digest(&file);
    file.extend_from_slice(&checksum);
    file
}

#[tokio::test]
async fn previous_format_or_garbage_cache_is_discarded_for_a_fresh_full() {
    for cached in [previous_format_file(9), vec![0xff; 200], Vec::new()] {
        let env = env();
        std::fs::create_dir_all(&env.state).unwrap();
        std::fs::write(env.state.join("snapshot.pb"), &cached).unwrap();

        let mut cp = FakeCp::start().await;
        let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
        let mut conn = cp.accept().await;
        let resumed = hello(&mut conn).await;
        assert_eq!(resumed.have_version, 0, "not offered as a base");
        assert!(resumed.have_digest.is_empty());
        assert_eq!(gw.readyz().await, 503, "not served either");

        conn.send(full(snapshot(3, &[("k1", "secret-1")])));
        ack(&mut conn, 3, &env.state).await;
        assert_eq!(gw.readyz().await, 200);
        assert_eq!(gw.models("secret-1").await, 200);
        assert_eq!(on_disk_version(&env.state), Some(3), "file replaced");
    }
}

#[tokio::test]
async fn unknown_strategy_is_unsupported_and_keeps_the_last_good_config() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;

    let mut future = snapshot(2, &[("k1", "secret-1")]);
    future.routers[0].strategy = "not_compiled_in".into();
    future.required_capabilities = vec!["strategy:not_compiled_in".into()];
    conn.send(full(future));
    assert_eq!(nack_code(&mut conn).await, NackCode::UnsupportedFeature);
    assert_eq!(gw.models("secret-1").await, 200);
    assert_eq!(on_disk_version(&env.state), Some(1));
}

#[tokio::test]
async fn delta_changes_aliases_on_the_stream() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    let mut first = snapshot(1, &[("k1", "secret-1")]);
    first.aliases = vec![Alias {
        name: "old-name".into(),
        target: "m1".into(),
    }];
    conn.send(full(first));
    ack(&mut conn, 1, &env.state).await;
    let names = |body: String| -> Vec<String> {
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        v["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["id"].as_str().unwrap().to_owned())
            .collect()
    };
    let (_, body) = gw.get(gw.addr, "/v1/models", Some("secret-1")).await;
    assert_eq!(names(body), ["d", "m1", "old-name"]);

    let mut update = delta(1, 2, &[]);
    let Some(Update::Delta(d)) = &mut update.update else {
        unreachable!()
    };
    d.upsert_aliases = vec![Alias {
        name: "new-name".into(),
        target: "d".into(),
    }];
    d.remove_aliases = vec!["old-name".into()];
    conn.send(update);
    ack(&mut conn, 2, &env.state).await;
    let (_, body) = gw.get(gw.addr, "/v1/models", Some("secret-1")).await;
    assert_eq!(names(body), ["d", "m1", "new-name"]);
}

#[tokio::test]
async fn wrong_scope_cache_and_full_are_refused() {
    let env = env();
    std::fs::create_dir_all(&env.state).unwrap();
    let mut foreign = snapshot(9, &[("k1", "secret-1")]);
    foreign.scope = "other".into();
    let mut file = b"LWSNAP02".to_vec();
    file.extend_from_slice(&foreign.encode_to_vec());
    file.extend_from_slice(&0u32.to_be_bytes());
    let checksum = Sha256::digest(&file);
    file.extend_from_slice(&checksum);
    std::fs::write(env.state.join("snapshot.pb"), file).unwrap();

    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    let hello = hello(&mut conn).await;
    assert_eq!(hello.have_version, 0, "foreign cache is not offered");
    assert_eq!(gw.readyz().await, 503);

    let mut other = snapshot(3, &[("k1", "secret-1")]);
    other.scope = "other".into();
    conn.send(full(other));
    assert_eq!(nack_code(&mut conn).await, NackCode::InvalidConfig);
    assert_eq!(gw.readyz().await, 503);
}

#[tokio::test]
async fn reconnect_resumes_from_have_version() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let addr = cp.addr;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;
    conn.send(delta(1, 2, &[("k2", "secret-2")]));
    ack(&mut conn, 2, &env.state).await;

    // Stream dropped by the server: gateway reconnects on its own.
    drop(conn);
    let mut conn = cp.accept().await;
    let resumed = hello(&mut conn).await;
    assert_eq!((resumed.have_version, resumed.scope.as_str()), (2, SCOPE));
    assert_eq!(resumed.have_digest, digest(2));

    // Control-plane restart: the listener goes away and comes back.
    drop(conn);
    cp.shutdown().await;
    gw.wait_connected(false).await;
    assert_eq!(gw.models("secret-2").await, 200, "keeps serving while down");
    let mut cp = FakeCp::start_on(Some(addr)).await;
    let mut conn = cp.accept().await;
    let resumed = hello(&mut conn).await;
    assert_eq!(resumed.have_version, 2);
    assert_eq!(resumed.have_digest, digest(2));
    conn.send(delta(2, 3, &[("k3", "secret-3")]));
    ack(&mut conn, 3, &env.state).await;
    assert_eq!(gw.models("secret-3").await, 200);
}

impl Gw {
    async fn wait_connected(&self, want: bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            let value = self
                .metrics()
                .await
                .value("lumewarp_config_stream_connected", &[]);
            if value == f64::from(u8::from(want)) {
                return;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "connected never became {want}"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
}

#[tokio::test]
async fn cold_boot_delta_with_base_zero_is_refused_until_a_full_arrives() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-1");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;

    // base_version 0 matches the empty cold-boot placeholder numerically, but
    // no complete base exists: never ready, nothing persisted, no ACK.
    conn.send(delta(0, 1, &[("k1", "secret-1")]));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code(), NackCode::BaseMismatch);
    assert_eq!((nack.version, nack.have_version), (1, 0));
    assert_eq!(gw.readyz().await, 503);
    assert_eq!(gw.models("secret-1").await, 503);
    assert!(!env.state.join("snapshot.pb").exists());

    conn.send(full(snapshot(1, &[("k1", "secret-1")])));
    ack(&mut conn, 1, &env.state).await;
    assert_eq!(gw.readyz().await, 200);

    // With a base applied, deltas work again.
    conn.send(delta(1, 2, &[("k2", "secret-2")]));
    ack(&mut conn, 2, &env.state).await;
    assert_eq!(gw.models("secret-2").await, 200);
}

/// Sealed by the control plane's sealing.py with `ENCRYPTION_KEY`; opens to "sk-test-123".
const SEALED: &str =
    "01cf29abe2c0cabded1dee59a04699ce44e9a26482cb8e102d94504cd57206704404b13d0b01faf8";
const ENCRYPTION_KEY: &str = "test-encryption-key-0123456789abcdef";

fn sealed_snapshot(version: u64, upstream: SocketAddr) -> Snapshot {
    let mut s = snapshot(version, &[("k1", "secret-1")]);
    s.upstreams[0].base_url = format!("http://{upstream}/v1");
    s.upstreams[0].api_key_ciphertext = hex::decode(SEALED).unwrap();
    s.required_capabilities.push("encrypted_api_key".into());
    s
}

#[tokio::test]
async fn sealed_upstream_key_is_opened_with_the_encryption_key() {
    let env = env();
    let mut mock = Mock::start(Behavior::Json).await;
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn_with_env(
        &cp.url(),
        &env.state,
        &env.work,
        "gw-sealed",
        &[("LUMEWARP_ENCRYPTION_KEY", ENCRYPTION_KEY)],
    );
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(sealed_snapshot(1, mock.addr)));
    ack(&mut conn, 1, &env.state).await;
    let request = Request::builder()
        .method("POST")
        .uri(format!("http://{}/v1/chat/completions", gw.addr))
        .header("authorization", "Bearer secret-1")
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(br#"{"model":"m1"}"#)))
        .unwrap();
    let response = bounded(gw.http.request(request)).await.unwrap();
    assert_eq!(response.status(), 200);
    bytes(response).await;
    assert_eq!(
        mock.next().await.headers["authorization"],
        "Bearer sk-test-123"
    );
    // The persisted snapshot keeps the ciphertext only.
    let data = std::fs::read(env.state.join("snapshot.pb")).unwrap();
    assert!(!data.windows(11).any(|w| w == b"sk-test-123"));
}

#[tokio::test]
async fn sealed_upstream_key_without_the_encryption_key_is_nacked() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let _gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-nokey");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    conn.send(full(sealed_snapshot(1, "127.0.0.1:1".parse().unwrap())));
    let nack = recv_nack(&mut conn).await;
    assert_eq!(nack.code, NackCode::InvalidConfig as i32);
    assert!(
        nack.reason.contains("LUMEWARP_ENCRYPTION_KEY"),
        "{}",
        nack.reason
    );
}

#[tokio::test]
async fn probe_state_changes_are_reported_to_the_control_plane() {
    use lumewarp_proto::config::v1::{ChatCheck, HealthCheck, TargetHealth};
    let env = env();
    let mock = Mock::start(Behavior::Json).await;
    let mut cp = FakeCp::start().await;
    let _gw = Gw::spawn(&cp.url(), &env.state, &env.work, "gw-health");
    let mut conn = cp.accept().await;
    hello(&mut conn).await;
    let mut s = snapshot(1, &[]);
    s.upstreams[0].base_url = format!("http://{}/v1", mock.addr);
    s.upstreams.push(Upstream {
        name: "down".into(),
        kind: UpstreamKind::Vllm as i32,
        // Nothing listens on port 1: every probe fails at once.
        base_url: "http://127.0.0.1:1/v1".into(),
        timeout_ms: 1000,
        ..Default::default()
    });
    for u in &mut s.upstreams {
        u.health_check = Some(HealthCheck {
            interval_ms: 1000,
            timeout_ms: 200,
            unhealthy_threshold: 1,
            ..Default::default()
        });
    }
    s.required_capabilities.push("health_check".into());
    // A model chat check against the mock (answers any chat completion).
    s.models[0].chat_check = Some(ChatCheck {
        interval_ms: 1000,
        timeout_ms: 500,
        unhealthy_threshold: 1,
        ..Default::default()
    });
    s.required_capabilities.push("model_chat_check".into());
    let model = s.models[0].name.clone();
    conn.send(full(s));
    ack(&mut conn, 1, &env.state).await;
    let expected = vec![
        TargetHealth {
            name: "down".into(),
            healthy: false,
        },
        TargetHealth {
            name: "u".into(),
            healthy: true,
        },
    ];
    let expected_models = vec![TargetHealth {
        name: model,
        healthy: true,
    }];
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        let report = conn.recv_health().await;
        if report.upstreams == expected && report.models == expected_models {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "last report: {report:?}"
        );
    }
}

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
use common::{bounded, bytes, client, Prometheus, TestClient};
use fake_cp::{Conn, FakeCp};
use http_body_util::Full;
use hyper::Request;
use lumewarp_proto::config::v1::{
    config_update::Update, gateway_message::Msg, ApiKey, ConfigUpdate, Delta, Deployment,
    EffectivePolicy, KeyStatus, ModelRoute, NackCode, Snapshot, Upstream, UpstreamKind,
};
use prost::Message;
use sha2::{Digest, Sha256};

const SCOPE: &str = "scope-a";
const TOKEN_ENV: &str = "LUMEWARP_G2_TEST_TOKEN";

fn key(id: &str, secret: &str) -> ApiKey {
    ApiKey {
        key_id: id.into(),
        key_hash: Sha256::digest(secret.as_bytes()).to_vec(),
        org_id: "o".into(),
        team_id: String::new(),
        project_id: "p".into(),
        status: KeyStatus::Active as i32,
        expires_at_unix_ms: 0,
        policy: Some(EffectivePolicy {
            allow_all_models: true,
            ..Default::default()
        }),
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
        deployments: vec![Deployment {
            name: "d".into(),
            upstream: "u".into(),
            upstream_model: "actual".into(),
            weight: 1,
            price: None,
        }],
        routes: vec![ModelRoute {
            model: "m1".into(),
            deployments: vec!["d".into()],
            strategy: 1,
        }],
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
            created_at_unix_ms: 2,
            ..Default::default()
        })),
    }
}

/// The persisted file is `payload || digest || len(digest) u32 BE || sha256(all before)`.
fn on_disk(dir: &Path) -> Option<(u64, Vec<u8>)> {
    let data = std::fs::read(dir.join("snapshot.pb")).ok()?;
    let (body, checksum) = data.split_at(data.len().checked_sub(32)?);
    assert_eq!(Sha256::digest(body).as_slice(), checksum, "checksum");
    let (rest, len) = body.split_at(body.len() - 4);
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
    fn spawn(control_plane: &str, state: &Path, work: &Path, id: &str) -> Self {
        let listen = StdListener::bind("127.0.0.1:0").unwrap();
        let admin = StdListener::bind("127.0.0.1:0").unwrap();
        let (addr, admin_addr) = (listen.local_addr().unwrap(), admin.local_addr().unwrap());
        let local = work.join(format!("{id}.yaml"));
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
            .args(["--scope", SCOPE, "--gateway-id", id])
            .arg("--local-config")
            .arg(local)
            .env(TOKEN_ENV, "gw-token")
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
    assert_eq!(hello.gateway_id, "gw-1");
    assert_eq!(hello.have_version, 0);
    assert!(hello.have_digest.is_empty());
    assert_eq!(hello.scope, "");
    assert!(!hello.gateway_version.is_empty());
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
    bad.routes[0].deployments = vec!["missing".into()];
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

#[tokio::test]
async fn wrong_scope_cache_and_full_are_refused() {
    let env = env();
    std::fs::create_dir_all(&env.state).unwrap();
    let mut foreign = snapshot(9, &[("k1", "secret-1")]);
    foreign.scope = "other".into();
    let payload = foreign.encode_to_vec();
    let mut file = payload.clone();
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

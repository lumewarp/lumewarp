// Integration test binaries share fixtures but exercise different subsets.
#![allow(dead_code)]

use std::{
    collections::BTreeMap,
    future::Future,
    net::{SocketAddr, TcpListener as StdListener},
    path::PathBuf,
    pin::Pin,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc,
    },
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full};
use hyper::{
    body::{Body, Frame, Incoming},
    service::service_fn,
    HeaderMap, Request, Response,
};
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::{TokioExecutor, TokioIo},
};
use sha2::{Digest, Sha256};
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot},
    task::{JoinHandle, JoinSet},
};

pub const FIRST: &[u8] = b"data: {\"choices\":[{\"delta\":{\"content\":\"first\"}}]}\n\n";
pub const LAST: &[u8] =
    b"data: {\"choices\":[{\"delta\":{\"content\":\"second\"}}]}\n\ndata: [DONE]\n\n";
pub const JSON: &[u8] = b"{ \"object\":\"mock.response\", \"unknown\":[1,true] }";
pub const WAIT: Duration = Duration::from_secs(5);
pub type TestClient = Client<HttpConnector, Full<Bytes>>;
type MockBody = UnsyncBoxBody<Bytes, std::io::Error>;

pub async fn bounded<T>(future: impl Future<Output = T>) -> T {
    tokio::time::timeout(WAIT, future)
        .await
        .expect("test operation exceeded 5 seconds")
}

pub fn client() -> TestClient {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    Client::builder(TokioExecutor::new()).build(connector)
}

/// A server that counts the HTTP requests it receives and answers `200`.
///
/// An auth plugin that `http-fetch`es at `configure` time (the fixture's
/// `configure_fetch` mode) makes "was this target configured again?" observable
/// from outside the gateway process (N1).
pub async fn counter() -> (SocketAddr, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let hits = Arc::new(AtomicUsize::new(0));
    let counted = Arc::clone(&hits);
    tokio::spawn(async move {
        loop {
            let Ok((socket, _)) = listener.accept().await else {
                return;
            };
            let hits = Arc::clone(&counted);
            tokio::spawn(async move {
                let service = service_fn(move |_request: Request<Incoming>| {
                    let hits = Arc::clone(&hits);
                    async move {
                        hits.fetch_add(1, Ordering::SeqCst);
                        Ok::<_, std::convert::Infallible>(Response::new(Full::new(Bytes::new())))
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(socket), service)
                    .await;
            });
        }
    });
    (addr, hits)
}

pub async fn bytes(response: Response<Incoming>) -> Bytes {
    bounded(response.into_body().collect())
        .await
        .expect("read response body")
        .to_bytes()
}

pub struct StreamControl {
    pub frames: mpsc::Sender<Bytes>,
    pub closed: oneshot::Receiver<()>,
}

pub struct Recorded {
    pub method: hyper::Method,
    pub path: String,
    pub headers: HeaderMap,
    pub body: Bytes,
    pub stream: Option<StreamControl>,
}

struct ChannelBody {
    frames: mpsc::Receiver<Bytes>,
    closed: Option<oneshot::Sender<()>>,
    fail_on_close: bool,
}
impl Body for ChannelBody {
    type Data = Bytes;
    type Error = std::io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, std::io::Error>>> {
        match self.frames.poll_recv(cx) {
            Poll::Ready(None) if self.fail_on_close => {
                self.fail_on_close = false;
                Poll::Ready(Some(Err(std::io::Error::other(
                    "injected upstream stream failure",
                ))))
            }
            frame => frame.map(|frame| frame.map(|bytes| Ok(Frame::data(bytes)))),
        }
    }
}
impl Drop for ChannelBody {
    fn drop(&mut self) {
        if let Some(closed) = self.closed.take() {
            let _ = closed.send(());
        }
    }
}

#[derive(Clone, Copy)]
pub enum Behavior {
    Json,
    ControlledStream,
    HeadersFirstStream,
    BrokenStream,
    DelayedHeaders,
    Hang,
    ImmediateClose,
}

pub struct Mock {
    pub addr: SocketAddr,
    pub records: mpsc::UnboundedReceiver<Recorded>,
    task: JoinHandle<()>,
}
impl Mock {
    pub async fn start(behavior: Behavior) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (records, receiver) = mpsc::unbounded_channel();
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    accepted = listener.accept() => {
                        let (socket, _) = accepted.unwrap();
                        if matches!(behavior, Behavior::ImmediateClose) {
                            drop(socket);
                            continue;
                        }
                        socket.set_nodelay(true).unwrap();
                        let records = records.clone();
                        connections.spawn(async move {
                            let service = service_fn(move |request| mock_response(request, behavior, records.clone()));
                            let _ = hyper::server::conn::http1::Builder::new()
                                .serve_connection(TokioIo::new(socket), service).await;
                        });
                    }
                    Some(_) = connections.join_next(), if !connections.is_empty() => {}
                }
            }
        });
        Self {
            addr,
            records: receiver,
            task,
        }
    }

    pub async fn next(&mut self) -> Recorded {
        bounded(self.records.recv()).await.expect("mock record")
    }
}
impl Drop for Mock {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn mock_response(
    request: Request<Incoming>,
    behavior: Behavior,
    records: mpsc::UnboundedSender<Recorded>,
) -> Result<Response<MockBody>, std::io::Error> {
    let (parts, incoming) = request.into_parts();
    let body = incoming.collect().await.unwrap().to_bytes();
    let streaming = matches!(
        behavior,
        Behavior::ControlledStream | Behavior::HeadersFirstStream | Behavior::BrokenStream
    ) && serde_json::from_slice::<serde_json::Value>(&body).unwrap()["stream"]
        == true;
    let (response_body, stream) = if streaming {
        let (frames, receiver) = mpsc::channel(8);
        let (closed, closure) = oneshot::channel();
        if !matches!(behavior, Behavior::HeadersFirstStream) {
            frames.try_send(Bytes::from_static(FIRST)).unwrap();
        }
        (
            ChannelBody {
                frames: receiver,
                closed: Some(closed),
                fail_on_close: matches!(behavior, Behavior::BrokenStream),
            }
            .boxed_unsync(),
            Some(StreamControl {
                frames,
                closed: closure,
            }),
        )
    } else {
        (
            Full::new(Bytes::from_static(JSON))
                .map_err(|never| match never {})
                .boxed_unsync(),
            None,
        )
    };
    records
        .send(Recorded {
            method: parts.method,
            path: parts.uri.path_and_query().unwrap().to_string(),
            headers: parts.headers,
            body,
            stream,
        })
        .unwrap_or_else(|_| panic!("test dropped mock record receiver"));
    if matches!(behavior, Behavior::DelayedHeaders) {
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    if matches!(behavior, Behavior::Hang) {
        std::future::pending::<()>().await;
    }
    Ok(Response::builder()
        .header(
            "content-type",
            if streaming {
                "text/event-stream"
            } else {
                "application/json"
            },
        )
        .header("set-cookie", "upstream-secret=hidden")
        .header("connection", "x-response-private")
        .header("x-response-private", "hidden")
        .header("x-upstream", "kept")
        .body(response_body)
        .unwrap())
}

static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);

pub struct Gateway {
    pub addr: SocketAddr,
    pub admin_addr: SocketAddr,
    pub child: Child,
    pub client: TestClient,
    dir: PathBuf,
}
impl Gateway {
    pub async fn start(upstreams: &[SocketAddr], authenticated: bool, timeout_ms: u32) -> Self {
        Self::start_with_server(upstreams, authenticated, timeout_ms, "").await
    }

    pub async fn start_with_server(
        upstreams: &[SocketAddr],
        authenticated: bool,
        timeout_ms: u32,
        server_options: &str,
    ) -> Self {
        Self::start_with(upstreams, authenticated, timeout_ms, server_options, "").await
    }

    /// `extra` is appended as top-level YAML (e.g. `teams:` / `keys:`).
    pub async fn start_with(
        upstreams: &[SocketAddr],
        authenticated: bool,
        timeout_ms: u32,
        server_options: &str,
        extra: &str,
    ) -> Self {
        Self::start_with_type(
            upstreams,
            authenticated,
            timeout_ms,
            server_options,
            extra,
            "llm",
        )
        .await
    }

    pub async fn start_with_type(
        upstreams: &[SocketAddr],
        authenticated: bool,
        timeout_ms: u32,
        server_options: &str,
        extra: &str,
        model_type: &str,
    ) -> Self {
        // Reserve distinct ports until immediately before spawning the child.
        let listen = StdListener::bind("127.0.0.1:0").unwrap();
        let admin = StdListener::bind("127.0.0.1:0").unwrap();
        let addr = listen.local_addr().unwrap();
        let admin_addr = admin.local_addr().unwrap();
        let mut yaml = format!("server:\n  listen: '{addr}'\n");
        yaml.push_str(&format!("  admin_listen: '{admin_addr}'\n"));
        yaml.push_str(server_options);
        if authenticated {
            let hash = hex::encode(Sha256::digest(b"client-secret"));
            yaml.push_str(&format!("  api_keys: ['{hash}']\n"));
        }
        yaml.push_str("upstreams:\n");
        for (i, upstream) in upstreams.iter().enumerate() {
            yaml.push_str(&format!("  - {{name: u{i}, kind: openai_compatible, base_url: 'http://{upstream}/prefix/v1', api_key_env: LUMEWARP_INTEGRATION_UPSTREAM_KEY, timeout_ms: {timeout_ms}}}\n"));
        }
        yaml.push_str("models:\n");
        for i in 0..upstreams.len() {
            yaml.push_str(&format!(
                "  - {{type: {model_type}, name: d{i}, upstream: u{i}, upstream_model: actual}}\n"
            ));
        }
        let members = (0..upstreams.len())
            .map(|i| format!("{{model: d{i}}}"))
            .collect::<Vec<_>>()
            .join(", ");
        yaml.push_str(&format!("routers:\n  - {{name: public, members: [{members}], strategy: least_in_flight}}\n  - {{name: another-alias, members: [{members}]}}\n"));
        yaml.push_str(extra);
        let dir = std::env::temp_dir().join(format!(
            "lumewarp-proxy-test-{}-{}",
            std::process::id(),
            NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&dir).unwrap();
        let config = dir.join("config.yaml");
        std::fs::write(&config, yaml).unwrap();
        let stdout = std::fs::File::create(dir.join("stdout.log")).unwrap();
        let stderr = std::fs::File::create(dir.join("stderr.log")).unwrap();
        drop(listen);
        drop(admin);
        let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(config)
            .env("LUMEWARP_INTEGRATION_UPSTREAM_KEY", "upstream-secret")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env("RUST_LOG", "error")
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr))
            .spawn()
            .unwrap();
        let mut gateway = Self {
            addr,
            admin_addr,
            child,
            client: client(),
            dir,
        };
        let ready = tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                if let Some(status) = gateway.child.try_wait().unwrap() {
                    panic!("gateway exited {status}: {}", gateway.logs());
                }
                let request = Request::builder()
                    .uri(format!("http://{addr}/healthz"))
                    .body(Full::new(Bytes::new()))
                    .unwrap();
                if let Ok(response) = gateway.client.request(request).await {
                    if response.status() == 200 {
                        bytes(response).await;
                        break;
                    }
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await;
        assert!(
            ready.is_ok(),
            "gateway startup timed out: {}",
            gateway.logs()
        );
        gateway
    }

    pub fn uri(&self, path: &str) -> String {
        format!("http://{}{path}", self.addr)
    }

    pub async fn request(
        &self,
        method: &str,
        path: &str,
        body: impl Into<Bytes>,
        headers: &[(&str, &str)],
    ) -> Response<Incoming> {
        let mut request = Request::builder().method(method).uri(self.uri(path));
        for &(name, value) in headers {
            request = request.header(name, value);
        }
        bounded(
            self.client
                .request(request.body(Full::new(body.into())).unwrap()),
        )
        .await
        .expect("gateway response")
    }

    pub async fn metrics(&self) -> Prometheus {
        let request = Request::builder()
            .uri(format!("http://{}/metrics", self.admin_addr))
            .body(Full::new(Bytes::new()))
            .unwrap();
        let response = bounded(self.client.request(request)).await.unwrap();
        assert_eq!(response.status(), 200);
        Prometheus::parse(std::str::from_utf8(&bytes(response).await).unwrap())
    }

    fn logs(&self) -> String {
        format!(
            "{}\n{}",
            std::fs::read_to_string(self.dir.join("stdout.log")).unwrap_or_default(),
            std::fs::read_to_string(self.dir.join("stderr.log")).unwrap_or_default()
        )
    }
}
impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[derive(Debug)]
pub struct Sample {
    pub name: String,
    pub labels: BTreeMap<String, String>,
    pub value: f64,
}

pub struct Prometheus {
    pub samples: Vec<Sample>,
}
impl Prometheus {
    pub fn parse(text: &str) -> Self {
        let mut samples = Vec::new();
        for line in text
            .lines()
            .filter(|line| !line.is_empty() && !line.starts_with('#'))
        {
            // Find the value separator without splitting quoted label values.
            let (mut quoted, mut escaped) = (false, false);
            let end = line
                .char_indices()
                .find_map(|(index, c)| {
                    if escaped {
                        escaped = false;
                    } else if quoted && c == '\\' {
                        escaped = true;
                    } else if c == '"' {
                        quoted = !quoted;
                    } else if c.is_whitespace() && !quoted {
                        return Some(index);
                    }
                    None
                })
                .expect("metric value separator");
            let series = &line[..end];
            let value = line[end..]
                .split_whitespace()
                .next()
                .unwrap()
                .parse()
                .expect("numeric metric value");
            let (name, labels) = if let Some((name, labels)) = series.split_once('{') {
                let labels = labels.strip_suffix('}').expect("closed metric labels");
                let (mut quoted, mut escaped) = (false, false);
                let mut start = 0;
                let mut parsed = BTreeMap::new();
                for (index, c) in labels
                    .char_indices()
                    .chain(std::iter::once((labels.len(), ',')))
                {
                    if escaped {
                        escaped = false;
                    } else if quoted && c == '\\' {
                        escaped = true;
                    } else if c == '"' {
                        quoted = !quoted;
                    } else if c == ',' && !quoted {
                        let pair = &labels[start..index];
                        if !pair.is_empty() {
                            let (key, value) = pair.split_once('=').expect("label assignment");
                            parsed.insert(
                                key.to_owned(),
                                serde_json::from_str::<String>(value).expect("quoted label"),
                            );
                        }
                        start = index + 1;
                    }
                }
                (name, parsed)
            } else {
                (series, BTreeMap::new())
            };
            samples.push(Sample {
                name: name.into(),
                labels,
                value,
            });
        }
        Self { samples }
    }

    pub fn sum(&self, name: &str, labels: &[(&str, &str)]) -> f64 {
        self.samples
            .iter()
            .filter(|sample| {
                sample.name == name
                    && labels.iter().all(|&(key, value)| {
                        sample.labels.get(key).is_some_and(|actual| actual == value)
                    })
            })
            .map(|sample| sample.value)
            .sum()
    }

    pub fn value(&self, name: &str, labels: &[(&str, &str)]) -> f64 {
        let matches: Vec<_> = self
            .samples
            .iter()
            .filter(|sample| {
                sample.name == name
                    && labels.iter().all(|&(key, value)| {
                        sample.labels.get(key).is_some_and(|actual| actual == value)
                    })
            })
            .collect();
        assert_eq!(
            matches.len(),
            1,
            "expected one series for {name} {labels:?}"
        );
        matches[0].value
    }
}

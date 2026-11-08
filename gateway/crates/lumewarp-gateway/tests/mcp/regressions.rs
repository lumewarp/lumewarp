//! Review regressions with controllable, real HTTP upstreams.
use super::{bytes, Gw};
use bytes::Bytes;
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full, StreamBody};
use hyper::{
    body::{Frame, Incoming},
    service::service_fn,
    Method, Request, Response, StatusCode,
};
use hyper_util::rt::TokioIo;
use serde_json::{json, Value};
use std::{
    net::SocketAddr,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio_stream::wrappers::ReceiverStream;

#[derive(Clone, Copy)]
#[repr(usize)]
enum Mode {
    Healthy,
    Disconnect,
    Invalid,
    RpcError,
    MissingArray,
    SlowHeaders,
    NotFound,
    Drip,
    Cr,
    CrLf,
    Mixed,
    BadCursor,
}
type Body = UnsyncBoxBody<Bytes, std::io::Error>;
struct Upstream {
    addr: SocketAddr,
    mode: Arc<AtomicUsize>,
    initialized: Arc<AtomicUsize>,
    deleted: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}
fn body(data: impl Into<Bytes>) -> Body {
    Full::new(data.into())
        .map_err(|never| match never {})
        .boxed_unsync()
}
impl Upstream {
    async fn start(mode: Mode) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let state = Arc::new(AtomicUsize::new(mode as usize));
        let initialized = Arc::new(AtomicUsize::new(0));
        let deleted = Arc::new(AtomicUsize::new(0));
        let (m, i, d) = (state.clone(), initialized.clone(), deleted.clone());
        let task = tokio::spawn(async move {
            loop {
                let (socket, _) = listener.accept().await.unwrap();
                let (m, i, d) = (m.clone(), i.clone(), d.clone());
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let (m, i, d) = (m.clone(), i.clone(), d.clone());
                        async move {
                            let verb = request.method().clone();
                            let data = request.into_body().collect().await.unwrap().to_bytes();
                            let v: Value = serde_json::from_slice(&data).unwrap_or(Value::Null);
                            let method = v["method"].as_str().unwrap_or("");
                            let mode = m.load(Ordering::SeqCst);
                            if method == "initialize" {
                                return Ok(Response::new(body(json!({"jsonrpc":"2.0","id":v["id"],"result":{"protocolVersion":"2025-11-25","capabilities":{},"serverInfo":{"name":"review-fixture","version":"1"}}}).to_string())));
                            }
                            if method == "notifications/initialized" {
                                i.fetch_add(1, Ordering::SeqCst);
                            }
                            if verb == Method::DELETE {
                                d.fetch_add(1, Ordering::SeqCst);
                            }
                            if mode == Mode::NotFound as usize {
                                return Ok(Response::builder()
                                    .status(StatusCode::NOT_FOUND)
                                    .body(body(Bytes::new()))
                                    .unwrap());
                            }
                            if method.starts_with("notifications/") || verb == Method::DELETE {
                                return Ok(Response::builder()
                                    .status(if verb == Method::DELETE {
                                        StatusCode::NO_CONTENT
                                    } else {
                                        StatusCode::ACCEPTED
                                    })
                                    .body(body(Bytes::new()))
                                    .unwrap());
                            }
                            if mode == Mode::Disconnect as usize {
                                return Err(std::io::Error::other("fixture connection failure"));
                            }
                            if mode == Mode::SlowHeaders as usize {
                                tokio::time::sleep(Duration::from_secs(3)).await;
                            }
                            if mode == Mode::Drip as usize {
                                let (tx, rx) = tokio::sync::mpsc::channel(2);
                                tokio::spawn(async move {
                                    let _ = tx
                                        .send(Ok::<_, std::io::Error>(Frame::data(
                                            Bytes::from_static(b"{\"jsonrpc\":"),
                                        )))
                                        .await;
                                    tokio::time::sleep(Duration::from_secs(3)).await;
                                    let _ = tx
                                        .send(Ok(Frame::data(Bytes::from_static(b"\"2.0\"}"))))
                                        .await;
                                });
                                return Ok(Response::builder()
                                    .header("content-type", "application/json")
                                    .body(StreamBody::new(ReceiverStream::new(rx)).boxed_unsync())
                                    .unwrap());
                            }
                            let field = if method == "prompts/list" {
                                "prompts"
                            } else {
                                "tools"
                            };
                            let mut result = json!({"jsonrpc":"2.0","id":v["id"],"result":{field:[{"name":"echo","inputSchema":{"type":"object"}}]}});
                            if mode == Mode::RpcError as usize {
                                result = json!({"jsonrpc":"2.0","id":v["id"],"error":{"code":-32603,"message":"untrusted error must not become a metric label"}});
                            }
                            if mode == Mode::MissingArray as usize {
                                result["result"] = json!({});
                            }
                            if mode == Mode::BadCursor as usize {
                                result["result"]["nextCursor"] = 17.into();
                            }
                            let (content_type, data) = if [
                                Mode::Cr as usize,
                                Mode::CrLf as usize,
                                Mode::Mixed as usize,
                            ]
                            .contains(&mode)
                            {
                                let ending = if mode == Mode::Cr as usize {
                                    "\r\r"
                                } else if mode == Mode::CrLf as usize {
                                    "\r\n\r\n"
                                } else {
                                    "\n\r\n"
                                };
                                let reverse = if method == "tools/call" {
                                    format!(
                                        "data: {}{ending}",
                                        json!({"jsonrpc":"2.0","id":17,"method":"elicitation/create","params":{}})
                                    )
                                } else {
                                    String::new()
                                };
                                (
                                    "text/event-stream",
                                    format!("{reverse}data: {result}{ending}"),
                                )
                            } else {
                                (
                                    "application/json",
                                    if mode == Mode::Invalid as usize {
                                        "not JSON".into()
                                    } else {
                                        result.to_string()
                                    },
                                )
                            };
                            Ok(Response::builder()
                                .header("content-type", content_type)
                                .body(body(data))
                                .unwrap())
                        }
                    });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });
        Self {
            addr,
            mode: state,
            initialized,
            deleted,
            task,
        }
    }
    fn set(&self, mode: Mode) {
        self.mode.store(mode as usize, Ordering::SeqCst);
    }
}

#[tokio::test]
async fn aggregate_skips_failed_discovery_legs_and_advances_cursor() {
    for mode in [
        Mode::Disconnect,
        Mode::Invalid,
        Mode::RpcError,
        Mode::MissingArray,
        Mode::SlowHeaders,
        Mode::BadCursor,
    ] {
        let broken = Upstream::start(mode).await;
        let good = Upstream::start(Mode::Healthy).await;
        let last = Upstream::start(Mode::Healthy).await;
        let gw = Gw::start(
            &[("aaa", broken.addr), ("bbb", good.addr), ("ccc", last.addr)],
            0,
        )
        .await;
        let session = gw.initialize("/mcp", "secret").await;
        let (status, page) = gw
            .rpc(
                "/mcp",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
            )
            .await;
        assert_eq!(status, 200, "{page}");
        assert_eq!(page["result"]["tools"][0]["name"], "bbb__echo");
        let (status,next) = gw.rpc("/mcp",Some(&session),"secret",json!({"jsonrpc":"2.0","id":3,"method":"tools/list","params":{"cursor":page["result"]["nextCursor"]}})).await;
        assert_eq!(status, 200, "{next}");
        assert_eq!(next["result"]["tools"][0]["name"], "ccc__echo");
        assert!(next["result"].get("nextCursor").is_none());
        let metrics = gw.metrics().await;
        let status = if matches!(mode, Mode::SlowHeaders) {
            "504"
        } else {
            "502"
        };
        assert!(
            metrics
                .lines()
                .any(|line| line.starts_with("lumewarp_mcp_requests_total{")
                    && line.contains("server=\"aaa\"")
                    && line.contains(&format!("status=\"{status}\""))
                    && line.ends_with(" 1")),
            "{metrics}"
        );
        good.set(Mode::Invalid);
        last.set(Mode::Invalid);
        assert_eq!(
            gw.rpc(
                "/mcp",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":4,"method":"prompts/list"})
            )
            .await
            .0,
            502
        );
        broken.set(Mode::SlowHeaders);
        good.set(Mode::SlowHeaders);
        last.set(Mode::SlowHeaders);
        assert_eq!(
            gw.rpc(
                "/mcp",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":5,"method":"tools/list"})
            )
            .await
            .0,
            504
        );
        broken.set(Mode::NotFound);
        assert_eq!(
            gw.rpc(
                "/mcp",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":6,"method":"tools/list"})
            )
            .await
            .0,
            404
        );
    }
}

#[tokio::test]
async fn direct_non_streaming_body_deadline_is_enforced() {
    let upstream = Upstream::start(Mode::Drip).await;
    let gw = Gw::start(&[("s1", upstream.addr)], 0).await;
    let session = gw.initialize("/mcp/servers/s1", "secret").await;
    let start = Instant::now();
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo"}})
        )
        .await
        .0,
        504
    );
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn fanout_finishes_all_legs_even_after_a_404() {
    let first = Upstream::start(Mode::NotFound).await;
    let second = Upstream::start(Mode::Healthy).await;
    let gw = Gw::start(&[("aaa", first.addr), ("zzz", second.addr)], 0).await;
    // Unlike initialize(), deliberately inspect the initialized notification's failure.
    let response = gw
        .raw(
            "/mcp",
            "POST",
            None,
            "secret",
            Bytes::from(super::initialize_request().to_string()),
        )
        .await;
    let session = response.headers()["mcp-session-id"]
        .to_str()
        .unwrap()
        .to_owned();
    let _ = bytes(response).await;
    assert_eq!(
        gw.rpc(
            "/mcp",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","method":"notifications/initialized"})
        )
        .await
        .0,
        404
    );
    assert_eq!(second.initialized.load(Ordering::SeqCst), 1);
    assert_eq!(
        gw.raw("/mcp", "DELETE", Some(&session), "secret", Bytes::new())
            .await
            .status(),
        404
    );
    assert_eq!(first.deleted.load(Ordering::SeqCst), 1);
    assert_eq!(second.deleted.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn sse_cr_crlf_and_mixed_terminators_on_discovery_and_streams() {
    for mode in [Mode::Cr, Mode::CrLf, Mode::Mixed] {
        let upstream = Upstream::start(mode).await;
        let gw = Gw::start(&[("s1", upstream.addr)], 0).await;
        let session = gw.initialize("/mcp", "secret").await;
        let (status, v) = gw
            .rpc(
                "/mcp",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
            )
            .await;
        assert_eq!(status, 200, "{v}");
        assert_eq!(v["result"]["tools"][0]["name"], "s1__echo");
        let response = gw.raw("/mcp","POST",Some(&session),"secret",Bytes::from(json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"s1__echo"}}).to_string())).await;
        let text = String::from_utf8(bytes(response).await.to_vec()).unwrap();
        assert!(text.contains("lw:s1:17"), "{text}");
        assert_eq!(text.matches("data: ").count(), 2, "{text}");
        let direct = gw.initialize("/mcp/servers/s1", "secret").await;
        let response = gw
            .raw(
                "/mcp/servers/s1",
                "POST",
                Some(&direct),
                "secret",
                Bytes::from(
                    json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo"}})
                        .to_string(),
                ),
            )
            .await;
        let text = String::from_utf8(bytes(response).await.to_vec()).unwrap();
        let ending = match mode {
            Mode::Cr => "\r\r",
            Mode::CrLf => "\r\n\r\n",
            _ => "\n\r\n",
        };
        assert_eq!(text.matches(ending).count(), 2, "{text:?}");
        assert!(!text.contains("lw:s1:"));
    }
}

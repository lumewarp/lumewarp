//! Fixed-latency OpenAI-compatible benchmark upstream; not for production use.
//! Example MCP server and A2A agent live in the `mcp` and `a2a` modules; the
//! `mcp-a2a-mock` binary is their CLI. The benchmark upstream below is not
//! affected by them.

pub mod a2a;
pub mod auth;
pub mod mcp;
pub mod server;
pub mod util;

use bytes::Bytes;
use clap::{Parser, ValueEnum};
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full, StreamBody};
use hyper::{body::Frame, service::service_fn, Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use serde::{Deserialize, Serialize};
use serde_json::{json, value::RawValue, Value};
use std::{
    borrow::Cow,
    convert::Infallible,
    net::SocketAddr,
    sync::{
        atomic::{AtomicU16, AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::net::TcpListener;

type Body = UnsyncBoxBody<Bytes, Infallible>;

#[derive(Clone, Debug, Parser)]
pub struct Options {
    #[arg(long, default_value = "127.0.0.1:9000")]
    pub listen: SocketAddr,
    #[arg(long, default_value_t = 50)]
    pub latency_ms: u64,
    /// Send SSE headers immediately, then simulate prefill before the first event.
    #[arg(long)]
    pub headers_first: bool,
    #[arg(long, default_value_t = 20)]
    pub stream_chunks: u32,
    #[arg(long, default_value_t = 10)]
    pub chunk_interval_ms: u64,
    #[arg(long)]
    pub echo: bool,
    #[arg(long, default_value_t = default_workers(), value_parser = parse_workers)]
    pub workers: usize,
    /// Fail every Nth inference request; zero leaves benchmark behavior unchanged.
    #[arg(long, default_value_t = 0)]
    pub fail_every: u64,
    #[arg(long, default_value_t = 503)]
    pub fail_status: u16,
    #[arg(long, value_enum, default_value_t = FailMode::Status)]
    pub fail_mode: FailMode,
    /// `model_name` label of the vLLM-style series served on `/metrics`.
    #[arg(long, default_value = "mock")]
    pub served_model_name: String,
    /// Initial `vllm:num_requests_waiting`; change at runtime with `POST /load`.
    #[arg(long, default_value_t = 0.0)]
    pub waiting: f64,
    /// Initial `vllm:kv_cache_usage_perc` (0..1).
    #[arg(long, default_value_t = 0.0)]
    pub kv_cache_usage: f64,
    /// Answer embeddings for model `mock` with a shared body of at least this
    /// many bytes, `model` after `data`; zero keeps the small response.
    #[arg(long, default_value_t = 0)]
    pub large_embedding_bytes: usize,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
pub enum FailMode {
    Status,
    Reset,
    Timeout,
}

fn parse_workers(value: &str) -> Result<usize, String> {
    value
        .parse::<usize>()
        .ok()
        .filter(|&n| n > 0)
        .ok_or_else(|| "workers must be a positive integer".into())
}

fn default_workers() -> usize {
    std::thread::available_parallelism().map_or(1, usize::from)
}

#[derive(Default, Deserialize)]
struct Fields<'a> {
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
    #[serde(default)]
    stream: bool,
    #[serde(default)]
    stream_options: StreamOptions,
    #[serde(borrow)]
    input: Option<&'a RawValue>,
}

#[derive(Default, Deserialize)]
struct StreamOptions {
    #[serde(default)]
    include_usage: bool,
}

struct Payloads {
    full: Bytes,
    content: Bytes,
    finish: Bytes,
    usage: Bytes,
}

fn usage(chunks: u32) -> Value {
    json!({"prompt_tokens": 8, "completion_tokens": chunks, "total_tokens": 8_u64 + u64::from(chunks)})
}

fn event(value: Value) -> Bytes {
    Bytes::from(format!("data: {value}\n\n"))
}

impl Payloads {
    fn new(model: &str, chat: bool, chunks: u32) -> Self {
        let object = if chat {
            "chat.completion"
        } else {
            "text_completion"
        };
        let base = json!({"id":"mock-001", "object":object, "created":1700000000, "model":model,
            "choices":[], "usage":usage(chunks)});
        let mut full = base.clone();
        full["choices"] = if chat {
            json!([{"index":0,"message":{"role":"assistant","content":"Hello from mock."},"finish_reason":"stop"}])
        } else {
            json!([{"index":0,"text":"Hello from mock.","logprobs":null,"finish_reason":"stop"}])
        };
        let mut chunk = base;
        if chat {
            chunk["object"] = json!("chat.completion.chunk");
        }
        let usage = event(chunk.clone());
        chunk["usage"] = Value::Null;
        chunk["choices"] = if chat {
            json!([{"index":0,"delta":{"content":"hello "},"finish_reason":null}])
        } else {
            json!([{"index":0,"text":"hello ","logprobs":null,"finish_reason":null}])
        };
        let content = event(chunk.clone());
        chunk["choices"] = if chat {
            json!([{"index":0,"delta":{},"finish_reason":"stop"}])
        } else {
            json!([{"index":0,"text":"","logprobs":null,"finish_reason":"stop"}])
        };
        Self {
            full: Bytes::from(full.to_string()),
            content,
            finish: event(chunk),
            usage,
        }
    }
}

/// A list of 1536-dimension vectors of at least `size` bytes, shaped like
/// OpenAI's: `model` follows `data`, so a scanner reads the whole body to find it.
fn large_embedding(size: usize) -> Bytes {
    let vector = format!("[{}0.125]", "0.125,".repeat(1535));
    let mut body = String::with_capacity(size + vector.len() + 256);
    body.push_str(r#"{"object":"list","data":["#);
    let mut index = 0;
    while body.len() < size {
        if index > 0 {
            body.push(',');
        }
        body.push_str(&format!(
            r#"{{"object":"embedding","index":{index},"embedding":{vector}}}"#
        ));
        index += 1;
    }
    body.push_str(r#"],"model":"mock","usage":{"prompt_tokens":8,"total_tokens":8}}"#);
    Bytes::from(body)
}

pub struct App {
    options: Options,
    chat: Arc<Payloads>,
    completions: Arc<Payloads>,
    large_embedding: Option<Bytes>,
    models: Bytes,
    fault: AtomicU16,
    requests: AtomicU64,
    /// f64 bits of the reported queue depth and KV-cache usage.
    waiting: AtomicU64,
    kv: AtomicU64,
    /// Non-zero: `/metrics` answers with this status instead.
    metrics_status: AtomicU16,
    metrics_scrapes: AtomicU64,
}

impl App {
    pub fn new(options: Options) -> Arc<Self> {
        Arc::new(Self {
            chat: Arc::new(Payloads::new("mock", true, options.stream_chunks)),
            completions: Arc::new(Payloads::new("mock", false, options.stream_chunks)),
            large_embedding: (options.large_embedding_bytes > 0)
                .then(|| large_embedding(options.large_embedding_bytes)),
            models: Bytes::from(json!({"object":"list","data":[{"id":"mock","object":"model","created":1700000000,"owned_by":"mock"}]}).to_string()),
            fault: AtomicU16::new(0),
            requests: AtomicU64::new(0),
            waiting: AtomicU64::new(options.waiting.to_bits()),
            kv: AtomicU64::new(options.kv_cache_usage.to_bits()),
            metrics_status: AtomicU16::new(0),
            metrics_scrapes: AtomicU64::new(0),
            options,
        })
    }

    async fn respond(
        &self,
        request: Request<hyper::body::Incoming>,
    ) -> Result<Response<Body>, std::io::Error> {
        if request.method() == Method::POST && request.uri().path() == "/fault" {
            let status = request
                .uri()
                .query()
                .and_then(|q| q.strip_prefix("status="))
                .and_then(|s| s.parse::<u16>().ok());
            return Ok(match status {
                Some(status) if status == 0 || (400..=599).contains(&status) => {
                    self.fault.store(status, Ordering::Relaxed);
                    response(
                        StatusCode::OK,
                        "application/json",
                        Bytes::from_static(b"{\"ok\":true}"),
                    )
                }
                _ => error(
                    StatusCode::BAD_REQUEST,
                    "Expected status=0 or status=400..599",
                ),
            });
        }
        if request.uri().path() == "/load" && request.method() == Method::POST {
            return Ok(self.set_load(request.uri().query().unwrap_or("")));
        }
        if request.uri().path() == "/metrics" && request.method() == Method::GET {
            return Ok(self.metrics());
        }
        let failure = request
            .headers()
            .get("x-mock-fail")
            .and_then(|h| h.to_str().ok());
        match failure {
            Some("reset") => return Err(std::io::Error::other("injected connection reset")),
            Some("timeout") => return std::future::pending().await,
            _ => {}
        }
        let periodic = self.options.fail_every > 0
            && request.method() == Method::POST
            && (self.requests.fetch_add(1, Ordering::Relaxed) + 1)
                .is_multiple_of(self.options.fail_every);
        if periodic {
            match self.options.fail_mode {
                FailMode::Reset => return Err(std::io::Error::other("injected connection reset")),
                FailMode::Timeout => return std::future::pending().await,
                FailMode::Status => {}
            }
        }
        let status = failure
            .and_then(|s| s.parse::<u16>().ok())
            .unwrap_or_else(|| {
                if periodic {
                    self.options.fail_status
                } else {
                    self.fault.load(Ordering::Relaxed)
                }
            });
        if (400..=599).contains(&status) {
            let mut response = error(
                StatusCode::from_u16(status).expect("validated status"),
                "Injected upstream failure",
            );
            if let Some(value) = request.headers().get("x-mock-retry-after") {
                response
                    .headers_mut()
                    .insert(hyper::header::RETRY_AFTER, value.clone());
            }
            return Ok(response);
        }
        Ok(self.handle(request).await)
    }

    async fn handle(&self, request: Request<hyper::body::Incoming>) -> Response<Body> {
        let path = request.uri().path();
        if request.method() == Method::GET && path == "/health" {
            return response(
                StatusCode::OK,
                "application/json",
                Bytes::from_static(b"{\"status\":\"ok\"}"),
            );
        }
        if request.method() == Method::GET && path == "/v1/models" {
            self.latency().await;
            return response(StatusCode::OK, "application/json", self.models.clone());
        }
        let chat = path == "/v1/chat/completions";
        let embeddings = path == "/v1/embeddings";
        if request.method() != Method::POST || !(chat || embeddings || path == "/v1/completions") {
            return error(StatusCode::NOT_FOUND, "Unknown endpoint");
        }
        let (parts, body) = request.into_parts();
        let Ok(body) = body.collect().await else {
            return error(StatusCode::BAD_REQUEST, "Cannot read body");
        };
        let body = body.to_bytes();
        // Validate the entire JSON (including UTF-8 in otherwise ignored fields)
        // before extracting fields or reflecting the raw body in echo mode.
        let Ok(raw) = serde_json::from_slice::<&RawValue>(&body) else {
            return error(StatusCode::BAD_REQUEST, "Invalid JSON request");
        };
        let Ok(fields) = serde_json::from_str::<Fields<'_>>(raw.get()) else {
            return error(StatusCode::BAD_REQUEST, "Invalid JSON request");
        };
        let model = fields.model.as_deref().unwrap_or("mock");
        let echo = if self.options.echo {
            let headers: serde_json::Map<String, Value> = parts
                .headers
                .iter()
                .map(|(name, value)| {
                    (
                        name.to_string(),
                        Value::String(String::from_utf8_lossy(value.as_bytes()).into_owned()),
                    )
                })
                .collect();
            // RawValue preserves the received JSON, including unknown fields and number spelling.
            #[derive(Serialize)]
            struct Echo<'a> {
                method: &'a str,
                path: &'a str,
                headers: serde_json::Map<String, Value>,
                body: &'a RawValue,
            }
            let echo = Echo {
                method: parts.method.as_str(),
                path: parts.uri.path(),
                headers,
                body: raw,
            };
            Some(Bytes::from(
                serde_json::to_vec(&echo).expect("serialize echo"),
            ))
        } else {
            None
        };
        let headers_first = self.options.headers_first && fields.stream && !embeddings;
        if !headers_first {
            self.latency().await;
        }
        if let Some(ref echo) = echo {
            if !fields.stream || embeddings {
                return response(StatusCode::OK, "application/json", echo.clone());
            }
        }
        if embeddings {
            let Some(input) = fields.input else {
                return error(StatusCode::BAD_REQUEST, "Missing input");
            };
            let Ok(input) = serde_json::from_str::<Value>(input.get()) else {
                return error(StatusCode::BAD_REQUEST, "Invalid input");
            };
            let count = match input {
                Value::String(_) => 1,
                Value::Array(ref values) => values.len(),
                _ => return error(StatusCode::BAD_REQUEST, "input must be a string or array"),
            };
            if let Some(large) = self.large_embedding.as_ref().filter(|_| model == "mock") {
                return response(StatusCode::OK, "application/json", large.clone());
            }
            let data: Vec<_> = (0..count)
                .map(
                    |index| json!({"object":"embedding","index":index,"embedding":vec![0.125; 16]}),
                )
                .collect();
            return response(StatusCode::OK, "application/json", Bytes::from(json!({"object":"list","model":model,"data":data,"usage":{"prompt_tokens":8,"total_tokens":8}}).to_string()));
        }
        let payload = if model == "mock" {
            if chat {
                self.chat.clone()
            } else {
                self.completions.clone()
            }
        } else {
            Arc::new(Payloads::new(model, chat, self.options.stream_chunks))
        };
        if !fields.stream {
            return response(StatusCode::OK, "application/json", payload.full.clone());
        }
        let chunks = self.options.stream_chunks;
        let interval = Duration::from_millis(self.options.chunk_interval_ms);
        let include_usage = fields.stream_options.include_usage;
        let prefill = Duration::from_millis(if headers_first {
            self.options.latency_ms
        } else {
            0
        });
        let stream = async_stream::stream! {
            if !prefill.is_zero() {
                tokio::time::sleep(prefill).await;
            }
            if let Some(echo) = echo {
                let mut frame = Vec::with_capacity(echo.len() + 8);
                frame.extend_from_slice(b"data: ");
                frame.extend_from_slice(&echo);
                frame.extend_from_slice(b"\n\n");
                yield Ok::<_, Infallible>(Frame::data(Bytes::from(frame)));
            }
            for index in 0..chunks {
                if index > 0 && !interval.is_zero() { tokio::time::sleep(interval).await; }
                yield Ok(Frame::data(payload.content.clone()));
            }
            yield Ok(Frame::data(payload.finish.clone()));
            if include_usage { yield Ok(Frame::data(payload.usage.clone())); }
            yield Ok(Frame::data(Bytes::from_static(b"data: [DONE]\n\n")));
        };
        Response::builder()
            .header("content-type", "text/event-stream")
            .header("cache-control", "no-cache")
            .header("x-accel-buffering", "no")
            .body(StreamBody::new(stream).boxed_unsync())
            .expect("static headers")
    }

    /// `POST /load?waiting=<n>&kv=<0..1>&metrics_status=<0|400..599>`: any
    /// subset; values persist until changed.
    fn set_load(&self, query: &str) -> Response<Body> {
        let mut updates = Vec::new();
        for pair in query.split('&').filter(|p| !p.is_empty()) {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            let ok = match key {
                "waiting" => value
                    .parse::<f64>()
                    .ok()
                    .filter(|v| v.is_finite() && *v >= 0.0)
                    .map(|v| updates.push((&self.waiting, v.to_bits()))),
                "kv" => value
                    .parse::<f64>()
                    .ok()
                    .filter(|v| (0.0..=1.0).contains(v))
                    .map(|v| updates.push((&self.kv, v.to_bits()))),
                "metrics_status" => value
                    .parse::<u16>()
                    .ok()
                    .filter(|&s| s == 0 || (400..=599).contains(&s))
                    .map(|s| self.metrics_status.store(s, Ordering::Relaxed)),
                _ => None,
            };
            if ok.is_none() {
                return error(
                    StatusCode::BAD_REQUEST,
                    "Expected waiting>=0, kv in 0..1, metrics_status=0|400..599",
                );
            }
        }
        for (target, bits) in updates {
            target.store(bits, Ordering::Relaxed);
        }
        response(
            StatusCode::OK,
            "application/json",
            Bytes::from(
                json!({"waiting": f64::from_bits(self.waiting.load(Ordering::Relaxed)),
                    "kv": f64::from_bits(self.kv.load(Ordering::Relaxed)),
                    "scrapes": self.metrics_scrapes.load(Ordering::Relaxed)})
                .to_string(),
            ),
        )
    }

    /// vLLM-style Prometheus text with the values set through `/load`.
    fn metrics(&self) -> Response<Body> {
        self.metrics_scrapes.fetch_add(1, Ordering::Relaxed);
        let status = self.metrics_status.load(Ordering::Relaxed);
        if status != 0 {
            return error(
                StatusCode::from_u16(status).expect("validated status"),
                "Injected metrics failure",
            );
        }
        let model = self
            .options
            .served_model_name
            .replace('\\', "\\\\")
            .replace('"', "\\\"");
        let waiting = f64::from_bits(self.waiting.load(Ordering::Relaxed));
        let kv = f64::from_bits(self.kv.load(Ordering::Relaxed));
        let labels = format!("engine=\"0\",model_name=\"{model}\"");
        response(
            StatusCode::OK,
            "text/plain; version=0.0.4; charset=utf-8",
            Bytes::from(format!(
                "# HELP vllm:num_requests_waiting Number of requests waiting to be processed.\n\
                 # TYPE vllm:num_requests_waiting gauge\n\
                 vllm:num_requests_waiting{{{labels}}} {waiting:?}\n\
                 # HELP vllm:num_requests_running Number of requests in model execution batches.\n\
                 # TYPE vllm:num_requests_running gauge\n\
                 vllm:num_requests_running{{{labels}}} 0.0\n\
                 # HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.\n\
                 # TYPE vllm:kv_cache_usage_perc gauge\n\
                 vllm:kv_cache_usage_perc{{{labels}}} {kv:?}\n"
            )),
        )
    }

    async fn latency(&self) {
        if self.options.latency_ms > 0 {
            tokio::time::sleep(Duration::from_millis(self.options.latency_ms)).await;
        }
    }
}

fn response(status: StatusCode, content_type: &'static str, data: Bytes) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", content_type)
        .body(Full::new(data).boxed_unsync())
        .expect("static headers")
}

fn error(status: StatusCode, message: &str) -> Response<Body> {
    response(status, "application/json", Bytes::from(json!({"error":{"message":message,"type":"invalid_request_error","param":null,"code":status.as_u16()}}).to_string()))
}

/// Serve HTTP/1 keep-alive connections. Dropping the enclosing task stops accepting.
pub async fn serve(listener: TcpListener, app: Arc<App>) -> std::io::Result<()> {
    loop {
        let (socket, _) = match listener.accept().await {
            Ok(connection) => connection,
            Err(error) => {
                eprintln!("mock-upstream accept error: {error}");
                tokio::time::sleep(Duration::from_millis(1)).await;
                continue;
            }
        };
        let _ = socket.set_nodelay(true);
        let app = app.clone();
        tokio::spawn(async move {
            let service = service_fn(move |request| {
                let app = app.clone();
                async move { app.respond(request).await }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .keep_alive(true)
                .serve_connection(TokioIo::new(socket), service)
                .await;
        });
    }
}

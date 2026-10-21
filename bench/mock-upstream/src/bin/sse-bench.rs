use clap::{Parser, ValueEnum};
use futures_util::StreamExt;
use hdrhistogram::Histogram;
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

#[derive(Clone, Copy, ValueEnum)]
enum Endpoint {
    Chat,
    Completions,
}

#[derive(Parser)]
struct Options {
    /// Full endpoint URL, or base URL ending in /v1 (or server root).
    #[arg(long)]
    url: String,
    #[arg(long, default_value = "mock")]
    model: String,
    #[arg(long)]
    api_key: Option<String>,
    #[arg(long, default_value_t = 1, value_parser = clap::value_parser!(u32).range(1..))]
    concurrency: u32,
    #[arg(long, conflicts_with = "requests", required_unless_present = "requests", value_parser = clap::value_parser!(u64).range(1..))]
    duration_secs: Option<u64>,
    #[arg(long, conflicts_with = "duration_secs", required_unless_present = "duration_secs", value_parser = clap::value_parser!(u64).range(1..))]
    requests: Option<u64>,
    #[arg(long, value_enum, default_value = "chat")]
    endpoint: Endpoint,
    #[arg(long)]
    out: Option<PathBuf>,
}

struct Stats {
    requests: u64,
    errors: u64,
    ttft: Histogram<u64>,
    total: Histogram<u64>,
}

impl Stats {
    fn new() -> Self {
        Self {
            requests: 0,
            errors: 0,
            ttft: Histogram::new(3).unwrap(),
            total: Histogram::new(3).unwrap(),
        }
    }
}

/// Incremental SSE decoder: HTTP frames need not align with lines or events.
#[derive(Default)]
struct Decoder {
    pending: Vec<u8>,
    data: String,
}
impl Decoder {
    fn feed(&mut self, bytes: &[u8]) -> Result<Vec<String>, String> {
        self.pending.extend_from_slice(bytes);
        let mut events = Vec::new();
        let mut consumed = 0;
        while let Some(offset) = self.pending[consumed..]
            .iter()
            .position(|&byte| byte == b'\n')
        {
            let end = consumed + offset;
            let line = std::str::from_utf8(&self.pending[consumed..end])
                .map_err(|e| e.to_string())?
                .trim_end_matches('\r');
            consumed = end + 1;
            if line.is_empty() {
                if !self.data.is_empty() {
                    events.push(std::mem::take(&mut self.data));
                }
            } else if let Some(data) = line.strip_prefix("data:") {
                if !self.data.is_empty() {
                    self.data.push('\n');
                }
                self.data.push_str(data.strip_prefix(' ').unwrap_or(data));
            }
        }
        self.pending.drain(..consumed);
        if self.pending.len() + self.data.len() > 4 * 1024 * 1024 {
            return Err("SSE event exceeds 4 MiB".into());
        }
        Ok(events)
    }
}

async fn request(
    client: &reqwest::Client,
    url: &str,
    body: &Value,
    key: Option<&str>,
) -> Result<(u64, u64), String> {
    let start = Instant::now();
    let mut builder = client.post(url).json(body);
    if let Some(key) = key {
        builder = builder.bearer_auth(key);
    }
    let response = builder.send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("HTTP {}", response.status()));
    }
    if !response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("text/event-stream"))
    {
        return Err("response is not text/event-stream".into());
    }
    let mut stream = response.bytes_stream();
    let mut decoder = Decoder::default();
    let mut ttft = None;
    let mut done = None;
    while let Some(frame) = stream.next().await {
        let frame = frame.map_err(|e| e.to_string())?;
        for data in decoder.feed(&frame)? {
            if done.is_some() {
                return Err("event after [DONE]".into());
            }
            if data == "[DONE]" {
                done = Some(micros(start.elapsed()));
                continue;
            }
            let value: Value = serde_json::from_str(&data).map_err(|e| e.to_string())?;
            let choices = value
                .get("choices")
                .and_then(Value::as_array)
                .ok_or("missing choices")?;
            if ttft.is_none()
                && choices.iter().any(|choice| {
                    choice
                        .pointer("/delta/content")
                        .or_else(|| choice.get("text"))
                        .and_then(Value::as_str)
                        .is_some_and(|text| !text.is_empty())
                })
            {
                ttft = Some(micros(start.elapsed()));
            }
        }
    }
    // Consume the HTTP body through EOF so the connection is returned to the pool.
    match (ttft, done) {
        (Some(ttft), Some(total)) => Ok((ttft, total)),
        _ => Err("missing content or [DONE]".into()),
    }
}

fn micros(duration: Duration) -> u64 {
    duration.as_micros().min(u128::from(u64::MAX)) as u64
}

fn percentiles(histogram: &Histogram<u64>) -> Value {
    if histogram.is_empty() {
        return json!({"p50":0.0,"p90":0.0,"p99":0.0,"max":0.0});
    }
    json!({"p50":histogram.value_at_quantile(0.50) as f64 / 1000.0,
        "p90":histogram.value_at_quantile(0.90) as f64 / 1000.0,
        "p99":histogram.value_at_quantile(0.99) as f64 / 1000.0,
        "max":histogram.max() as f64 / 1000.0})
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let options = Arc::new(Options::parse());
    let suffix = match options.endpoint {
        Endpoint::Chat => "/chat/completions",
        Endpoint::Completions => "/completions",
    };
    let base = options.url.trim_end_matches('/');
    let url = if base.ends_with("/completions") {
        base.to_owned()
    } else if base.ends_with("/v1") {
        format!("{base}{suffix}")
    } else {
        format!("{base}/v1{suffix}")
    };
    let url = Arc::new(url);
    let body = Arc::new(match options.endpoint {
        Endpoint::Chat => {
            json!({"model":options.model,"stream":true,"messages":[{"role":"user","content":"hi"}]})
        }
        Endpoint::Completions => json!({"model":options.model,"stream":true,"prompt":"hi"}),
    });
    let client = reqwest::Client::builder()
        .tcp_nodelay(true)
        .pool_max_idle_per_host(options.concurrency as usize)
        .timeout(Duration::from_secs(60))
        .build()?;
    let sequence = Arc::new(AtomicU64::new(0));
    let start = Instant::now();
    let deadline = options
        .duration_secs
        .map(|seconds| start + Duration::from_secs(seconds));
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..options.concurrency {
        let (options, url, body, client, sequence) = (
            options.clone(),
            url.clone(),
            body.clone(),
            client.clone(),
            sequence.clone(),
        );
        tasks.spawn(async move {
            let mut stats = Stats::new();
            loop {
                if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    break;
                }
                let index = sequence.fetch_add(1, Ordering::Relaxed);
                if options.requests.is_some_and(|limit| index >= limit) {
                    break;
                }
                stats.requests += 1;
                match request(&client, &url, &body, options.api_key.as_deref()).await {
                    Ok((ttft, total)) => {
                        stats.ttft.record(ttft).expect("auto-resizing histogram");
                        stats.total.record(total).expect("auto-resizing histogram");
                    }
                    Err(_) => stats.errors += 1,
                }
            }
            stats
        });
    }
    let mut stats = Stats::new();
    while let Some(result) = tasks.join_next().await {
        let worker = result?;
        stats.requests += worker.requests;
        stats.errors += worker.errors;
        stats.ttft.add(worker.ttft)?;
        stats.total.add(worker.total)?;
    }
    let elapsed = start.elapsed().as_secs_f64();
    let report = json!({"requests":stats.requests,"errors":stats.errors,"rps":stats.requests as f64 / elapsed,
        "ok_rps":(stats.requests - stats.errors) as f64 / elapsed,
        "ttft_ms":percentiles(&stats.ttft),"total_ms":percentiles(&stats.total)});
    let output = serde_json::to_string_pretty(&report)?;
    if let Some(path) = &options.out {
        std::fs::write(path, format!("{output}\n"))?;
    } else {
        println!("{output}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragmented_crlf_and_multiline_events() {
        let mut decoder = Decoder::default();
        assert!(decoder.feed(b": comment\r\ndata: hel").unwrap().is_empty());
        assert_eq!(
            decoder
                .feed(b"lo\r\ndata: world\r\n\r\ndata: [DONE]\n\n")
                .unwrap(),
            vec!["hello\nworld", "[DONE]"]
        );
    }

    #[tokio::test]
    async fn rejects_http_errors_malformed_events_and_missing_done() {
        use bytes::Bytes;
        use http_body_util::Full;
        use hyper::{service::service_fn, Response};
        use hyper_util::rt::TokioIo;
        use std::convert::Infallible;

        for (status, data) in [
            (500, "data: [DONE]\n\n"),
            (200, "data: not-json\n\n"),
            (
                200,
                "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n",
            ),
            (200, "data: [DONE]\n\n"),
        ] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move {
                let (socket, _) = listener.accept().await.unwrap();
                let service = service_fn(move |_| async move {
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "text/event-stream")
                            .body(Full::new(Bytes::from_static(data.as_bytes())))
                            .unwrap(),
                    )
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(socket), service)
                    .await;
            });
            assert!(request(&reqwest::Client::new(), &url, &json!({}), None)
                .await
                .is_err());
            server.abort();
        }
    }

    #[tokio::test]
    async fn measures_mock_and_reuses_connection() {
        use clap::Parser;
        let options = mock_upstream::Options::parse_from([
            "mock",
            "--latency-ms",
            "0",
            "--stream-chunks",
            "2",
            "--chunk-interval-ms",
            "10",
        ]);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!(
            "http://{}/v1/chat/completions",
            listener.local_addr().unwrap()
        );
        let server = tokio::spawn(mock_upstream::serve(
            listener,
            mock_upstream::App::new(options),
        ));
        let client = reqwest::Client::new();
        let body = json!({"stream":true,"model":"mock"});
        for _ in 0..2 {
            let (ttft, total) = request(&client, &url, &body, None).await.unwrap();
            assert!(total > ttft);
        }
        server.abort();
    }
}

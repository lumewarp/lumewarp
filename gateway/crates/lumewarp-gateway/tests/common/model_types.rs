//! Endpoint/type enforcement and metering through a real gateway process.
use super::*;
use lumewarp_proto::config::v1::ModelType;

const ENDPOINTS: [(&str, &str, ModelType); 8] = [
    ("/v1/chat/completions", "chat_completions", ModelType::Llm),
    ("/v1/completions", "completions", ModelType::Llm),
    ("/v1/responses", "responses", ModelType::Llm),
    ("/v1/embeddings", "embeddings", ModelType::Embedding),
    (
        "/v1/audio/transcriptions",
        "audio_transcriptions",
        ModelType::Transcription,
    ),
    (
        "/v1/audio/translations",
        "audio_translations",
        ModelType::Transcription,
    ),
    ("/v1/decisions", "decisions", ModelType::Decision),
    ("/v1/systemone", "systemone", ModelType::Decision),
];
fn typed_snapshot(addr: SocketAddr) -> Snapshot {
    let mut s = snapshot(addr);
    s.models.clear();
    s.routers.clear();
    s.aliases.clear();
    for (name, kind) in [
        ("llm", ModelType::Llm),
        ("embedding", ModelType::Embedding),
        ("audio", ModelType::Transcription),
        ("decision", ModelType::Decision),
    ] {
        s.models.push(Model {
            name: name.into(),
            upstream: "u".into(),
            upstream_model: "actual".into(),
            r#type: kind as i32,
            price: Some(Price {
                input_nano_usd_per_mtok: 1_000_000_000,
                output_nano_usd_per_mtok: if kind == ModelType::Embedding {
                    0
                } else {
                    2_000_000_000
                },
                nano_usd_per_audio_second: if kind == ModelType::Transcription {
                    6_000_000
                } else {
                    0
                },
            }),
            ..Default::default()
        });
        s.routers.push(Router {
            name: format!("{name}-router"),
            strategy: "weighted".into(),
            members: vec![RouterMember {
                model: name.into(),
                weight: 1,
                ..Default::default()
            }],
            ..Default::default()
        });
        s.aliases.push(Alias {
            name: format!("{name}-alias"),
            target: format!("{name}-router"),
        });
    }
    s.api_keys[0].policy.as_mut().unwrap().allow_all_models = true;
    s.required_capabilities
        .extend(["model_types".into(), "audio_price".into()]);
    s
}
async fn post(gateway: &Gateway, path: &str, name: &str, stream: bool) -> Response<Incoming> {
    if path.starts_with("/v1/audio/") {
        let body = format!("--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test.wav\"\r\nContent-Type: audio/wav\r\n\r\nfile\r\n--b\r\nContent-Disposition: form-data; name=model\r\n\r\n{name}\r\n--b\r\nContent-Disposition: form-data; name=stream\r\n\r\n{stream}\r\n--b--\r\n");
        let request = Request::builder()
            .method("POST")
            .uri(format!("http://{}{path}", gateway.addr))
            .header("authorization", format!("Bearer {KEY}"))
            .header("content-type", "multipart/form-data; boundary=b")
            .body(Full::new(Bytes::from(body)))
            .unwrap();
        bounded(gateway.http.request(request)).await.unwrap()
    } else {
        gateway.post(path, KEY, &format!(r#"{{"model":"{name}","stream":{stream},"previous_response_id":"keep","input":"ping"}}"#)).await
    }
}
#[tokio::test]
async fn type_mismatch_for_every_endpoint_and_callable_kind_is_reported() {
    let upstream = Upstream::start().await;
    let mut cp = FakeCp::start(typed_snapshot(upstream.addr)).await;
    let gateway = Gateway::start(cp.addr).await;
    for (path, _, kind) in ENDPOINTS {
        let wrong = if kind == ModelType::Llm {
            "embedding"
        } else {
            "llm"
        };
        for suffix in ["", "-alias", "-router"] {
            let name = format!("{wrong}{suffix}");
            let response = post(&gateway, path, &name, false).await;
            assert_eq!(response.status(), 400, "{path} {name}");
            let body = json(&bytes(response).await);
            assert_eq!(body["error"]["code"], "model_type_mismatch");
            assert_eq!(
                body["error"]["message"],
                format!("Model \"{name}\" does not serve {path}")
            );
        }
    }
    for (_, endpoint, _) in ENDPOINTS {
        for _ in 0..3 {
            let event = cp.event().await;
            assert_eq!(event.endpoint, endpoint);
            assert_eq!(event.outcome(), RequestOutcome::Rejected);
            assert_eq!(event.attempts, 0);
            assert!(!event.requested.is_empty());
        }
    }
    assert_eq!(
        gateway.metrics().await.sum(
            "lumewarp_requests_rejected_total",
            &[("reason", "model_type_mismatch")]
        ),
        24.0
    );
    let denied = gateway
        .post("/v1/embeddings", LIMITED_KEY, r#"{"model":"llm"}"#)
        .await;
    assert_eq!(denied.status(), 403);
    bytes(denied).await;
    assert!(upstream.scripts.lock().unwrap().is_empty());
    for method in ["GET", "DELETE"] {
        let request = Request::builder()
            .method(method)
            .uri(format!("http://{}/v1/responses/id", gateway.addr))
            .header("authorization", format!("Bearer {KEY}"))
            .body(Full::new(Bytes::new()))
            .unwrap();
        assert_eq!(
            bounded(gateway.http.request(request))
                .await
                .unwrap()
                .status(),
            404
        );
    }
}
#[tokio::test]
async fn multipart_retries_reuse_the_buffered_upload() {
    let mut upstream = Upstream::start().await;
    let mut s = typed_snapshot(upstream.addr);
    s.routers
        .iter_mut()
        .find(|r| r.name == "audio-router")
        .unwrap()
        .retry = Some(lumewarp_proto::config::v1::RetryPolicy {
        max_attempts: 2,
        ..Default::default()
    });
    s.required_capabilities.push("retry".into());
    let mut cp = FakeCp::start(s).await;
    let gateway = Gateway::start(cp.addr).await;
    upstream.respond(503, "application/json", &[b"{\"error\":\"retry\"}"]);
    upstream.respond(
        200,
        "application/json",
        &[br#"{"usage":{"type":"duration","seconds":1}}"#],
    );
    let response = post(&gateway, "/v1/audio/transcriptions", "audio-router", false).await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let first = upstream.received().await;
    let second = upstream.received().await;
    assert_eq!(first.body, second.body);
    assert!(String::from_utf8_lossy(&second.body).contains("\r\nactual\r\n"));
    let event = cp.event().await;
    assert_eq!(event.attempts, 2);
    assert_eq!(event.audio_ms, 1000);
    assert_eq!(event.cost_nano_usd, 6_000_000);
}

#[tokio::test]
async fn new_endpoints_capture_usage_and_rewrite_model() {
    let mut upstream = Upstream::start().await;
    let mut cp = FakeCp::start(typed_snapshot(upstream.addr)).await;
    let gateway = Gateway::start(cp.addr).await;
    for (path, endpoint, kind) in ENDPOINTS {
        let name = match kind {
            ModelType::Llm => "llm-alias",
            ModelType::Embedding => "embedding-alias",
            ModelType::Transcription => "audio-alias",
            ModelType::Decision => "decision-alias",
            _ => unreachable!(),
        };
        let reply: &[u8] = if kind == ModelType::Transcription {
            br#"{"text":"hi","usage":{"type":"duration","seconds":1.5}}"#
        } else {
            br#"{"model":"actual","usage":{"input_tokens":5,"output_tokens":0,"total_tokens":5}}"#
        };
        upstream.respond(200, "application/json", &[reply]);
        let response = post(&gateway, path, name, false).await;
        assert_eq!(response.status(), 200, "{path}");
        let body = json(&bytes(response).await);
        if kind != ModelType::Transcription {
            assert_eq!(body["model"], name);
        }
        let received = upstream.received().await;
        assert_eq!(received.path, path);
        if kind == ModelType::Transcription {
            assert!(String::from_utf8_lossy(&received.body).contains("\r\nactual\r\n"));
            assert!(String::from_utf8_lossy(&received.body).contains("\r\nfile\r\n"));
        } else {
            assert_eq!(json(&received.body)["previous_response_id"], "keep");
        }
        let event = cp.event().await;
        assert_eq!(event.endpoint, endpoint);
        assert_eq!(event.usage_state(), UsageState::Reported);
        assert_eq!(
            event.audio_ms,
            if kind == ModelType::Transcription {
                1500
            } else {
                0
            }
        );
        assert_eq!(
            event.cost_nano_usd,
            if kind == ModelType::Transcription {
                9_000_000
            } else {
                5000
            }
        );
    }
    for (path, name, reply, expected_tokens, audio_ms) in [
        ("/v1/responses", "llm", "event: response.created\ndata: {\"response\":{\"model\":\"actual\"}}\n\nevent: response.completed\ndata: {\"response\":{\"model\":\"actual\",\"usage\":{\"input_tokens\":5,\"output_tokens\":3}}}\n\n", (5,3), 0),
        ("/v1/audio/transcriptions", "audio", "event: transcript.text.done\ndata: {\"usage\":{\"type\":\"duration\",\"seconds\":1.5}}\n\n", (0,0), 1500),
        ("/v1/audio/translations", "audio", "data: {\"usage\":{\"type\":\"tokens\",\"input_tokens\":5,\"output_tokens\":3}}\n\n", (5,3), 0),
    ] {
        upstream.respond(200, "text/event-stream", &[reply.as_bytes()]);
        let response = post(&gateway, path, name, true).await; assert_eq!(response.status(), 200);
        let body = bytes(response).await;
        let received = upstream.received().await;
        if path == "/v1/responses" { assert_eq!(body, reply.replace("\"actual\"", "\"llm\"").as_bytes()); assert!(json(&received.body).get("stream_options").is_none()); }
        else { assert_eq!(body, reply.as_bytes()); }
        let event = cp.event().await; assert_eq!(event.usage_state(), UsageState::Reported); assert_eq!((event.prompt_tokens,event.completion_tokens), expected_tokens); assert_eq!(event.audio_ms, audio_ms);
    }
    for content_type in ["text/plain", "text/vtt", "application/x-subrip"] {
        upstream.respond(200, content_type, &[b"opaque transcript"]);
        let response = post(&gateway, "/v1/audio/transcriptions", "audio", false).await;
        assert_eq!(bytes(response).await, "opaque transcript");
        upstream.received().await;
        assert_eq!(cp.event().await.usage_state(), UsageState::Missing);
    }
}

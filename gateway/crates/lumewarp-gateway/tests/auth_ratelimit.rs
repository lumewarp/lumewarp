mod common;

use std::time::Duration;

use bytes::Bytes;
use common::*;
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use sha2::{Digest, Sha256};

const NORMAL: &[u8] = br#"{"model":"public"}"#;
const STREAM: &[u8] = br#"{"model":"public","stream":true}"#;

fn hash(secret: &str) -> String {
    hex::encode(Sha256::digest(secret.as_bytes()))
}

/// `keys` entries are `(secret, extra YAML fields)`; key ids equal secrets.
fn yaml(teams: &str, keys: &[(&str, &str)]) -> String {
    let mut yaml = format!("teams:\n{teams}keys:\n");
    for (secret, extra) in keys {
        yaml.push_str(&format!(
            "  - {{key_id: {secret}, key_sha256: '{}', allow_all_models: true{extra}}}\n",
            hash(secret)
        ));
    }
    yaml
}

async fn start(mock: &Mock, extra: &str) -> Gateway {
    Gateway::start_with(&[mock.addr], false, 2000, "", extra).await
}

async fn post(gateway: &Gateway, secret: &str, body: &'static [u8]) -> Response<Incoming> {
    let auth = format!("Bearer {secret}");
    gateway
        .request(
            "POST",
            "/v1/chat/completions",
            body,
            &[("authorization", &auth)],
        )
        .await
}

async fn status(gateway: &Gateway, secret: &str) -> (u16, Option<String>, Value) {
    let response = post(gateway, secret, NORMAL).await;
    let status = response.status().as_u16();
    let retry = response
        .headers()
        .get("retry-after")
        .map(|v| v.to_str().unwrap().to_owned());
    let body = bytes(response).await;
    (
        status,
        retry,
        serde_json::from_slice(&body).unwrap_or(Value::Null),
    )
}

async fn first_event(response: Response<Incoming>) -> Incoming {
    assert_eq!(response.status(), 200);
    let mut body = response.into_body();
    bounded(async {
        let mut received = Vec::new();
        while received.len() < FIRST.len() {
            if let Ok(data) = body.frame().await.unwrap().unwrap().into_data() {
                received.extend_from_slice(&data);
            }
        }
    })
    .await;
    body
}

#[tokio::test]
async fn rpm_exhaustion_and_refill() {
    let mock = Mock::start(Behavior::Json).await;
    let extra = yaml(
        "  []\n",
        &[("rpm", ", rate_limits: {requests_per_minute: 120}")],
    );
    let gateway = start(&mock, &extra).await;
    // Burst capacity is the full minute's allowance, refilled at 2/s.
    let mut admitted = 0;
    let limited = loop {
        let (code, retry, body) = status(&gateway, "rpm").await;
        if code != 200 {
            break (code, retry, body);
        }
        admitted += 1;
        assert!(admitted <= 130, "never limited");
    };
    assert!((120..=125).contains(&admitted), "admitted {admitted}");
    assert_eq!(limited.0, 429);
    assert_eq!(limited.1.as_deref(), Some("1"));
    assert_eq!(limited.2["error"]["code"], "rate_limit_exceeded");
    assert_eq!(limited.2["error"]["type"], "rate_limit_error");
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(status(&gateway, "rpm").await.0, 200, "refilled");
    assert_eq!(status(&gateway, "rpm").await.0, 429);
}

#[tokio::test]
async fn team_scope_is_shared_across_keys() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let extra = yaml(
        "  - {id: team-a, rate_limits: {max_parallel_requests: 1}}\n  - {id: team-b}\n",
        &[
            ("a1", ", team_id: team-a"),
            ("a2", ", team_id: team-a"),
            ("b1", ", team_id: team-b"),
        ],
    );
    let gateway = start(&mock, &extra).await;
    let held = first_event(post(&gateway, "a1", STREAM).await).await;
    let control = mock.next().await.stream.unwrap();
    for secret in ["a1", "a2"] {
        let (code, retry, body) = status(&gateway, secret).await;
        assert_eq!(code, 429, "{secret}");
        assert_eq!(retry.as_deref(), Some("1"));
        assert_eq!(body["error"]["code"], "rate_limit_exceeded");
    }
    // Another team is unaffected.
    assert_eq!(status(&gateway, "b1").await.0, 200);
    mock.next().await;
    // Finishing the stream frees the team slot.
    control.frames.send(Bytes::from_static(LAST)).await.unwrap();
    drop(control.frames);
    bounded(held.collect()).await.unwrap();
    assert_eq!(status(&gateway, "a2").await.0, 200);
}

#[tokio::test]
async fn max_parallel_released_on_client_disconnect() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let extra = yaml(
        "  []\n",
        &[("p", ", rate_limits: {max_parallel_requests: 1}")],
    );
    let gateway = start(&mock, &extra).await;
    // Own the connection so aborting it is a real TCP disconnect.
    let socket = bounded(tokio::net::TcpStream::connect(gateway.addr))
        .await
        .unwrap();
    let (mut sender, connection) =
        hyper::client::conn::http1::handshake::<_, Full<Bytes>>(TokioIo::new(socket))
            .await
            .unwrap();
    let driver = tokio::spawn(connection);
    let request = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("host", gateway.addr.to_string())
        .header("authorization", "Bearer p")
        .body(Full::new(Bytes::from_static(STREAM)))
        .unwrap();
    let response = bounded(sender.send_request(request)).await.unwrap();
    let body = first_event(response).await;
    let control = mock.next().await.stream.unwrap();
    assert_eq!(status(&gateway, "p").await.0, 429);
    drop(body);
    drop(sender);
    driver.abort();
    let _ = driver.await;
    let _ = control
        .frames
        .send(Bytes::from_static(b"data: probe\n\n"))
        .await;
    tokio::time::timeout(Duration::from_secs(2), control.closed)
        .await
        .expect("gateway kept the upstream stream after client disconnect")
        .unwrap();
    // The permit is released with the response body.
    let code = bounded(async {
        loop {
            let (code, _, _) = status(&gateway, "p").await;
            if code != 429 {
                return code;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert_eq!(code, 200);
}

#[tokio::test]
async fn key_status_and_expiry() {
    let mut mock = Mock::start(Behavior::Json).await;
    let extra = yaml(
        "  []\n",
        &[
            ("ok", ""),
            ("budget", ", status: budget_exceeded"),
            ("revoked", ", status: revoked"),
            ("expired", ", expires_at_unix_ms: 1000"),
            (
                "expired-budget",
                ", status: budget_exceeded, expires_at_unix_ms: 1000",
            ),
            ("future", ", expires_at_unix_ms: 9999999999999"),
        ],
    );
    let gateway = start(&mock, &extra).await;
    let (code, retry, body) = status(&gateway, "budget").await;
    assert_eq!(code, 429);
    assert_eq!(retry, None);
    assert_eq!(body["error"]["code"], "insufficient_quota");
    for secret in ["revoked", "expired", "expired-budget", "missing"] {
        let (code, _, body) = status(&gateway, secret).await;
        assert_eq!(code, 401, "{secret}");
        assert_eq!(body["error"]["code"], "invalid_api_key");
    }
    assert!(mock.records.try_recv().is_err(), "rejected before upstream");
    for secret in ["ok", "future"] {
        assert_eq!(status(&gateway, secret).await.0, 200, "{secret}");
        mock.next().await;
    }
}

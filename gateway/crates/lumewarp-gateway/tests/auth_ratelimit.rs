mod common;

use std::time::{Duration, SystemTime, UNIX_EPOCH};

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

fn unix_ms() -> u64 {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before the epoch")
            .as_millis(),
    )
    .expect("system clock out of range")
}

/// The wall-clock minute the limiter counts in.
fn minute_now() -> u64 {
    unix_ms() / 60_000
}

/// What the sliding limiter answers once the current minute's window is full:
/// the wait to the next wall-clock minute, plus the ~60 ms it takes for the
/// previous window to fade one count under the limit.
fn boundary_retry_after(now_ms: u64) -> u64 {
    (60_060u64.saturating_sub(now_ms % 60_000))
        .div_ceil(1000)
        .clamp(1, 60)
}

#[tokio::test]
async fn rpm_exhaustion_and_retry_after() {
    let mock = Mock::start(Behavior::Json).await;
    let extra = yaml(
        "  []\n",
        &[
            ("rpm", ", rate_limits: {requests_per_minute: 120}"),
            ("free", ""),
        ],
    );
    let gateway = start(&mock, &extra).await;
    let started_minute = minute_now();
    // The sliding window allows one full minute's allowance, however fast the
    // requests arrive.
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
    assert_eq!(limited.2["error"]["code"], "rate_limit_exceeded");
    assert_eq!(limited.2["error"]["type"], "rate_limit_error");
    let retry: u64 = limited
        .1
        .as_deref()
        .expect("Retry-After on a rate-limit rejection")
        .parse()
        .expect("Retry-After is a whole number of seconds");
    assert!((1..=60).contains(&retry), "Retry-After {retry}");

    // Another key without a limit is unaffected.
    assert_eq!(status(&gateway, "free").await.0, 200);

    // Away from a minute boundary the burst filled the current window, so the
    // answer is the wait to the next boundary, not a per-request refill. Skip
    // the exact check when the burst straddled a boundary or the clock is
    // within 2 s of one, where the limiter's 5 ms clock could disagree. Two
    // seconds of slack cover the requests' own latency and a delayed clock
    // tick; the result is still tens of seconds away from a GCRA refill.
    let now = unix_ms();
    if minute_now() == started_minute && (2_000..58_000).contains(&(now % 60_000)) {
        let expected = boundary_retry_after(now);
        assert!(
            retry.abs_diff(expected) <= 2,
            "Retry-After {retry}, expected about {expected}"
        );
        // The window stays closed inside the same minute; it does not
        // trickle back per request. Recovery as the previous minute ages is
        // covered deterministically by
        // ratelimit::tests::sliding_window_ages_the_previous_minute.
        assert_eq!(status(&gateway, "rpm").await.0, 429);
    }
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

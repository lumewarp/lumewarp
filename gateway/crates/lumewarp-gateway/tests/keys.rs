mod common;

use bytes::Bytes;
use common::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const BODY: &[u8] = br#"{"model":"public"}"#;

fn hash(secret: &str) -> String {
    hex::encode(Sha256::digest(secret.as_bytes()))
}

fn keys_yaml() -> String {
    format!(
        r#"teams:
  - {{id: team-a}}
  - {{id: team-b}}
keys:
  - {{key_id: limited, key_sha256: '{}', team_id: team-a, allowed_models: [public]}}
  - {{key_id: all, key_sha256: '{}', team_id: team-b, allow_all_models: true}}
  - {{key_id: deny-all, key_sha256: '{}', team_id: team-a}}
  - {{key_id: revoked, key_sha256: '{}', status: revoked, allow_all_models: true}}
  - {{key_id: suspended, key_sha256: '{}', status: suspended, allow_all_models: true}}
  - {{key_id: expired, key_sha256: '{}', expires_at_unix_ms: 1, allow_all_models: true}}
  - {{key_id: budget, key_sha256: '{}', status: budget_exceeded, allow_all_models: true}}
"#,
        hash("limited"),
        hash("all"),
        hash("deny-all"),
        hash("revoked"),
        hash("suspended"),
        hash("expired"),
        hash("budget"),
    )
}

async fn start(mock: &Mock) -> Gateway {
    Gateway::start_with(&[mock.addr], false, 2000, "", &keys_yaml()).await
}

async fn call(gateway: &Gateway, secret: &str, body: &'static [u8]) -> (u16, Value) {
    let auth = format!("Bearer {secret}");
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            body,
            &[("authorization", &auth)],
        )
        .await;
    let status = response.status().as_u16();
    let body = bytes(response).await;
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

async fn models(gateway: &Gateway, secret: &str) -> (u16, Vec<String>) {
    let auth = format!("Bearer {secret}");
    let response = gateway
        .request(
            "GET",
            "/v1/models",
            Bytes::new(),
            &[("authorization", &auth)],
        )
        .await;
    let status = response.status().as_u16();
    let body: Value = serde_json::from_slice(&bytes(response).await).unwrap();
    let mut ids: Vec<String> = body["data"]
        .as_array()
        .map(|data| {
            data.iter()
                .map(|m| m["id"].as_str().unwrap().to_owned())
                .collect()
        })
        .unwrap_or_default();
    ids.sort();
    (status, ids)
}

#[tokio::test]
async fn virtual_key_allowed_and_denied_models() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = start(&mock).await;
    let (status, _) = call(&gateway, "limited", BODY).await;
    assert_eq!(status, 200);
    assert_eq!(mock.next().await.body, br#"{"model":"actual"}"#.as_slice());
    let (status, body) = call(&gateway, "limited", br#"{"model":"another-alias"}"#).await;
    assert_eq!(status, 403);
    assert_eq!(body["error"]["code"], "model_not_allowed");
    assert_eq!(body["error"]["type"], "permission_error");
    // Not allowed beats not found: denied callers cannot probe aliases.
    let (status, _) = call(&gateway, "limited", br#"{"model":"nope"}"#).await;
    assert_eq!(status, 403);
    let (status, body) = call(&gateway, "all", br#"{"model":"nope"}"#).await;
    assert_eq!(status, 404);
    assert_eq!(body["error"]["code"], "model_not_found");
    let (status, _) = call(&gateway, "all", br#"{"model":"another-alias"}"#).await;
    assert_eq!(status, 200);
    mock.next().await;
    assert!(
        mock.records.try_recv().is_err(),
        "denied requests must not reach upstream"
    );
    assert_eq!(
        models(&gateway, "limited").await,
        (200, vec!["public".to_owned()])
    );
    assert_eq!(
        models(&gateway, "all").await,
        (200, vec!["another-alias".to_owned(), "public".to_owned()])
    );
}

#[tokio::test]
async fn revoked_suspended_expired_and_unknown_keys_are_401() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = start(&mock).await;
    for secret in ["revoked", "suspended", "expired", "unknown", ""] {
        let (status, body) = call(&gateway, secret, BODY).await;
        assert_eq!(status, 401, "{secret}");
        assert_eq!(body["error"]["code"], "invalid_api_key");
        assert_eq!(models(&gateway, secret).await.0, 401);
    }
    let response = gateway
        .request("POST", "/v1/chat/completions", BODY, &[])
        .await;
    assert_eq!(response.status(), 401);
    bytes(response).await;
    let (status, body) = call(&gateway, "budget", BODY).await;
    assert_eq!(status, 429);
    assert_eq!(body["error"]["code"], "insufficient_quota");
    assert_eq!(body["error"]["type"], "insufficient_quota");
    assert!(mock.records.try_recv().is_err());
    // /healthz stays open.
    let response = gateway.request("GET", "/healthz", Bytes::new(), &[]).await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
}

#[tokio::test]
async fn deny_all_key_calls_nothing() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = start(&mock).await;
    for body in [
        BODY,
        br#"{"model":"another-alias"}"#.as_slice(),
        br#"{"model":"unknown"}"#,
    ] {
        let (status, _) = call(&gateway, "deny-all", body).await;
        assert_eq!(status, 403);
    }
    assert_eq!(models(&gateway, "deny-all").await, (200, vec![]));
    assert!(mock.records.try_recv().is_err());
}

#[tokio::test]
async fn team_label_in_request_metrics() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = start(&mock).await;
    let before = gateway.metrics().await;
    assert_eq!(
        before.sum("lumewarp_requests_total", &[("team", "team-a")]),
        0.0
    );
    assert_eq!(call(&gateway, "limited", BODY).await.0, 200);
    mock.next().await;
    assert_eq!(
        call(&gateway, "limited", br#"{"model":"another-alias"}"#)
            .await
            .0,
        403
    );
    assert_eq!(call(&gateway, "all", BODY).await.0, 200);
    mock.next().await;
    assert_eq!(call(&gateway, "revoked", BODY).await.0, 401);
    let metrics = bounded(async {
        loop {
            let m = gateway.metrics().await;
            if m.sum(
                "lumewarp_requests_total",
                &[("endpoint", "chat_completions")],
            ) >= 4.0
            {
                return m;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await;
    let series = |labels: &[(&str, &str)]| metrics.value("lumewarp_requests_total", labels);
    assert_eq!(
        series(&[
            ("requested", "public"),
            ("deployment", "d0"),
            ("endpoint", "chat_completions"),
            ("team", "team-a"),
            ("status", "200"),
        ]),
        1.0
    );
    assert_eq!(
        series(&[
            ("requested", "another-alias"),
            ("deployment", ""),
            ("endpoint", "chat_completions"),
            ("team", "team-a"),
            ("status", "403"),
        ]),
        1.0
    );
    assert_eq!(
        series(&[
            ("requested", "public"),
            ("deployment", "d0"),
            ("endpoint", "chat_completions"),
            ("team", "team-b"),
            ("status", "200"),
        ]),
        1.0
    );
    // Pre-auth rejections carry no team.
    assert_eq!(
        series(&[
            ("requested", ""),
            ("deployment", ""),
            ("endpoint", "chat_completions"),
            ("team", ""),
            ("status", "401"),
        ]),
        1.0
    );
    // No key ids or secrets ever become labels.
    assert!(!metrics.samples.iter().any(|s| s
        .labels
        .values()
        .any(|v| v == "limited" || v == "all" || v == &hash("limited"))));
}

#[tokio::test]
async fn authenticated_traffic_without_a_model_keeps_the_team() {
    let mock = Mock::start(Behavior::Json).await;
    let gateway = start(&mock).await;
    assert_eq!(models(&gateway, "limited").await.0, 200);
    assert_eq!(models(&gateway, "nope").await.0, 401);
    assert_eq!(call(&gateway, "limited", b"{bad").await.0, 400);
    assert_eq!(call(&gateway, "nope", b"{bad").await.0, 401);
    let metrics = bounded(async {
        loop {
            let m = gateway.metrics().await;
            if m.sum("lumewarp_requests_total", &[("endpoint", "models")]) >= 2.0
                && m.sum(
                    "lumewarp_requests_total",
                    &[("endpoint", "chat_completions")],
                ) >= 2.0
            {
                return m;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await;
    let series = |route: &str, team: &str, status: &str| {
        metrics.value(
            "lumewarp_requests_total",
            &[
                ("requested", ""),
                ("deployment", ""),
                ("endpoint", route),
                ("team", team),
                ("status", status),
            ],
        )
    };
    assert_eq!(series("models", "team-a", "200"), 1.0);
    assert_eq!(series("models", "", "401"), 1.0);
    assert_eq!(series("models", "", "200"), 0.0);
    assert_eq!(series("chat_completions", "team-a", "400"), 1.0);
    assert_eq!(series("chat_completions", "", "401"), 1.0);
    assert_eq!(series("chat_completions", "", "400"), 0.0);
}

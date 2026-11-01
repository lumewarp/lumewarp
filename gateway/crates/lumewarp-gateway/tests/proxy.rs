mod common;

use std::{
    io::{BufRead, BufReader, Write},
    time::Duration,
};

use bytes::Bytes;
use common::*;
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;

async fn first_chunk(response: Response<Incoming>) -> Incoming {
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let mut body = response.into_body();
    let first = bounded(async {
        let mut received = Vec::new();
        while received.len() < FIRST.len() {
            let frame = body.frame().await.expect("first event before EOF").unwrap();
            if let Ok(data) = frame.into_data() {
                received.extend_from_slice(&data);
            }
        }
        received
    })
    .await;
    assert_eq!(first, FIRST);
    body
}

async fn complete_stream(body: Incoming, control: StreamControl) {
    bounded(control.frames.send(Bytes::from_static(LAST)))
        .await
        .unwrap();
    drop(control.frames);
    let rest = bounded(body.collect()).await.unwrap().to_bytes();
    assert_eq!(rest, LAST);
}

#[tokio::test]
async fn non_stream_endpoints_rewrite_only_model_and_preserve_raw_bytes() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    for endpoint in ["chat/completions", "completions", "embeddings"] {
        // Exact whitespace/order, escaped strings and nested model fields must survive.
        let original = br#"{ "unknown" : [1, {"model":"public"}], "model" : "public", "extra_body": {"n":1.2300e+02}, "guided_json":{"type":"object"}, "input":["hi"], "messages":[{"role":"user","content":"hi\nthere"}], "stream":false }"#;
        let mut expected = Vec::new();
        let token = b"\"model\" : \"public\"";
        let position = original
            .windows(token.len())
            .position(|part| part == token)
            .unwrap();
        expected.extend_from_slice(&original[..position]);
        expected.extend_from_slice(b"\"model\" : \"actual\"");
        expected.extend_from_slice(&original[position + token.len()..]);
        let path = format!("/v1/{endpoint}?x=a%2Fb&x=two");
        let response = gateway
            .request(
                "POST",
                &path,
                original.as_slice(),
                &[("content-type", "application/json")],
            )
            .await;
        assert_eq!(response.status(), 200);
        assert_eq!(bytes(response).await, JSON);
        let received = mock.next().await;
        assert_eq!(received.method, hyper::Method::POST);
        assert_eq!(
            received.path,
            format!("/prefix/v1/{endpoint}?x=a%2Fb&x=two")
        );
        assert_eq!(received.body, expected);
    }
}

#[tokio::test]
async fn sse_reaches_client_before_next_chunk_is_released() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public","stream":true}"#.as_slice(),
            &[],
        )
        .await;
    let control = mock.next().await.stream.unwrap();
    // LAST does not exist in the channel until after FIRST is observed downstream.
    let body = first_chunk(response).await;
    complete_stream(body, control).await;
}

#[tokio::test]
async fn auth_and_upstream_credentials_are_independent() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], true, 2000).await;
    for headers in [vec![], vec![("authorization", "Bearer wrong")]] {
        let response = gateway
            .request(
                "POST",
                "/v1/chat/completions",
                br#"{"model":"public"}"#.as_slice(),
                &headers,
            )
            .await;
        assert_eq!(response.status(), 401);
        assert!(
            serde_json::from_slice::<Value>(&bytes(response).await).unwrap()["error"].is_object()
        );
    }
    assert!(
        mock.records.try_recv().is_err(),
        "unauthorized requests must not reach upstream"
    );
    let response = gateway.request("GET", "/healthz", Bytes::new(), &[]).await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[("authorization", "Bearer client-secret")],
        )
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let request = mock.next().await;
    assert_eq!(request.headers["authorization"], "Bearer upstream-secret");
    assert!(!request
        .headers
        .values()
        .any(|value| value.as_bytes() == b"Bearer client-secret"));
    let open = Gateway::start(&[mock.addr], false, 2000).await;
    let response = open
        .request(
            "POST",
            "/v1/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    assert_eq!(
        mock.next().await.headers["authorization"],
        "Bearer upstream-secret"
    );
}

#[tokio::test]
async fn strips_hop_and_account_headers_but_propagates_traceparent() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let trace = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
    let headers = [
        ("content-type", "application/json"),
        ("openai-organization", "client-org"),
        ("openai-project", "client-project"),
        ("expect", "100-continue"),
        ("connection", "x-private, keep-alive"),
        ("x-private", "hidden"),
        ("keep-alive", "timeout=10"),
        ("te", "trailers"),
        ("trailer", "x-trailer"),
        ("upgrade", "websocket"),
        ("proxy-authorization", "secret"),
        ("proxy-custom", "hidden"),
        ("traceparent", trace),
    ];
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &headers,
        )
        .await;
    assert_eq!(response.status(), 200);
    assert!(!response.headers().contains_key("set-cookie"));
    assert!(!response.headers().contains_key("x-response-private"));
    assert_eq!(response.headers()["x-upstream"], "kept");
    bytes(response).await;
    let request = mock.next().await;
    for name in [
        "openai-organization",
        "openai-project",
        "expect",
        "connection",
        "x-private",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-custom",
    ] {
        assert!(!request.headers.contains_key(name), "leaked {name}");
    }
    assert_eq!(request.headers["traceparent"], trace);
    assert_eq!(request.headers["content-type"], "application/json");
}

#[tokio::test]
async fn errors_and_null_stream() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"unknown"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 404);
    let body: Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["error"]["code"], "model_not_found");
    for invalid in [
        b"{broken".as_slice(),
        b"[]",
        br#"{"model":"public","stream":"true"}"#,
    ] {
        let response = gateway
            .request("POST", "/v1/chat/completions", invalid, &[])
            .await;
        assert_eq!(response.status(), 400);
        assert!(
            serde_json::from_slice::<Value>(&bytes(response).await).unwrap()["error"].is_object()
        );
    }
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public","stream":null}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    assert_eq!(
        mock.next().await.body,
        br#"{"model":"actual","stream":null}"#.as_slice()
    );
    assert!(mock.records.try_recv().is_err());
}

#[tokio::test]
async fn upstream_connection_failures_and_timeout() {
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = closed.local_addr().unwrap();
    let gateway = Gateway::start(&[addr], false, 200).await;
    drop(closed);
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 502);
    assert!(serde_json::from_slice::<Value>(&bytes(response).await).unwrap()["error"].is_object());

    let close = Mock::start(Behavior::ImmediateClose).await;
    let gateway = Gateway::start(&[close.addr], false, 200).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 502);
    bytes(response).await;

    let mut hang = Mock::start(Behavior::Hang).await;
    let gateway = Gateway::start(&[hang.addr], false, 200).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 504);
    assert!(serde_json::from_slice::<Value>(&bytes(response).await).unwrap()["error"].is_object());
    hang.next().await;
}

#[tokio::test]
async fn oversized_content_length_is_rejected_before_reading_body() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let addr = gateway.addr;
    let status = bounded(tokio::task::spawn_blocking(move || {
        let mut socket = std::net::TcpStream::connect(addr).unwrap();
        socket.set_read_timeout(Some(WAIT)).unwrap();
        socket.set_write_timeout(Some(WAIT)).unwrap();
        // Deliberately send only headers. The gateway must not wait for 32 MiB.
        write!(socket, "POST /v1/chat/completions HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", 32 * 1024 * 1024 + 1).unwrap();
        let mut status = String::new();
        BufReader::new(socket).read_line(&mut status).unwrap();
        status
    })).await.unwrap();
    assert!(status.starts_with("HTTP/1.1 413"), "{status}");
    assert!(mock.records.try_recv().is_err());
}

#[tokio::test]
async fn models_lists_router_aliases() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let response = gateway
        .request("GET", "/v1/models", Bytes::new(), &[])
        .await;
    assert_eq!(response.status(), 200);
    let body: Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["object"], "list");
    let mut models: Vec<_> = body["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|model| model["id"].as_str().unwrap())
        .collect();
    models.sort_unstable();
    assert_eq!(models, ["another-alias", "public"]);
    assert!(
        mock.records.try_recv().is_err(),
        "models must be served locally"
    );
}

#[tokio::test]
async fn least_in_flight_avoids_the_model_with_an_open_stream() {
    let mut a = Mock::start(Behavior::ControlledStream).await;
    let mut b = Mock::start(Behavior::ControlledStream).await;
    let gateway = Gateway::start(&[a.addr, b.addr], false, 2000).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public","stream":true}"#.as_slice(),
            &[],
        )
        .await;
    // Ties are randomized: name the model selected first A for this test.
    let (first_is_a, first) = bounded(async {
        tokio::select! {
            record = a.records.recv() => (true, record.unwrap()),
            record = b.records.recv() => (false, record.unwrap()),
        }
    })
    .await;
    let body = first_chunk(response).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    // Waiting on the opposite record channel fails if the busy model is reused.
    let second = if first_is_a {
        b.next().await
    } else {
        a.next().await
    };
    assert_eq!(
        serde_json::from_slice::<Value>(&second.body).unwrap()["model"],
        "actual"
    );
    assert!(a.records.try_recv().is_err());
    assert!(b.records.try_recv().is_err());
    complete_stream(body, first.stream.unwrap()).await;
}

#[tokio::test]
async fn client_disconnect_closes_the_upstream_stream_within_two_seconds() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let gateway = Gateway::start(&[mock.addr], false, 2000).await;
    // Own the downstream connection driver so abort means a real TCP disconnect,
    // not merely dropping a pooled response and leaving a socket alive.
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
        .body(Full::new(Bytes::from_static(
            br#"{"model":"public","stream":true}"#,
        )))
        .unwrap();
    let response = bounded(sender.send_request(request)).await.unwrap();
    let body = first_chunk(response).await;
    let control = mock.next().await.stream.unwrap();
    drop(body);
    drop(sender);
    driver.abort();
    let _ = driver.await;
    // Keep the producer alive: a body-drop notification cannot be normal EOF.
    let _ = control
        .frames
        .send(Bytes::from_static(b"data: probe\n\n"))
        .await;
    tokio::time::timeout(Duration::from_secs(2), control.closed)
        .await
        .expect("gateway retained upstream after client disconnect")
        .unwrap();
    drop(control.frames);
}

#[cfg(unix)]
#[tokio::test]
async fn sigterm_drains_an_in_flight_stream_and_exits_successfully() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let mut gateway = Gateway::start(&[mock.addr], false, 2000).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public","stream":true}"#.as_slice(),
            &[],
        )
        .await;
    let control = mock.next().await.stream.unwrap();
    let body = first_chunk(response).await;
    let status = std::process::Command::new("kill")
        .arg("-TERM")
        .arg(gateway.child.id().to_string())
        .status()
        .unwrap();
    assert!(status.success());
    // Observe shutdown beginning before releasing the stream; no fixed sleep.
    bounded(async {
        loop {
            assert!(
                gateway.child.try_wait().unwrap().is_none(),
                "gateway exited before stream drain"
            );
            if tokio::net::TcpStream::connect(gateway.addr).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    complete_stream(body, control).await;
    let status = bounded(async {
        loop {
            if let Some(status) = gateway.child.try_wait().unwrap() {
                break status;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    assert!(status.success(), "gateway shutdown status: {status}");
}

//! MCP routing, discovery review and immutable session membership.
use super::{
    session::{self, Cursor, Leg, Session},
    transport, Call, Route,
};
use crate::{
    auth::Caller, catalog, error, mcp_hash, metrics, proxy::ProxyBody, targets::McpTarget,
};
use bytes::Bytes;
use hyper::{HeaderMap, Method, Response, StatusCode};
use lumewarp_proto::config::v1::McpDefinitionKind as Kind;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

#[derive(Clone)]
pub(super) struct Counted;

pub async fn handle(call: Call<'_>) -> Response<ProxyBody> {
    if call.runtime.id_key.is_none() {
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "mcp_not_configured",
            "MCP requires LUMEWARP_GATEWAY_ID_KEY for session sealing",
        );
    }
    let headers = call.http.headers().clone();
    // Browser origins are not trusted by default. Public URL is operator-supplied.
    if let Some(origin) = headers.get("origin") {
        let valid = origin
            .to_str()
            .ok()
            .and_then(|s| s.parse::<hyper::Uri>().ok())
            .is_some_and(|origin| {
                call.runtime
                    .public_url
                    .as_deref()
                    .and_then(|s| s.parse::<hyper::Uri>().ok())
                    .is_some_and(|public| {
                        origin.scheme() == public.scheme()
                            && origin.authority() == public.authority()
                            && origin.path() == "/"
                            && origin.query().is_none()
                    })
            });
        if !valid {
            return fail(StatusCode::FORBIDDEN, "Origin is not allowed");
        }
    }
    if !matches!(call.method, Method::POST | Method::GET | Method::DELETE) {
        return transport::empty(StatusCode::METHOD_NOT_ALLOWED);
    }
    if (call.method == Method::POST
        && !(accepts(&headers, "application/json") && accepts(&headers, "text/event-stream")))
        || (call.method == Method::GET && !accepts(&headers, "text/event-stream"))
    {
        return fail(
            StatusCode::NOT_ACCEPTABLE,
            "Accept must include the MCP response formats",
        );
    }
    if call.method == Method::POST
        && !headers
            .get("content-type")
            .and_then(|h| h.to_str().ok())
            .is_some_and(|h| h.split(';').next() == Some("application/json"))
    {
        return fail(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "Expected application/json",
        );
    }
    // Keep the route context while taking ownership of the incoming body.
    let Call {
        route,
        method,
        http,
        caller,
        routing_table,
        targets,
        runtime,
        request,
        body_read_timeout,
    } = call;
    let context = Context {
        route,
        caller,
        routing_table,
        targets,
        runtime,
        request,
        headers: &headers,
    };
    let value = if method == Method::POST {
        let bytes = match tokio::time::timeout(
            body_read_timeout,
            transport::collect(http.into_body(), transport::REQUEST_LIMIT),
        )
        .await
        {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(())) => {
                return fail(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "MCP request exceeds 4 MiB or is unreadable",
                )
            }
            Err(_) => return fail(StatusCode::REQUEST_TIMEOUT, "MCP body read timed out"),
        };
        match serde_json::from_slice::<Value>(&bytes) {
            Ok(value) if valid_message(&value) => value,
            _ => return rpc(Value::Null, -32600, "Invalid JSON-RPC message"),
        }
    } else {
        Value::Null
    };
    let rpc_method = value.get("method").and_then(Value::as_str).unwrap_or("");
    let label = method_label(rpc_method);
    let mut response = context.dispatch(&method, value.clone()).await;
    response.extensions_mut().insert(Counted);
    let server = match route {
        Route::Aggregate => "_aggregate",
        Route::Server { name } if targets.server(name).is_some() => name,
        _ => "_unknown",
    };
    runtime.metrics.mcp_request_handles(server, label)
        [metrics::status_index(response.status().as_u16())]
    .inc();
    if rpc_method == "tools/call" {
        let raw = value
            .pointer("/params/name")
            .and_then(Value::as_str)
            .unwrap_or("");
        let (server, tool) = match route {
            Route::Aggregate => raw.split_once("__").unwrap_or(("_unknown", "")),
            Route::Server { name } => (name, raw),
        };
        let server = if targets.server(server).is_some() {
            server
        } else {
            "_unknown"
        };
        let approved = targets
            .server(server)
            .is_some_and(|s| s.approved().has_approved(Kind::Tool, tool));
        let team = match caller {
            Caller::Key(key) => key.team_id.as_ref(),
            _ => "",
        };
        runtime
            .metrics
            .mcp_tool_call_handles(server, tool, team, approved)
            [metrics::status_index(response.status().as_u16())]
        .inc();
    }
    // A successful stream is reported by `TrafficRecorder` when it ends (its
    // outcome is not known at handoff); everything else is final now. An error
    // status is never a stream: it is recorded here as the rejection it is.
    let streamed = transport::is_sse(response.headers()) && response.status().is_success();
    if value.get("id").is_some() && !rpc_method.is_empty() && !streamed {
        let (target, tool) = context.usage_identity(&value);
        let status = response.status().as_u16();
        use lumewarp_proto::usage::v1::{RequestOutcome, TrafficKind};
        crate::usage::record_traffic(
            runtime,
            crate::usage::TrafficEvent {
                kind: TrafficKind::Mcp,
                request,
                caller,
                target,
                method: rpc_method,
                tool,
                status,
                stream: false,
                config_version: routing_table.version,
                outcome: if let Some(outcome) = response
                    .extensions()
                    .get::<lumewarp_proto::usage::v1::RequestOutcome>()
                {
                    *outcome
                } else if status >= 500 {
                    RequestOutcome::UpstreamError
                } else if status >= 400 {
                    RequestOutcome::Rejected
                } else {
                    RequestOutcome::Completed
                },
            },
        );
    }
    response
}
fn valid_message(v: &Value) -> bool {
    v.is_object()
        && v["jsonrpc"] == "2.0"
        && v.get("id")
            .is_none_or(|id| id.is_string() || id.is_i64() || id.is_u64())
        && v.get("params").is_none_or(Value::is_object)
        && ((v.get("method").is_some_and(Value::is_string)
            && v.get("result").is_none()
            && v.get("error").is_none())
            || (v.get("method").is_none()
                && v.get("id").is_some()
                && (v.get("result").is_some() ^ v.get("error").is_some())))
}
struct Context<'a> {
    route: Route<'a>,
    caller: &'a Caller,
    routing_table: &'a crate::router::RoutingTable,
    targets: &'a crate::targets::Targets,
    runtime: &'a crate::runtime::Runtime,
    request: &'a crate::usage::RequestCtx,
    headers: &'a HeaderMap,
}
impl Context<'_> {
    fn owner(&self) -> &str {
        match self.caller {
            Caller::Key(k) => &k.key_id,
            _ => unreachable!("MCP is authenticated"),
        }
    }
    fn grants(&self) -> &crate::grants::Grants {
        match self.caller {
            Caller::Key(k) => &k.grants,
            _ => unreachable!(),
        }
    }
    fn route_name(&self) -> &str {
        match self.route {
            Route::Aggregate => "",
            Route::Server { name } => name,
        }
    }
    /// The usage identity of one forwarded request: the target name when the
    /// key may see it (`""` otherwise, so no label leaks) and the upstream
    /// tool/prompt name. Shared by the handoff event and the stream recorder so
    /// the two can never label the same request differently.
    fn usage_identity<'v>(&'v self, value: &'v Value) -> (&'v str, &'v str) {
        let raw = value
            .pointer("/params/name")
            .and_then(Value::as_str)
            .unwrap_or("");
        let (target, tool) = match self.route {
            Route::Aggregate => raw.split_once("__").unwrap_or(("", "")),
            Route::Server { name } => (name, raw),
        };
        let target = if self.grants().may_see_server(target) {
            target
        } else {
            ""
        };
        (target, tool)
    }
    // Error responses are returned directly; boxing would only add an allocation.
    #[allow(clippy::result_large_err)]
    fn session(&self) -> Result<Session, Response<ProxyBody>> {
        if self.headers.get_all("mcp-session-id").iter().count() > 1 {
            return Err(fail(StatusCode::NOT_FOUND, "Ambiguous MCP session"));
        }
        let token = self
            .headers
            .get("mcp-session-id")
            .and_then(|h| h.to_str().ok())
            .ok_or_else(|| fail(StatusCode::BAD_REQUEST, "MCP-Session-Id is required"))?;
        let s: Session = session::open(
            self.runtime.id_key.as_ref().unwrap(),
            session::SESSION,
            token,
        )
        .map_err(|_| fail(StatusCode::NOT_FOUND, "Unknown MCP session"))?;
        if s.owner != self.owner()
            || s.credential != credential(self.headers)
            || s.route != self.route_name()
            || s.issued > session::now()
            || session::now().saturating_sub(s.issued) >= session::TTL
        {
            return Err(fail(
                StatusCode::NOT_FOUND,
                "Expired or foreign MCP session",
            ));
        }
        if self
            .headers
            .get("mcp-protocol-version")
            .is_some_and(|h| h.to_str().ok() != Some(s.version.as_str()))
        {
            return Err(fail(
                StatusCode::BAD_REQUEST,
                "MCP protocol version mismatch",
            ));
        }
        for leg in &s.legs {
            if !self.targets.server(&leg.name).is_some_and(|t| {
                t.id.as_ref() == leg.target
                    && t.url.to_string() == leg.url
                    && target_auth(t) == leg.auth
            }) {
                return Err(fail(
                    StatusCode::NOT_FOUND,
                    "MCP server configuration changed",
                ));
            }
        }
        Ok(s)
    }
    async fn dispatch(&self, method: &Method, mut value: Value) -> Response<ProxyBody> {
        let name = value
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let id = value.get("id").cloned().unwrap_or(Value::Null);
        if *method == Method::POST
            && !name.is_empty()
            && !name.starts_with("notifications/")
            && value.get("id").is_none()
        {
            return fail(StatusCode::BAD_REQUEST, "An RPC request id is required");
        }
        if name == "initialize" {
            if !value
                .pointer("/params/protocolVersion")
                .is_some_and(Value::is_string)
                || !value
                    .pointer("/params/capabilities")
                    .is_some_and(Value::is_object)
                || !value
                    .pointer("/params/clientInfo/name")
                    .is_some_and(Value::is_string)
                || !value
                    .pointer("/params/clientInfo/version")
                    .is_some_and(Value::is_string)
            {
                return rpc(id, -32602, "Invalid initialize parameters");
            }
            return self.initialize(value).await;
        }
        let s = match self.session() {
            Ok(s) => s,
            Err(r) => return r,
        };
        if *method == Method::GET && self.route == Route::Aggregate {
            return transport::empty(StatusCode::METHOD_NOT_ALLOWED);
        }
        if *method == Method::DELETE
            || name == "notifications/initialized"
            || name == "notifications/roots/list_changed"
        {
            let mut status = StatusCode::NO_CONTENT;
            let mut missing_session = false;
            for leg in &s.legs {
                if !self.grants().may_see_server(&leg.name) {
                    continue;
                }
                let target = self.targets.server(&leg.name).unwrap();
                let body = if *method == Method::DELETE {
                    Bytes::new()
                } else {
                    Bytes::from(value.to_string())
                };
                match transport::send(
                    self.runtime,
                    target,
                    self.headers,
                    method.clone(),
                    Some(leg),
                    body,
                )
                .await
                {
                    Ok(r) if r.status() == StatusCode::NOT_FOUND => missing_session = true,
                    Ok(r) if !r.status().is_success() => status = r.status(),
                    Err(error) => status = error.status(),
                    _ => (),
                }
            }
            if missing_session {
                return fail(StatusCode::NOT_FOUND, "Upstream session ended");
            }
            return transport::empty(if *method == Method::POST && status.is_success() {
                StatusCode::ACCEPTED
            } else {
                status
            });
        }
        if name == "ping" {
            return ok(id, json!({}));
        }
        if name == "tools/list" || name == "prompts/list" {
            return self.list(&s, value).await;
        }
        let server = match self.route {
            Route::Server { name } => name.to_owned(),
            Route::Aggregate if name.is_empty() => {
                let Some((server, original)) = decode_id(&value["id"]) else {
                    return fail(StatusCode::BAD_REQUEST, "Malformed upstream response id");
                };
                value["id"] = original;
                server
            }
            Route::Aggregate
                if name == "notifications/cancelled" || name == "notifications/progress" =>
            {
                let field = if name == "notifications/cancelled" {
                    "requestId"
                } else {
                    "progressToken"
                };
                if let Some((server, original)) = decode_id(&value["params"][field]) {
                    value["params"][field] = original;
                    server
                } else if let Some(server) = super::local::lookup(&s.nonce, &value["params"][field])
                {
                    server
                } else {
                    return fail(
                        StatusCode::BAD_REQUEST,
                        "Unknown notification request or progress token",
                    );
                }
            }
            Route::Aggregate => {
                if name != "tools/call" && name != "prompts/get" {
                    return rpc(id, -32601, "Method not found");
                }
                let Some(raw) = value.pointer("/params/name").and_then(Value::as_str) else {
                    return rpc(id, -32602, "Missing name");
                };
                let Some((server, raw)) = raw.split_once("__") else {
                    return rpc(id, -32602, "A server namespace is required");
                };
                let server = server.to_owned();
                let raw = raw.to_owned();
                value["params"]["name"] = raw.into();
                server
            }
        };
        let Some(leg) = s.legs.iter().find(|l| l.name == server) else {
            return fail(StatusCode::NOT_FOUND, "Server is not in this session");
        };
        if !self.grants().may_see_server(&server) {
            return fail(StatusCode::FORBIDDEN, "Server grant required");
        }
        let target = self.targets.server(&server).unwrap();
        if name == "tools/call" || name == "prompts/get" {
            if value.pointer("/params/task").is_some() {
                return rpc(id, -32601, "Tasks are not supported");
            }
            let raw = value["params"]["name"].as_str().unwrap_or("");
            let kind = if name == "tools/call" {
                Kind::Tool
            } else {
                Kind::Prompt
            };
            if !(if kind == Kind::Tool {
                self.grants().may_call_tool(&server, raw)
            } else {
                self.grants().may_see_prompt(&server, raw)
            }) {
                return fail(StatusCode::FORBIDDEN, "Definition grant required");
            }
            let local_tofu = target.review == crate::targets::ReviewMode::Tofu
                && !target.approved().knows(kind, raw)
                && self
                    .runtime
                    .observed
                    .last(&server, kind, raw)
                    .is_some_and(|last| {
                        self.runtime
                            .observed
                            .first(&target.name, &target.id, kind, raw, last)
                            == Some(last)
                    });
            if !local_tofu
                && !self.runtime.observed.may_call(
                    target.review,
                    &server,
                    target.approved(),
                    kind,
                    raw,
                )
            {
                return rpc(id, -32602, "Definition is not approved");
            }
            if name == "tools/call" {
                if let Err(limited) = self.runtime.limiters.charge_request(self.caller) {
                    let mut r = fail(
                        StatusCode::TOO_MANY_REQUESTS,
                        "MCP tool call RPM limit exceeded",
                    );
                    r.headers_mut().insert(
                        "retry-after",
                        limited.retry_after_secs.to_string().parse().unwrap(),
                    );
                    return r;
                }
            }
        } else if *method != Method::GET
            && !matches!(
                name.as_str(),
                "" | "notifications/cancelled" | "notifications/progress"
            )
        {
            return rpc(id, -32601, "Method not found");
        }
        if self.route == Route::Aggregate && (name == "tools/call" || name == "prompts/get") {
            super::local::remember(&s.nonce, &value["id"], &server);
            if let Some(token) = value.pointer("/params/_meta/progressToken") {
                super::local::remember(&s.nonce, token, &server);
            }
        }
        let body = if *method == Method::GET {
            Bytes::new()
        } else {
            Bytes::from(value.to_string())
        };
        match transport::send(
            self.runtime,
            target,
            self.headers,
            method.clone(),
            Some(leg),
            body,
        )
        .await
        {
            Ok(response) => {
                let stream = transport::is_sse(response.headers());
                // The streaming usage record, finished by the relay's pump. The
                // target is already resolved and granted here (the namespace
                // was stripped above), so it is `server`, not a re-split name.
                let usage = (stream
                    && response.status().is_success()
                    && value.get("id").is_some()
                    && !name.is_empty())
                .then(|| {
                    let tool = value
                        .pointer("/params/name")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    crate::usage::TrafficRecorder::new(
                        self.runtime,
                        &crate::usage::TrafficEvent {
                            kind: lumewarp_proto::usage::v1::TrafficKind::Mcp,
                            request: self.request,
                            caller: self.caller,
                            target: &server,
                            method: &name,
                            tool,
                            status: response.status().as_u16(),
                            outcome: lumewarp_proto::usage::v1::RequestOutcome::Completed,
                            stream: true,
                            config_version: self.routing_table.version,
                        },
                    )
                });
                if stream {
                    transport::relay(
                        response,
                        (self.route == Route::Aggregate).then(|| server.to_owned()),
                        usage,
                    )
                } else {
                    relay_json(response, target.timeout).await
                }
            }
            Err(error) => fail(error.status(), "MCP upstream failed"),
        }
    }
    async fn initialize(&self, value: Value) -> Response<ProxyBody> {
        let id = value["id"].clone();
        let requested = value
            .pointer("/params/protocolVersion")
            .and_then(Value::as_str)
            .unwrap_or("");
        let version = if supported(requested) {
            requested
        } else {
            "2025-11-25"
        };
        let mut session = Session {
            owner: self.owner().into(),
            credential: credential(self.headers),
            route: self.route_name().into(),
            issued: session::now(),
            nonce: uuid::Uuid::now_v7().to_string(),
            version: version.into(),
            legs: vec![],
        };
        let selected: Vec<_> = self
            .targets
            .servers()
            .filter(|t| {
                self.grants().may_see_server(&t.name)
                    && (self.route == Route::Aggregate || t.name.as_ref() == self.route_name())
            })
            .take(65)
            .collect();
        let selected = if selected.len() > 64 {
            tracing::warn!("MCP initialize upstream limit reached; extra upstreams excluded");
            &selected[..64]
        } else {
            &selected[..]
        };
        if self.route != Route::Aggregate && selected.is_empty() {
            return fail(StatusCode::NOT_FOUND, "Unknown MCP server");
        }
        // Poll borrowed legs concurrently, preserving deterministic server order.
        let mut futures: Vec<_> = selected
            .iter()
            .map(|t| Some(Box::pin(self.initialize_leg(t, value.clone()))))
            .collect();
        let mut results: Vec<_> = (0..futures.len()).map(|_| None).collect();
        std::future::poll_fn(|cx| {
            let mut pending = false;
            for (index, slot) in futures.iter_mut().enumerate() {
                if let Some(future) = slot {
                    match std::future::Future::poll(future.as_mut(), cx) {
                        std::task::Poll::Ready(result) => {
                            results[index] = Some(result);
                            *slot = None;
                        }
                        std::task::Poll::Pending => pending = true,
                    }
                }
            }
            if pending {
                std::task::Poll::Pending
            } else {
                std::task::Poll::Ready(())
            }
        })
        .await;
        let mut direct = None;
        for result in results.into_iter().flatten() {
            let (leg, result) = match result {
                Ok(result) => result,
                Err(error) if self.route != Route::Aggregate => {
                    return fail(error.status(), "MCP initialization failed");
                }
                Err(_) => continue,
            };
            session.legs.push(leg);
            if session::seal(
                self.runtime.id_key.as_ref().unwrap(),
                session::SESSION,
                &session,
            )
            .is_err()
            {
                session.legs.pop();
                tracing::warn!("MCP session header capacity reached; upstream excluded");
                break;
            }
            direct = Some(result);
        }
        if self.route != Route::Aggregate && session.legs.is_empty() {
            return fail(StatusCode::BAD_GATEWAY, "MCP initialization failed");
        }
        if self.route != Route::Aggregate {
            session.version = session.legs[0].version.clone();
        }
        let token = match session::seal(
            self.runtime.id_key.as_ref().unwrap(),
            session::SESSION,
            &session,
        ) {
            Ok(t) => t,
            Err(()) => {
                return fail(
                    StatusCode::BAD_GATEWAY,
                    "MCP session exceeds header capacity",
                )
            }
        };
        let mut response = if self.route == Route::Aggregate {
            ok(
                id,
                json!({"protocolVersion":version,"capabilities":{"tools":{"listChanged":false},"prompts":{"listChanged":false}},"serverInfo":{"name":"lumewarp","version":env!("CARGO_PKG_VERSION")}}),
            )
        } else {
            transport::reply(StatusCode::OK, direct.unwrap())
        };
        response
            .headers_mut()
            .insert("mcp-session-id", token.parse().unwrap());
        response
    }
    async fn initialize_leg(
        &self,
        target: &McpTarget,
        mut value: Value,
    ) -> Result<(Leg, Value), transport::SendError> {
        if let Some(caps) = value
            .pointer_mut("/params/capabilities")
            .and_then(Value::as_object_mut)
        {
            caps.remove("tasks");
        }
        let response = transport::send(
            self.runtime,
            target,
            self.headers,
            Method::POST,
            None,
            Bytes::from(value.to_string()),
        )
        .await?;
        let session = response
            .headers()
            .get("mcp-session-id")
            .map(|h| h.to_str().map(str::to_owned))
            .transpose()
            .map_err(|_| transport::SendError::Other)?;
        let mut result = tokio::time::timeout(target.timeout, transport::json(response))
            .await
            .map_err(|_| transport::SendError::UpstreamTimeout)?
            .map_err(|_| transport::SendError::Other)?;
        let version = result
            .pointer("/result/protocolVersion")
            .and_then(Value::as_str)
            .filter(|v| supported(v))
            .ok_or(transport::SendError::Other)?
            .to_owned();
        if let Some(caps) = result
            .pointer_mut("/result/capabilities")
            .and_then(Value::as_object_mut)
        {
            for name in ["tasks", "resources", "completions", "logging"] {
                caps.remove(name);
            }
        }
        Ok((
            Leg {
                name: target.name.to_string(),
                target: target.id.to_string(),
                url: target.url.to_string(),
                auth: target_auth(target),
                session,
                version,
            },
            result,
        ))
    }
    async fn list(&self, s: &Session, mut value: Value) -> Response<ProxyBody> {
        let id = value["id"].clone();
        let method = value["method"].as_str().unwrap().to_owned();
        let kind = if method == "tools/list" {
            Kind::Tool
        } else {
            Kind::Prompt
        };
        let field = if kind == Kind::Tool {
            "tools"
        } else {
            "prompts"
        };
        let aggregate = self.route == Route::Aggregate;
        let mut cursor = Cursor {
            session: s.nonce.clone(),
            method: method.clone(),
            index: 0,
            upstream: None,
        };
        if aggregate {
            if let Some(raw) = value.pointer("/params/cursor") {
                cursor = match raw.as_str().and_then(|raw| {
                    session::open::<Cursor>(
                        self.runtime.id_key.as_ref().unwrap(),
                        session::CURSOR,
                        raw,
                    )
                    .ok()
                }) {
                    Some(c)
                        if c.session == s.nonce && c.method == method && c.index < s.legs.len() =>
                    {
                        c
                    }
                    _ => return rpc(id, -32602, "Invalid cursor"),
                };
            }
        }
        let mut last_failure = None;
        while cursor.index < s.legs.len() {
            let leg = &s.legs[cursor.index];
            if !self.grants().may_see_server(&leg.name) {
                cursor.index += 1;
                cursor.upstream = None;
                continue;
            }
            let target = self.targets.server(&leg.name).unwrap();
            if aggregate {
                value["params"] = json!({});
                if let Some(upstream) = &cursor.upstream {
                    value["params"]["cursor"] = upstream.clone().into();
                }
            }
            let page = async {
                let response = transport::send(
                    self.runtime,
                    target,
                    self.headers,
                    Method::POST,
                    Some(leg),
                    Bytes::from(value.to_string()),
                )
                .await
                .map_err(|error| error.status())?;
                if response.status() == StatusCode::NOT_FOUND {
                    return Err(StatusCode::NOT_FOUND);
                }
                let result = tokio::time::timeout(target.timeout, transport::json(response))
                    .await
                    .map_err(|_| StatusCode::GATEWAY_TIMEOUT)?
                    .map_err(|_| StatusCode::BAD_GATEWAY)?;
                if result.get("error").is_some() {
                    return if aggregate {
                        Err(StatusCode::BAD_GATEWAY)
                    } else {
                        Ok(result)
                    };
                }
                if !result
                    .pointer(&format!("/result/{field}"))
                    .is_some_and(Value::is_array)
                {
                    return Err(StatusCode::BAD_GATEWAY);
                }
                if aggregate {
                    if let Some(next) = result.pointer("/result/nextCursor") {
                        let next = next.as_str().ok_or(StatusCode::BAD_GATEWAY)?;
                        if cursor.upstream.as_deref() == Some(next) {
                            return Err(StatusCode::BAD_GATEWAY);
                        }
                    }
                }
                Ok(result)
            }
            .await;
            let mut result = match page {
                Ok(result) => result,
                Err(StatusCode::NOT_FOUND) => {
                    return fail(StatusCode::NOT_FOUND, "Upstream session ended")
                }
                Err(status) if aggregate => {
                    // Only configured server names, fixed method labels and HTTP status
                    // labels enter observability; upstream errors/bodies never do.
                    tracing::warn!(server = %target.name, status = status.as_u16(), "Skipping failed aggregate MCP discovery leg");
                    self.runtime
                        .metrics
                        .mcp_request_handles(&target.name, method_label(&method))
                        [metrics::status_index(status.as_u16())]
                    .inc();
                    last_failure = Some(status);
                    cursor.index += 1;
                    cursor.upstream = None;
                    continue;
                }
                Err(status) => return fail(status, "MCP list failed"),
            };
            if result.get("error").is_some() {
                return transport::reply(StatusCode::OK, result);
            }
            let definitions = result
                .pointer_mut(&format!("/result/{field}"))
                .and_then(Value::as_array_mut)
                .expect("validated list page");
            definitions.retain_mut(|definition| {
                let Some(observed) = mcp_hash::observe(kind, definition) else {
                    return false;
                };
                let mut known = target.approved().knows(kind, &observed.name);
                let mut approved = target
                    .approved()
                    .accepts(kind, &observed.name, &observed.hash);
                // Preserve immediate local TOFU until its authoritative snapshot arrives.
                if !known && target.review == crate::targets::ReviewMode::Tofu {
                    let first = self.runtime.observed.first(
                        &target.name,
                        &target.id,
                        kind,
                        &observed.name,
                        observed.hash,
                    );
                    if first != Some(observed.hash) {
                        known = true;
                        approved = false;
                    }
                }
                let review = catalog::review(target.review, known, approved);
                self.runtime
                    .observed
                    .record(&leg.name, kind, &observed.name, observed.hash);
                if review.report {
                    self.runtime.catalog.observe(&catalog::Observation {
                        server: &leg.name,
                        server_id: &target.id,
                        kind,
                        name: &observed.name,
                        canonical_json: &observed.canonical_json,
                        hash: observed.hash,
                        auto_approved: review.auto_approved,
                        config_version: self.routing_table.version,
                    });
                }
                let granted = if kind == Kind::Tool {
                    self.grants().may_call_tool(&leg.name, &observed.name)
                } else {
                    self.grants().may_see_prompt(&leg.name, &observed.name)
                };
                if !review.serve
                    || !granted
                    || definition
                        .pointer("/execution/taskSupport")
                        .and_then(Value::as_str)
                        == Some("required")
                {
                    return false;
                }
                if aggregate {
                    definition["name"] = format!("{}__{}", leg.name, observed.name).into();
                }
                true
            });
            if aggregate {
                let next = result["result"]
                    .as_object_mut()
                    .unwrap()
                    .remove("nextCursor");
                if let Some(next) = next {
                    let Some(next) = next.as_str() else {
                        return fail(StatusCode::BAD_GATEWAY, "Invalid upstream cursor");
                    };
                    if cursor.upstream.as_deref() == Some(next) {
                        return fail(StatusCode::BAD_GATEWAY, "Upstream cursor loop");
                    }
                    cursor.upstream = Some(next.into());
                } else {
                    cursor.index += 1;
                    cursor.upstream = None;
                }
                if cursor.index < s.legs.len() {
                    let next = match session::seal(
                        self.runtime.id_key.as_ref().unwrap(),
                        session::CURSOR,
                        &cursor,
                    ) {
                        Ok(c) => c,
                        Err(()) => return fail(StatusCode::BAD_GATEWAY, "Cursor exceeds limit"),
                    };
                    result["result"]["nextCursor"] = next.into();
                }
            }
            return transport::reply(StatusCode::OK, result);
        }
        match last_failure {
            Some(status) => fail(status, "No MCP discovery leg could answer"),
            None => ok(id, json!({field:[]})),
        }
    }
}
/// A non-streaming upstream response, forwarded as it is. SSE responses go
/// through `transport::relay`, which records their usage when the stream ends.
async fn relay_json(
    response: hyper::Response<hyper::body::Incoming>,
    timeout: std::time::Duration,
) -> Response<ProxyBody> {
    let (mut parts, body) = response.into_parts();
    transport::response_headers(&mut parts.headers);
    match tokio::time::timeout(timeout, transport::collect(body, transport::RESPONSE_LIMIT)).await {
        Ok(Ok(bytes)) => {
            if serde_json::from_slice::<Value>(&bytes)
                .ok()
                .is_some_and(|v| {
                    v.get("error").is_some()
                        || v.pointer("/result/isError").and_then(Value::as_bool) == Some(true)
                })
            {
                parts
                    .extensions
                    .insert(lumewarp_proto::usage::v1::RequestOutcome::UpstreamError);
            }
            Response::from_parts(parts, crate::proxy::full_body(bytes))
        }
        Ok(Err(())) => fail(
            StatusCode::BAD_GATEWAY,
            "MCP response exceeds 16 MiB or is unreadable",
        ),
        Err(_) => fail(StatusCode::GATEWAY_TIMEOUT, "MCP response body timed out"),
    }
}
fn accepts(headers: &HeaderMap, media: &str) -> bool {
    headers
        .get_all("accept")
        .iter()
        .filter_map(|h| h.to_str().ok())
        .flat_map(|h| h.split(','))
        .any(|entry| {
            let mut parts = entry.split(';');
            parts
                .next()
                .is_some_and(|v| v.trim().eq_ignore_ascii_case(media))
                && !parts.any(|p| {
                    p.trim()
                        .strip_prefix("q=")
                        .is_some_and(|q| q.parse::<f32>().map_or(true, |v| v <= 0.0))
                })
        })
}
fn credential(headers: &HeaderMap) -> String {
    hex::encode(Sha256::digest(
        headers
            .get("authorization")
            .map_or(&[][..], |h| h.as_bytes()),
    ))
}
fn target_auth(target: &McpTarget) -> String {
    let mut hash = Sha256::new();
    hash.update(target.auth.plugin_name().as_bytes());
    if let Some(authorization) = target.auth.authorization() {
        hash.update(authorization.as_bytes());
    }
    if let Some(plugin) = target.auth.plugin() {
        hash.update(plugin.digest);
    }
    hex::encode(hash.finalize())
}
fn supported(v: &str) -> bool {
    matches!(v, "2025-11-25" | "2025-06-18")
}
fn decode_id(value: &Value) -> Option<(String, Value)> {
    let (server, raw) = value.as_str()?.strip_prefix("lw:")?.split_once(':')?;
    if server.is_empty() || server.len() > 63 || server.contains("__") {
        return None;
    }
    let id: Value = serde_json::from_str(raw).ok()?;
    (id.is_string() || id.is_i64() || id.is_u64()).then(|| (server.into(), id))
}
fn fail(status: StatusCode, message: &str) -> Response<ProxyBody> {
    error::json(status, "mcp_error", message)
}
fn ok(id: Value, result: Value) -> Response<ProxyBody> {
    transport::reply(
        StatusCode::OK,
        json!({"jsonrpc":"2.0","id":id,"result":result}),
    )
}
fn rpc(id: Value, code: i32, message: &str) -> Response<ProxyBody> {
    let mut response = transport::reply(
        StatusCode::OK,
        json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}}),
    );
    response
        .extensions_mut()
        .insert(lumewarp_proto::usage::v1::RequestOutcome::Rejected);
    response
}
fn method_label(method: &str) -> &'static str {
    match method {
        "initialize" => "initialize",
        "tools/list" => "tools/list",
        "tools/call" => "tools/call",
        "prompts/list" => "prompts/list",
        "prompts/get" => "prompts/get",
        "ping" => "ping",
        "notifications/initialized" => "notifications/initialized",
        _ => "other",
    }
}

//! The A2A endpoints: `/a2a`, `/a2a/{agent}` and
//! `/a2a/{agent}/.well-known/agent-card.json` (`docs/plugins.md`, A2A v1.0
//! JSON-RPC with SSE).
//!
//! `server.rs` owns everything before this module: the route match, the
//! request id, the OTel span, the snapshot lookup and `X-Lumewarp-Key`
//! authentication (`auth::authenticate_mcp`, L8). This module is the proxy:
//!
//! - [`handle`] answers `GET /a2a` (the agents the key may call) and the card
//!   route itself, and hands JSON-RPC requests to [`rpc`];
//! - [`ids`] wraps and unwraps task/context ids for the caller's key (D17);
//! - [`card`] fetches, caches and rewrites Agent Cards (P7, L6);
//! - [`rpc`] implements the pipeline of plan L5, the A2A-Version check, the
//!   push-notification refusal (D12) and streaming (L10).
//!
//! The endpoint needs `LUMEWARP_GATEWAY_ID_KEY` and `LUMEWARP_GATEWAY_PUBLIC_URL`
//! (plan P3): without either, every route answers 503 `a2a_not_configured` and
//! a metric is counted when the route names an agent of the snapshot. LLM and
//! MCP traffic are unaffected.

mod card;
mod ids;
mod rpc;

use std::time::Duration;

use bytes::Bytes;
use hyper::{body::Incoming, header, Method, Request, Response, StatusCode};
use lumewarp_plugin_host::a2a_middleware;
use serde_json::Value;

use crate::{
    auth, error, metrics::status_index, proxy::ProxyBody, router::RoutingTable, runtime::Runtime,
    targets, usage::RequestCtx,
};

/// Which A2A endpoint a request named.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route<'a> {
    /// `GET /a2a`: the agents this key may call, as a JSON array.
    List,
    /// `/a2a/{agent}`: that agent's JSON-RPC endpoint (and SSE streams).
    Rpc {
        /// `A2aAgent.name`; existence and grants are the proxy's to decide.
        agent: &'a str,
    },
    /// `/a2a/{agent}/.well-known/agent-card.json`: the rewritten Agent Card
    /// (L6).
    Card { agent: &'a str },
}

impl Route<'_> {
    fn agent(&self) -> Option<&str> {
        match self {
            Self::List => None,
            Self::Rpc { agent } | Self::Card { agent } => Some(agent),
        }
    }
}

/// One A2A request, authenticated and routed.
pub struct Call<'a> {
    pub route: Route<'a>,
    /// Same as `http.method()`, owned so the proxy can match on it after
    /// consuming `http`.
    pub method: Method,
    /// The request body and headers as sent. `X-Lumewarp-Key` is still
    /// present and must never be forwarded upstream (L9).
    pub http: Request<Incoming>,
    /// Always `Caller::Key`: an A2A request is never anonymous (L8).
    pub caller: &'a auth::Caller,
    /// The routing table this request started with (a swap does not affect it).
    pub routing_table: &'a RoutingTable,
    /// `routing_table.targets`.
    pub targets: &'a targets::Targets,
    pub runtime: &'a Runtime,
    /// The request's id (also echoed in `x-lumewarp-request-id`) and start.
    pub request: &'a RequestCtx,
    /// How long reading the request body may take.
    pub body_read_timeout: Duration,
}

impl Call<'_> {
    /// The authenticated key. `server.rs` never passes another caller.
    fn key(&self) -> &crate::router::KeyContext {
        match self.caller {
            auth::Caller::Key(key) => key,
            auth::Caller::Anonymous => unreachable!("A2A requests are authenticated"),
        }
    }

    fn public_url(&self) -> &str {
        self.runtime
            .public_url
            .as_deref()
            .expect("checked by handle")
    }

    /// Records one `lumewarp_a2a_requests_total` sample. `agent` must be a
    /// snapshot name, never client input.
    fn record(&self, agent: &str, method: &'static str, status: u16) {
        let handles = self.runtime.metrics.a2a_request_handles(agent, method);
        handles[status_index(status)].inc();
    }
}

/// Serves one A2A request.
pub async fn handle(call: Call<'_>) -> Response<ProxyBody> {
    // P3: ids are wrapped with `LUMEWARP_GATEWAY_ID_KEY` and cards point at
    // `LUMEWARP_GATEWAY_PUBLIC_URL`; without either the endpoint cannot work at all, so
    // it says so instead of forwarding unwrapped ids (LLM and MCP are
    // unaffected).
    if call.runtime.id_key.is_none() || call.runtime.public_url.is_none() {
        if let Some(agent) = call.route.agent() {
            if call.targets.agent(agent).is_some() {
                let method = if matches!(call.route, Route::Card { .. }) {
                    "agent_card"
                } else {
                    "other"
                };
                call.record(agent, method, 503);
            }
        }
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "a2a_not_configured",
            "A2A needs LUMEWARP_GATEWAY_ID_KEY and LUMEWARP_GATEWAY_PUBLIC_URL",
        );
    }
    match call.route {
        Route::List => list(call).await,
        Route::Card { agent } => serve_card(call, agent).await,
        Route::Rpc { agent } => rpc::serve(call, agent).await,
    }
}

/// Startup-owned card fetching: spawned once by `server::serve` so an agent's
/// card exists (and is reported through `runtime.catalog.card`) before any
/// client request. A request never starts or flushes a fetch.
pub(crate) async fn supervise_cards(
    runtime: std::sync::Arc<Runtime>,
    snapshot: crate::server::Snapshot,
) {
    card::supervise(runtime, snapshot).await;
}

/// `GET /a2a`: the agents this key may call, with the card URL a client should
/// fetch. The list is an array so a client can iterate it without a schema.
async fn list(call: Call<'_>) -> Response<ProxyBody> {
    if call.method != Method::GET {
        return method_not_allowed("GET");
    }
    let key = call.key();
    let public_url = call.public_url();
    let mut agents = Vec::new();
    for target in call.targets.agents() {
        if !key.grants.may_call_agent(&target.name) {
            continue;
        }
        agents.push(serde_json::json!({
            "name": target.name.as_ref(),
            "cardUrl": format!(
                "{public_url}/a2a/{}/{}",
                target.name,
                targets::WELL_KNOWN_CARD_PATH.trim_start_matches('/')
            ),
        }));
    }
    json_response(StatusCode::OK, &Value::Array(agents))
}

/// The rewritten Agent Card: the last card the gateway fetched, else the
/// snapshot's remembered `card_json`, else 404.
async fn serve_card(call: Call<'_>, agent: &str) -> Response<ProxyBody> {
    if call.method != Method::GET {
        return method_not_allowed("GET");
    }
    let key = call.key();
    let Some(target) = call.targets.agent(agent) else {
        return card_not_found();
    };
    if !key.grants.may_call_agent(&target.name) {
        // Do not reveal whether an agent the key cannot reach exists.
        return card_not_found();
    }
    let source = card::cached(&target.name, &target.id)
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .or_else(|| {
            target
                .card_json
                .as_deref()
                .and_then(|text| serde_json::from_str::<Value>(text).ok())
        });
    let Some(source) = source else {
        call.record(agent, "agent_card", 404);
        return card_not_found();
    };
    let Some(rendered) = card::render(&source, agent, call.public_url()) else {
        call.record(agent, "agent_card", 502);
        return error::json(
            StatusCode::BAD_GATEWAY,
            "invalid_agent_card",
            "The agent card is not a JSON object",
        );
    };
    let response = card::response(&rendered, call.http.headers());
    call.record(agent, "agent_card", response.status().as_u16());
    response
}

fn card_not_found() -> Response<ProxyBody> {
    error::json(
        StatusCode::NOT_FOUND,
        "agent_card_not_found",
        "No agent card",
    )
}

fn method_not_allowed(allow: &'static str) -> Response<ProxyBody> {
    let mut response = error::json(
        StatusCode::METHOD_NOT_ALLOWED,
        "method_not_allowed",
        "Method not allowed",
    );
    response
        .headers_mut()
        .insert(header::ALLOW, header::HeaderValue::from_static(allow));
    response
}

/// A plain JSON response (the list endpoint); JSON-RPC errors are built in
/// [`rpc`].
fn json_response(status: StatusCode, value: &Value) -> Response<ProxyBody> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(crate::proxy::full_body(Bytes::from(
            serde_json::to_vec(value).expect("JSON values serialize"),
        )))
        .expect("static response parts are valid")
}

/// The apply-path hook for the A2A middleware: register and configure every
/// agent's plugin, reusing the ones whose name, settings and destination did
/// not change. Call it from `RoutingTable::compile` next to
/// `upstream_auth::precompile`; a refused `configure` becomes an
/// `INVALID_CONFIG` NACK instead of a 502 on the first request.
#[allow(dead_code)] // wired by the lead in `RoutingTable::compile`, next to auth's hook
pub fn precompile(targets: &targets::Targets) -> Result<(), crate::snapshot::ApplyError> {
    let specs: Vec<a2a_middleware::Spec> = targets
        .agents()
        .filter_map(|agent| {
            let plugin = agent.middleware.as_ref()?;
            // A plugin this gateway did not load is refused by the capability
            // check in `snapshot::validate` (`UNSUPPORTED_FEATURE`); skip it
            // here rather than reporting a second, different error.
            lumewarp_plugin_host::global().get(a2a_middleware::KIND, &plugin.name)?;
            Some(a2a_middleware::Spec {
                id: agent.id.to_string(),
                name: agent.name.to_string(),
                url: agent.url.to_string(),
                plugin: plugin.name.to_string(),
                params_json: plugin.params_json.to_string(),
                digest: plugin.digest,
            })
        })
        .collect();
    a2a_middleware::precompile(&specs).map_err(|refused| {
        crate::snapshot::ApplyError::invalid(format!(
            "a2a agent {:?}: middleware plugin could not be configured",
            refused.agent
        ))
    })
}

/// One usage event per A2A RPC (plan P10, D16). `method` is the v1.0 method
/// name; `requested` is empty for non-LLM traffic and `target` is the agent.
#[allow(clippy::too_many_arguments)]
pub(crate) fn record_usage(
    runtime: &Runtime,
    routing_table: &RoutingTable,
    request: &RequestCtx,
    key: &crate::router::KeyContext,
    agent: &str,
    method: &str,
    status: u16,
    stream: bool,
    outcome: lumewarp_proto::usage::v1::RequestOutcome,
) {
    if !runtime.usage.enabled() {
        return;
    }
    use lumewarp_proto::usage::v1::{TrafficKind, UsageEvent, UsageState};
    let elapsed = request.started.elapsed();
    runtime.usage.try_record(UsageEvent {
        request_id: request.request_id.to_string(),
        timestamp_unix_ms: request
            .started_at_unix_ms
            .saturating_add(i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX)),
        key_id: key.key_id.to_string(),
        team_id: key.team_id.to_string(),
        kind: TrafficKind::A2a as i32,
        target: agent.to_owned(),
        method: method.to_owned(),
        status: status.into(),
        stream,
        duration_ms: u32::try_from(elapsed.as_millis()).unwrap_or(u32::MAX),
        config_version: routing_table.version,
        usage_state: UsageState::Missing as i32,
        outcome: outcome as i32,
        started_at_unix_ms: request.started_at_unix_ms,
        ..Default::default()
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_name_their_agent() {
        assert_eq!(Route::List.agent(), None);
        assert_eq!(Route::Rpc { agent: "a" }.agent(), Some("a"));
        assert_eq!(Route::Card { agent: "b" }.agent(), Some("b"));
    }

    #[test]
    fn the_card_url_is_under_the_public_prefix() {
        // The list builds the same shape; keep the two in sync by construction.
        let url = format!(
            "{}/a2a/{}/{}",
            "https://gw.example.test/lumewarp",
            "helper",
            targets::WELL_KNOWN_CARD_PATH.trim_start_matches('/')
        );
        assert_eq!(
            url,
            "https://gw.example.test/lumewarp/a2a/helper/.well-known/agent-card.json"
        );
    }
}

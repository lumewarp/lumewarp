//! The MCP endpoints: `/mcp` and `/mcp/servers/{name}` (`docs/plugins.md`,
//! MCP revision 2025-11-25, Streamable HTTP).
//!
//! `server.rs` owns everything before this module: the route match, the
//! request id, the OTel span, the snapshot lookup and `X-Lumewarp-Key`
//! authentication (`auth::authenticate_mcp`, L8). MCP requires a configured
//! `LUMEWARP_GATEWAY_ID_KEY`: without it all MCP routes answer 503, unlike
//! the original P3 which restricted that requirement to A2A.
//!
//! What the proxy gets:
//! - [`Route`]: the aggregated virtual server or one named server, and with it
//!   the upstream name to prefix (`targets::TOOL_SEPARATOR`) or none at all;
//! - `caller`: an authenticated `Caller::Key` whose `KeyContext` holds
//!   `grants` (`grants::Grants`), the call-gate scope, and `scopes` for D20;
//! - `targets`: the snapshot's MCP servers (`targets::Targets::server`,
//!   `servers`, each `McpTarget` with `url`, `auth`, `timeout`, `review` and
//!   `approved()`) and, for the shared machinery, the A2A agents;
//! - `runtime`: the process-lifetime state — `catalog` (report observations),
//!   `observed` (the L1 call gate), `limiters`
//!   (`charge_request` for `tools/call`, D20), `client` (the outbound HTTP
//!   client) and `id_key` (MCP session sealing);
//! - `request` / `request_id`: the `x-lumewarp-request-id` of this request, for
//!   usage events and spans;
//! - `http`: the untouched request, whose body the proxy reads with
//!   `body_read_timeout`.
//!
//! The body cap (4 MiB for a request, 16 MiB for a non-streaming response,
//! 1 MiB per SSE event, L10) is the proxy's to enforce; nothing has read the
//! body yet when [`handle`] runs.

mod engine;
mod local;
mod session;
mod sse;
mod transport;
pub(crate) use local::First;

use std::time::Duration;

use hyper::{body::Incoming, Method, Request, Response};

use crate::{
    auth, proxy::ProxyBody, router::RoutingTable, runtime::Runtime, targets, usage::RequestCtx,
};

/// Which MCP endpoint a request named.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route<'a> {
    /// `/mcp`: the key's aggregated virtual server. Tool and prompt names are
    /// `<server>__<upstream name>`, split at the first `__` (L4); servers are
    /// filtered by the key's grants.
    Aggregate,
    /// `/mcp/servers/{name}`: exactly one server, upstream names unprefixed.
    Server {
        /// `McpServer.name`; it may or may not exist in `targets`, and the key
        /// may or may not be granted it — deciding that (and not leaking
        /// existence) is the proxy's job.
        name: &'a str,
    },
}

/// One MCP request, authenticated and routed.
pub struct Call<'a> {
    pub route: Route<'a>,
    /// Same as `http.method()`, owned so the proxy can match on it after
    /// consuming `http`.
    pub method: Method,
    /// The request body and headers as sent. `X-Lumewarp-Key` is still
    /// present and must never be forwarded upstream (L9).
    pub http: Request<Incoming>,
    /// Always `Caller::Key`: an MCP request is never anonymous (L8).
    pub caller: &'a auth::Caller,
    /// The routing table this request started with (a swap does not affect it).
    pub routing_table: &'a RoutingTable,
    /// `routing_table.targets`.
    pub targets: &'a targets::Targets,
    /// Process-lifetime state: the catalog registry, the observed-definition
    /// memory (L1), the limiter registry (D20) and the HTTP client.
    pub runtime: &'a Runtime,
    /// The request's id (also echoed in `x-lumewarp-request-id`) and start.
    pub request: &'a RequestCtx,
    /// How long reading the request body may take.
    pub body_read_timeout: Duration,
}

/// Serve a Streamable HTTP request using the immutable routing snapshot.
pub async fn handle(call: Call<'_>) -> Response<ProxyBody> {
    let runtime = call.runtime;
    let server = match call.route {
        Route::Aggregate => "_aggregate",
        Route::Server { name } if call.targets.server(name).is_some() => name,
        _ => "_unknown",
    };
    let response = engine::handle(call).await;
    if response.extensions().get::<engine::Counted>().is_none() {
        runtime.metrics.mcp_request_handles(server, "other")
            [crate::metrics::status_index(response.status().as_u16())]
        .inc();
    }
    response
}

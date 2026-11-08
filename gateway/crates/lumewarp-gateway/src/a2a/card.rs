//! Agent Card fetching and rewriting (plan P7, D13, L6).
//!
//! The control plane cannot fetch a card for an agent that uses a WASM
//! outbound-auth plugin or a client's `passthrough` bearer, so the gateway
//! fetches each agent's card itself, every five minutes, with that agent's
//! outbound auth. The last successful card is served (and reported through
//! `CatalogReport`); a failed fetch keeps the previous card and reports the
//! error. If no fetch has succeeded, the snapshot's `card_json` (pasted by an
//! administrator or reported earlier) is used; with neither, the card route is
//! a 404.
//!
//! Every card — served or fetched, public or `GetExtendedAgentCard` — goes
//! through the same [`rewrite`]:
//!
//! - one `JSONRPC`/`1.0` interface pointing at
//!   `LUMEWARP_GATEWAY_PUBLIC_URL` plus `/a2a/<agent>`, never at the upstream
//!   URL or the `Host` header;
//! - `securitySchemes`/`securityRequirements` describe `X-Lumewarp-Key`
//!   instead of the upstream's schemes, including on every skill that declared
//!   its own requirements;
//! - `signatures` are removed (the gateway does not sign, D13);
//! - `capabilities.pushNotifications` is false (D12).
//!
//! Fetching is **startup-owned** (I6): `server::serve` spawns [`supervise`],
//! which watches the snapshot and reconciles one fetch task per agent (start,
//! stop on removal, restart when the URL/card URL/auth/timeout changes). It
//! runs in both standalone and control-plane mode, so an agent's card exists
//! before the first client request and the control plane receives it through
//! `runtime.catalog.card` without any A2A traffic. A request never starts or
//! flushes a fetch.

use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{
    header::{HeaderMap, HeaderValue, ACCEPT, CONTENT_TYPE, ETAG, IF_NONE_MATCH},
    Method, Request, Response, StatusCode, Uri,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::{
    catalog::{CardObservation, Registry, MAX_ERROR_BYTES},
    proxy::{full_body, ProxyBody, UpstreamClient},
    runtime::Runtime,
    targets::{self, A2aTarget},
    upstream_auth,
};

/// How often a card is fetched again (plan P7).
pub const REFRESH: Duration = Duration::from_secs(5 * 60);
/// Largest card accepted from upstream or the snapshot.
pub const MAX_CARD_BYTES: usize = targets::MAX_CARD_BYTES;
/// Most agents whose cards are fetched by one process.
pub const MAX_AGENTS: usize = 200;
/// The card's `Cache-Control` lifetime; the gateway itself refetches every
/// [`REFRESH`]. `private`: the response is grant-gated by `X-Lumewarp-Key`, so a
/// shared cache must not serve it to another key (m6).
const CARD_MAX_AGE: &str = "private, max-age=300";

/// The rewritten card bytes and its validator.
pub struct Card {
    pub body: Arc<str>,
    pub etag: String,
}

/// Rewrites one Agent Card in place for the gateway (plan L6). Returns false
/// when the value is not a JSON object.
pub fn rewrite(card: &mut Value, agent: &str, public_url: &str) -> bool {
    let Some(object) = card.as_object_mut() else {
        return false;
    };
    // Keep the selected interface's tenant: every request must echo it and the
    // agent's configured URL is the one this interface routes to.
    let tenant = object
        .get("supportedInterfaces")
        .and_then(Value::as_array)
        .and_then(|interfaces| {
            interfaces
                .iter()
                .find(|interface| {
                    interface.get("protocolBinding").and_then(Value::as_str) == Some("JSONRPC")
                        && interface
                            .get("protocolVersion")
                            .and_then(Value::as_str)
                            .is_some_and(|version| version == "1.0")
                })
                .and_then(|interface| interface.get("tenant").cloned())
        });
    let mut interface = json!({
        "url": format!("{public_url}/a2a/{agent}"),
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
    });
    if let Some(tenant) = tenant {
        interface["tenant"] = tenant;
    }
    object.insert("supportedInterfaces".into(), Value::Array(vec![interface]));
    object.insert(
        "securitySchemes".into(),
        json!({
            "lumewarp": {
                "apiKeySecurityScheme": {
                    "description": "Lumewarp API key, sent as the X-Lumewarp-Key header.",
                    "location": "header",
                    "name": "X-Lumewarp-Key",
                },
            },
        }),
    );
    object.insert(
        "securityRequirements".into(),
        json!([{"schemes": {"lumewarp": {"list": []}}}]),
    );
    if let Some(skills) = object.get_mut("skills").and_then(Value::as_array_mut) {
        for skill in skills {
            if let Some(skill) = skill.as_object_mut() {
                if skill.contains_key("securityRequirements") {
                    skill.insert(
                        "securityRequirements".into(),
                        json!([{"schemes": {"lumewarp": {"list": []}}}]),
                    );
                }
            }
        }
    }
    object.remove("signatures");
    let capabilities = object
        .entry("capabilities")
        .or_insert_with(|| Value::Object(Map::new()));
    if let Some(capabilities) = capabilities.as_object_mut() {
        capabilities.insert("pushNotifications".into(), Value::Bool(false));
    }
    true
}

/// A rewritten card for a client, with an ETag over the bytes actually sent.
pub fn render(card: &Value, agent: &str, public_url: &str) -> Option<Card> {
    let mut card = card.clone();
    if !rewrite(&mut card, agent, public_url) {
        return None;
    }
    let body: Arc<str> = Arc::from(serde_json::to_string(&card).ok()?);
    let etag = format!("\"{}\"", hex::encode(Sha256::digest(body.as_bytes())));
    Some(Card { body, etag })
}

/// The card response: 200 with the rewritten card, or 304 when the client's
/// `If-None-Match` already has it.
pub fn response(card: &Card, request_headers: &HeaderMap) -> Response<ProxyBody> {
    if request_headers
        .get(IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.split(',').any(|tag| tag.trim() == card.etag))
    {
        let mut response = Response::new(full_body(Bytes::new()));
        *response.status_mut() = StatusCode::NOT_MODIFIED;
        response
            .headers_mut()
            .insert(ETAG, HeaderValue::from_str(&card.etag).expect("hex etag"));
        return response;
    }
    let mut response = Response::new(full_body(Bytes::from(card.body.as_bytes().to_vec())));
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static(CARD_MAX_AGE));
    response
        .headers_mut()
        .insert(ETAG, HeaderValue::from_str(&card.etag).expect("hex etag"));
    response
}

// ---------------------------------------------------------------------------
// Startup supervisor, fetch tasks and the last-good card
// ---------------------------------------------------------------------------

/// How often the supervisor compares the running tasks with the snapshot. A
/// snapshot change is picked up within this interval; the interval is cheap
/// (one Arc load and a walk over the agents).
const RECONCILE: Duration = Duration::from_secs(1);

/// One agent's last good card.
struct Fetched {
    /// The agent id this card belongs to; a renamed/recreated agent is a new
    /// target and must never inherit the old card.
    id: String,
    card: Option<Arc<str>>,
}

struct Store {
    cards: Mutex<HashMap<String, Fetched>>,
}

static STORE: OnceLock<Store> = OnceLock::new();

fn store() -> &'static Store {
    STORE.get_or_init(|| Store {
        cards: Mutex::new(HashMap::new()),
    })
}

fn lock() -> std::sync::MutexGuard<'static, HashMap<String, Fetched>> {
    store()
        .cards
        .lock()
        .unwrap_or_else(|error| error.into_inner())
}

/// The last card fetched for `agent`, if it belongs to the target with `id`.
pub fn cached(agent: &str, id: &str) -> Option<Arc<str>> {
    let cards = lock();
    cards
        .get(agent)
        .filter(|fetched| fetched.id == id)
        .and_then(|fetched| fetched.card.clone())
}

/// What a running fetch task was configured for. A change restarts the task so
/// it never keeps fetching from the old destination or with an old credential.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Signature {
    url: String,
    card_url: String,
    auth: String,
    timeout: Duration,
}

fn signature(target: &A2aTarget) -> Signature {
    use crate::targets::Auth;
    let auth = match &target.auth {
        Auth::None => "none".to_owned(),
        Auth::StaticBearer { authorization } => format!(
            "static_bearer:{}",
            hex::encode(Sha256::digest(authorization.as_bytes()))
        ),
        Auth::Passthrough => "passthrough".to_owned(),
        Auth::Plugin(plugin) => {
            format!("{}:{}", plugin.name, hex::encode(plugin.digest))
        }
    };
    Signature {
        url: target.url.to_string(),
        card_url: target.card_url.to_string(),
        auth,
        timeout: target.timeout,
    }
}

/// The fetch tasks of one supervisor. Dropping the supervisor (shutdown or a
/// panic that unwinds) aborts every child.
#[derive(Default)]
struct Tasks {
    running: HashMap<(String, String), (Signature, tokio::task::JoinHandle<()>)>,
}

impl Drop for Tasks {
    fn drop(&mut self) {
        for (_, (_, task)) in self.running.drain() {
            task.abort();
        }
    }
}

/// Startup-owned card fetching. Spawned once by `server::serve`; it follows the
/// snapshot for the life of the process and never runs because of a request.
pub async fn supervise(runtime: Arc<Runtime>, snapshot: crate::server::Snapshot) {
    let mut tasks = Tasks::default();
    loop {
        reconcile(&runtime, &snapshot, &mut tasks);
        tokio::time::sleep(RECONCILE).await;
    }
}

fn reconcile(runtime: &Arc<Runtime>, snapshot: &crate::server::Snapshot, tasks: &mut Tasks) {
    let table = snapshot.load_full();
    let agents: Vec<Arc<A2aTarget>> = match &table {
        Some(table) => table.targets.agents().take(MAX_AGENTS).cloned().collect(),
        None => Vec::new(),
    };
    let mut desired = HashSet::with_capacity(agents.len());
    for target in agents {
        let key = (target.name.to_string(), target.id.to_string());
        desired.insert(key.clone());
        let signature = signature(&target);
        if tasks
            .running
            .get(&key)
            .is_some_and(|(current, task)| *current == signature && !task.is_finished())
        {
            continue;
        }
        if let Some((_, task)) = tasks.running.remove(&key) {
            task.abort();
        }
        let task = tokio::spawn(fetch_loop(
            Arc::clone(runtime),
            target,
            Arc::clone(snapshot),
        ));
        tasks.running.insert(key, (signature, task));
    }
    // Stop tasks for removed agents. Their cached card may stay until the
    // bounded map evicts it; `serve_card` only serves a card for a target that
    // is still in the snapshot, so a removed agent can never be served again.
    tasks.running.retain(|key, (_, task)| {
        if desired.contains(key) {
            true
        } else {
            task.abort();
            false
        }
    });
}

/// The version of the snapshot the process is serving right now. A card task
/// lives across snapshots, so every report must carry the current version, not
/// the one it was spawned with (M7); the control plane discards a card whose
/// version is older than the target's last edit.
fn snapshot_version(snapshot: &crate::server::Snapshot) -> u64 {
    snapshot.load().as_ref().map_or(0, |table| table.version)
}

async fn fetch_loop(
    runtime: Arc<Runtime>,
    target: Arc<A2aTarget>,
    snapshot: crate::server::Snapshot,
) {
    // Spread the herd: a snapshot reaches every replica at once.
    tokio::time::sleep(Duration::from_millis(fastrand::u64(0..2_000))).await;
    loop {
        let started = Instant::now();
        let span = tracing::info_span!("a2a_upstream", agent = %target.name, kind = "agent_card");
        let result = tracing::Instrument::instrument(fetch(&target, &runtime.client), span).await;
        runtime
            .metrics
            .a2a_upstream_duration(&target.name)
            .observe(started.elapsed().as_secs_f64());
        let public_url = runtime.public_url.as_deref();
        let version = snapshot_version(&snapshot);
        match result {
            Ok(card) => report_success(&runtime.catalog, &target, card, public_url, version),
            Err(error) => report_error(&runtime.catalog, &target, &error, public_url, version),
        }
        // Five minutes, +/- 10%: a whole fleet does not refetch in lockstep.
        let jitter = REFRESH / 10;
        let spread = fastrand::u64(0..=jitter.as_millis() as u64 * 2);
        let interval = REFRESH
            .saturating_sub(jitter)
            .saturating_add(Duration::from_millis(spread));
        tokio::time::sleep(interval).await;
    }
}

/// Whether two URLs share scheme, host and effective port. A token for one
/// destination must not be sent to another (`auth.rs`), so the card fetch
/// attaches the agent's credential only on its own origin.
fn same_origin(left: &Uri, right: &Uri) -> bool {
    fn effective_port(uri: &Uri) -> Option<u16> {
        uri.port_u16().or_else(|| match uri.scheme_str() {
            Some("http") => Some(80),
            Some("https") => Some(443),
            _ => None,
        })
    }
    left.scheme_str() == right.scheme_str()
        && left
            .host()
            .zip(right.host())
            .is_some_and(|(left, right)| left.eq_ignore_ascii_case(right))
        && effective_port(left) == effective_port(right)
}

async fn fetch(target: &A2aTarget, client: &UpstreamClient) -> Result<String, String> {
    let mut headers = HeaderMap::new();
    headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
    // The agent's own outbound auth, exactly like a JSON-RPC call (P7), but only
    // when the card is on the agent's origin. A `card_url` on another origin
    // gets no credential (R6 B1; docs/plugins.md, "Agent cards").
    if same_origin(&target.url, &target.card_url) {
        upstream_auth::authorize_a2a(target, &target.card_url, &Method::GET, &[], &mut headers)
            .await
            .map_err(|failure| format!("outbound auth failed: {}", failure.label()))?;
    }
    let mut request = Request::builder()
        .method(Method::GET)
        .uri(target.card_url.clone())
        .body(Full::new(Bytes::new()))
        .map_err(|_| "invalid card request".to_string())?;
    // The review found this line missing: `headers` (Accept and the outbound
    // auth) was computed but the request never carried it.
    *request.headers_mut() = headers;
    let response = tokio::time::timeout(target.timeout, client.request(request))
        .await
        .map_err(|_| "card fetch timed out".to_string())?
        .map_err(|_| "card fetch failed".to_string())?;
    let status = response.status();
    if status != StatusCode::OK {
        return Err(format!("card fetch returned status {}", status.as_u16()));
    }
    let body = tokio::time::timeout(
        target.timeout,
        Limited::new(response.into_body(), MAX_CARD_BYTES).collect(),
    )
    .await
    .map_err(|_| "card fetch timed out".to_string())?
    .map_err(|error| {
        if error.is::<http_body_util::LengthLimitError>() {
            format!("card exceeds {MAX_CARD_BYTES} bytes")
        } else {
            "could not read the card body".to_string()
        }
    })?
    .to_bytes();
    let text = std::str::from_utf8(&body).map_err(|_| "card is not UTF-8".to_string())?;
    let card: Value =
        serde_json::from_str(text).map_err(|_| "card is not valid JSON".to_string())?;
    if !card.is_object() {
        return Err("card is not a JSON object".to_string());
    }
    Ok(text.to_owned())
}

fn truncate_error(error: &str) -> Arc<str> {
    let mut end = error.len().min(MAX_ERROR_BYTES);
    while !error.is_char_boundary(end) {
        end -= 1;
    }
    Arc::from(&error[..end])
}

/// Remembers the last good card, bounded at [`MAX_AGENTS`].
fn remember(target: &A2aTarget, card: Arc<str>) {
    let mut cards = lock();
    if cards.len() >= MAX_AGENTS && !cards.contains_key(&*target.name) {
        if let Some(stale) = cards.keys().next().cloned() {
            cards.remove(&stale);
        }
    }
    cards.insert(
        target.name.to_string(),
        Fetched {
            id: target.id.to_string(),
            card: Some(card),
        },
    );
}

/// The last good card for the error report, if it belongs to this target.
fn last_good(target: &A2aTarget) -> Option<Arc<str>> {
    lock()
        .get(&*target.name)
        .filter(|fetched| fetched.id == *target.id)
        .and_then(|fetched| fetched.card.clone())
}

/// The card as the control plane should store and display it: the rewrite clients
/// receive (gateway URL, gateway auth scheme, no signatures, push off). The raw
/// upstream bytes stay in the process for serving; without a public URL there is
/// no rewrite and the raw card is reported.
fn reported_card(target: &A2aTarget, raw: &str, public_url: Option<&str>) -> String {
    let (Some(public_url), Ok(mut card)) = (public_url, serde_json::from_str::<Value>(raw)) else {
        return raw.to_owned();
    };
    rewrite(&mut card, &target.name, public_url);
    serde_json::to_string(&card).unwrap_or_else(|_| raw.to_owned())
}

fn report_success(
    catalog: &Registry,
    target: &A2aTarget,
    card: String,
    public_url: Option<&str>,
    config_version: u64,
) {
    let reported = reported_card(target, &card, public_url);
    remember(target, Arc::from(card));
    catalog.card(&CardObservation {
        agent: &target.name,
        agent_id: &target.id,
        card_json: &reported,
        error: "",
        config_version,
    });
}

fn report_error(
    catalog: &Registry,
    target: &A2aTarget,
    error: &str,
    public_url: Option<&str>,
    config_version: u64,
) {
    let error = truncate_error(error);
    // A failed fetch keeps the last good card and reports both.
    let card = last_good(target);
    {
        let mut cards = lock();
        if cards.len() >= MAX_AGENTS && !cards.contains_key(&*target.name) {
            if let Some(stale) = cards.keys().next().cloned() {
                cards.remove(&stale);
            }
        }
        cards.insert(
            target.name.to_string(),
            Fetched {
                id: target.id.to_string(),
                card: card.clone(),
            },
        );
    }
    let reported = card
        .as_deref()
        .map(|card| reported_card(target, card, public_url));
    catalog.card(&CardObservation {
        agent: &target.name,
        agent_id: &target.id,
        card_json: reported.as_deref().unwrap_or(""),
        error: &error,
        config_version,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    const PUBLIC: &str = "https://gateway.example.test";

    fn card() -> Value {
        json!({
            "name": "Mock",
            "description": "An agent",
            "version": "1.0.0",
            "supportedInterfaces": [
                {"url": "http://10.0.0.1:9120/a2a", "protocolBinding": "GRPC", "protocolVersion": "1.0"},
                {"url": "http://10.0.0.1:9120/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0", "tenant": "t1"},
                {"url": "http://10.0.0.1:9120/a2a", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
            ],
            "capabilities": {"streaming": true, "pushNotifications": true, "extendedAgentCard": true},
            "securitySchemes": {"upstream": {"oauth2SecurityScheme": {}}},
            "securityRequirements": [{"schemes": {"upstream": {"list": ["x"]}}}],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {"id": "s1", "name": "S", "description": "d", "tags": [], "securityRequirements": [{"schemes": {"upstream": {"list": []}}}]},
                {"id": "s2", "name": "S2", "description": "d", "tags": []},
            ],
            "signatures": [{"protected": "e30", "signature": "AA"}],
        })
    }

    #[test]
    fn the_card_points_at_the_gateway_and_describes_its_auth() {
        let mut card = card();
        assert!(rewrite(&mut card, "helper", PUBLIC));
        assert_eq!(
            card["supportedInterfaces"],
            json!([{
                "url": "https://gateway.example.test/a2a/helper",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
                "tenant": "t1",
            }])
        );
        assert_eq!(
            card["securitySchemes"]["lumewarp"]["apiKeySecurityScheme"]["name"],
            "X-Lumewarp-Key"
        );
        assert_eq!(
            card["securityRequirements"],
            json!([{"schemes": {"lumewarp": {"list": []}}}])
        );
        assert_eq!(
            card["skills"][0]["securityRequirements"],
            json!([{"schemes": {"lumewarp": {"list": []}}}])
        );
        assert!(card["skills"][1].get("securityRequirements").is_none());
        assert!(card.get("signatures").is_none());
        assert_eq!(card["capabilities"]["pushNotifications"], false);
        // Everything else is preserved.
        assert_eq!(card["skills"][1]["id"], "s2");
        assert_eq!(card["version"], "1.0.0");
    }

    #[test]
    fn a_card_without_capabilities_gains_a_push_false_and_no_other_claims() {
        let mut card = json!({"name": "x", "description": "d", "version": "1", "skills": []});
        rewrite(&mut card, "a", PUBLIC);
        assert_eq!(card["capabilities"], json!({"pushNotifications": false}));
        assert_eq!(
            card["supportedInterfaces"][0]["url"],
            "https://gateway.example.test/a2a/a"
        );
    }

    #[test]
    fn non_objects_are_refused_and_the_etag_covers_the_rewritten_bytes() {
        let mut value = json!([1, 2]);
        assert!(!rewrite(&mut value, "a", PUBLIC));
        let rendered = render(&card(), "a", PUBLIC).unwrap();
        assert!(rendered.etag.starts_with('"') && rendered.etag.ends_with('"'));
        assert_eq!(rendered.etag.len(), 66);
        assert!(rendered.body.contains("X-Lumewarp-Key"));
        assert!(!rendered.body.contains("10.0.0.1"));
    }

    #[test]
    fn the_served_card_is_private_to_the_callers_key() {
        let rendered = render(&card(), "a", PUBLIC).unwrap();
        let response = response(&rendered, &HeaderMap::new());
        assert_eq!(
            response.headers().get("cache-control").unwrap(),
            "private, max-age=300"
        );
    }

    #[test]
    fn a_changed_card_has_a_different_etag() {
        let first = render(&card(), "a", PUBLIC).unwrap();
        let mut modified = card();
        modified["description"] = json!("changed");
        let second = render(&modified, "a", PUBLIC).unwrap();
        assert_ne!(first.etag, second.etag);
    }

    #[test]
    fn errors_are_bounded_on_a_character_boundary() {
        let text = "é".repeat(MAX_ERROR_BYTES);
        let truncated = truncate_error(&text);
        assert!(truncated.len() <= MAX_ERROR_BYTES);
        assert!(truncated.is_char_boundary(truncated.len()));
    }

    fn target(name: &str, id: &str, url: &str) -> A2aTarget {
        A2aTarget {
            id: id.into(),
            name: name.into(),
            url: url.parse().unwrap(),
            card_url: format!(
                "{}://{}",
                url.split("://").next().unwrap(),
                url.split("://").nth(1).unwrap()
            )
            .parse()
            .unwrap(),
            auth: crate::targets::Auth::None,
            middleware: None,
            timeout: targets::DEFAULT_TIMEOUT,
            card_json: None,
        }
    }

    #[test]
    fn a_cached_card_belongs_to_one_agent_id_and_failures_keep_it() {
        let agent = target("cached-card-agent", "id-1", "http://127.0.0.1:1/a2a");
        let catalog = Registry::new();
        report_success(&catalog, &agent, "{\"name\":\"Mock\"}".to_owned(), None, 3);
        assert!(cached("cached-card-agent", "id-1").is_some());
        assert!(cached("cached-card-agent", "id-2").is_none());
        report_error(&catalog, &agent, "card fetch returned status 500", None, 3);
        // A failed fetch keeps the last good card and reports the error with it.
        assert_eq!(
            cached("cached-card-agent", "id-1").as_deref(),
            Some("{\"name\":\"Mock\"}")
        );
        let report = catalog.drain_report(2000, 64 * 1024 * 1024);
        assert_eq!(report.cards.len(), 1);
        assert_eq!(report.cards[0].agent, "cached-card-agent");
        assert_eq!(report.cards[0].agent_id, "id-1");
        assert_eq!(report.cards[0].error, "card fetch returned status 500");
        assert!(report.cards[0].card_json.contains("Mock"));
        assert_eq!(report.cards[0].config_version, 3);
    }

    /// A tiny one-shot HTTP server: records the raw request (headers included)
    /// and answers with `body` as a JSON document.
    fn serve_card_once(
        body: String,
    ) -> (
        std::net::SocketAddr,
        Arc<Mutex<Option<String>>>,
        std::thread::JoinHandle<()>,
    ) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(None));
        let recorded = Arc::clone(&seen);
        let thread = std::thread::spawn(move || {
            let Ok((mut stream, _)) = listener.accept() else {
                return;
            };
            let mut raw = Vec::new();
            let mut chunk = [0u8; 512];
            while !raw.windows(4).any(|window| window == b"\r\n\r\n") {
                match stream.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(read) => raw.extend_from_slice(&chunk[..read]),
                }
            }
            *recorded.lock().unwrap() = Some(String::from_utf8_lossy(&raw).into_owned());
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = stream.write_all(response.as_bytes());
        });
        (address, seen, thread)
    }

    fn card_target(url: &str, card_url: &str, auth: crate::targets::Auth) -> A2aTarget {
        A2aTarget {
            id: "card-auth".into(),
            name: "card-auth".into(),
            url: url.parse().unwrap(),
            card_url: card_url.parse().unwrap(),
            auth,
            middleware: None,
            timeout: targets::DEFAULT_TIMEOUT,
            card_json: None,
        }
    }

    fn static_bearer() -> crate::targets::Auth {
        crate::targets::Auth::StaticBearer {
            authorization: hyper::header::HeaderValue::from_static("Bearer secret"),
        }
    }

    #[tokio::test]
    async fn the_card_fetch_sends_the_agent_auth_on_its_own_origin() {
        let body = r#"{"name":"Auth","description":"d","version":"1","skills":[]}"#;
        let (address, seen, thread) = serve_card_once(body.to_owned());
        let target = card_target(
            &format!("http://{address}/a2a"),
            &format!("http://{address}/.well-known/agent-card.json"),
            static_bearer(),
        );
        let text = fetch(&target, &crate::proxy::client()).await.unwrap();
        thread.join().unwrap();
        assert_eq!(text, body);
        let request = seen.lock().unwrap().clone().expect("the card request");
        let lower = request.to_ascii_lowercase();
        assert!(
            lower.contains("authorization: bearer secret"),
            "the credential was not sent: {request}"
        );
        assert!(lower.contains("accept: application/json"), "{request}");
    }

    #[tokio::test]
    async fn a_card_on_another_origin_is_fetched_without_credentials() {
        let body = r#"{"name":"Public","description":"d","version":"1","skills":[]}"#;
        let (address, seen, thread) = serve_card_once(body.to_owned());
        // The agent endpoint is on a different port, hence a different origin.
        let target = card_target(
            "http://127.0.0.1:1/a2a",
            &format!("http://{address}/.well-known/agent-card.json"),
            static_bearer(),
        );
        let text = fetch(&target, &crate::proxy::client()).await.unwrap();
        thread.join().unwrap();
        assert_eq!(text, body);
        let request = seen.lock().unwrap().clone().expect("the card request");
        assert!(
            !request.to_ascii_lowercase().contains("authorization:"),
            "the agent's credential went to another origin: {request}"
        );
    }

    #[test]
    fn a_card_report_reads_the_current_snapshot_version() {
        use crate::router::RoutingTable;
        use arc_swap::ArcSwapOption;
        let runtime = Runtime::new();
        let yaml = "a2a_agents:\n  - {name: versioned, url: 'http://127.0.0.1:1/a2a'}\n";
        let (_, mut snapshot) = lumewarp_config::Config::parse(yaml).unwrap();
        snapshot.version = 7;
        let table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::standalone(&snapshot),
            &runtime,
        )
        .unwrap();
        let slot: crate::server::Snapshot = Arc::new(ArcSwapOption::from_pointee(table));
        assert_eq!(snapshot_version(&slot), 7);
        let empty: crate::server::Snapshot = Arc::new(ArcSwapOption::empty());
        assert_eq!(snapshot_version(&empty), 0);
    }

    #[test]
    fn a_signature_changes_on_url_auth_or_timeout() {
        let base = target("sig", "sig", "http://127.0.0.1:1/a2a");
        assert_eq!(signature(&base), signature(&base));
        let mut other_url = target("sig", "sig", "http://127.0.0.1:2/a2a");
        assert_ne!(signature(&base), signature(&other_url));
        other_url.timeout = Duration::from_secs(1);
        assert_ne!(signature(&base), signature(&other_url));
        let mut other_card_url = target("sig", "sig", "http://127.0.0.1:1/a2a");
        other_card_url.card_url = "http://127.0.0.1:1/other.json".parse().unwrap();
        assert_ne!(signature(&base), signature(&other_card_url));
        let mut bearer = target("sig", "sig", "http://127.0.0.1:1/a2a");
        bearer.auth = crate::targets::Auth::StaticBearer {
            authorization: hyper::header::HeaderValue::from_static("Bearer a"),
        };
        assert_ne!(signature(&base), signature(&bearer));
        let mut rotated = target("sig", "sig", "http://127.0.0.1:1/a2a");
        rotated.auth = crate::targets::Auth::StaticBearer {
            authorization: hyper::header::HeaderValue::from_static("Bearer b"),
        };
        assert_ne!(signature(&bearer), signature(&rotated));
    }

    /// No A2A request is made anywhere in this test: the startup supervisor
    /// must fetch the card and report it on its own.
    #[tokio::test]
    async fn the_supervisor_fetches_and_reports_a_card_without_any_request() {
        use crate::router::RoutingTable;
        use arc_swap::ArcSwapOption;
        use mock_upstream::a2a::{serve_a2a, A2aOptions, A2aServer};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let options = A2aOptions {
            listen: addr,
            ..Default::default()
        };
        let mock = Arc::new(A2aServer::new(options).unwrap());
        tokio::spawn(async move {
            let _ = serve_a2a(listener, mock).await;
        });

        let yaml =
            format!("a2a_agents:\n  - {{name: supervised_agent, url: 'http://{addr}/a2a'}}\n");
        let (_, snapshot) = lumewarp_config::Config::parse(&yaml).unwrap();
        let runtime = Runtime::with_usage_and_env(
            Arc::new(crate::metrics::Metrics::new()),
            crate::usage::UsageSink::disabled(),
            crate::runtime::EnvOptions {
                id_key: None,
                public_url: Some("https://gw.example.test".into()),
            },
        );
        let table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::standalone(&snapshot),
            &runtime,
        )
        .unwrap();
        let slot: crate::server::Snapshot = Arc::new(ArcSwapOption::from_pointee(table));
        let supervisor = tokio::spawn(supervise(Arc::clone(&runtime), Arc::clone(&slot)));

        let deadline = Instant::now() + Duration::from_secs(10);
        let mut cards = Vec::new();
        while Instant::now() < deadline && cards.is_empty() {
            cards = runtime.catalog.drain_report(2000, 64 * 1024 * 1024).cards;
            if cards.is_empty() {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
        supervisor.abort();
        assert_eq!(cards.len(), 1, "no CatalogReport came from the supervisor");
        assert_eq!(cards[0].agent, "supervised_agent");
        assert_eq!(cards[0].agent_id, "supervised_agent");
        assert_eq!(cards[0].error, "");
        assert!(
            cards[0].card_json.contains("Lumewarp Mock A2A Agent"),
            "{}",
            cards[0].card_json
        );
        // Reported as the rewritten card: gateway URL, no upstream signatures.
        let reported: Value = serde_json::from_str(&cards[0].card_json).unwrap();
        assert_eq!(
            reported["supportedInterfaces"][0]["url"],
            "https://gw.example.test/a2a/supervised_agent"
        );
        assert!(reported.get("signatures").is_none());
        assert_eq!(reported["capabilities"]["pushNotifications"], false);
        assert_eq!(cards[0].config_version, 0);
        assert!(cached("supervised_agent", "supervised_agent").is_some());
    }
}

//! What the gateway learned from upstream and has to tell the control plane:
//! MCP tool/prompt definitions and A2A agent cards, bounded and deduplicated
//! (`docs/plugins.md`, "Catalog reports").
//!
//! The control plane cannot fetch these itself when a server uses a WASM auth
//! plugin or a client's token, so the gateway reports them in `CatalogReport` on
//! the config stream. Everything upstream sends is untrusted:
//!
//! - a definition or card over 64 KiB is dropped and counted, and a fetch error
//!   is cut to [`MAX_ERROR_BYTES`], which is also the control plane's limit;
//! - at most 200 unreported definitions are held per server (and 200 cards);
//! - the same (server, hash) is queued at most once per 10 minutes, counted
//!   from the moment it left the queue in a report;
//! - one `CatalogReport` carries at most [`MAX_REPORT_ITEMS`] observations and
//!   [`MAX_REPORT_BYTES`] bytes; the rest stays queued ([`Registry::drain_report`]);
//! - a card is reported whenever it differs from the last one reported (m7);
//! - the dedupe tables are bounded and evict the oldest entry first, without a
//!   scan per observation (M6).
//!
//! Like `health::Registry`, the registry lives in `Runtime` for the life of the
//! process: the config stream waits on [`Registry::changed`] and drains a report
//! with [`Registry::drain_report`]. Reports add to what was reported before, so
//! draining never loses older entries, and [`Registry::restore`] puts a report
//! back when its message could not be sent.
//!
//! [`Observed`] is the sibling process-lifetime memory for the call gate (L1):
//! the last definition hash seen upstream per (server, kind, name), so a call
//! can be refused after the upstream changed a definition, not only the name
//! missing from a list response.

// The MCP/A2A request path (W2) wires these modules; until then most of the
// API has no caller in the binary, which `-D warnings` would reject.
#![allow(dead_code)]
use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

use lumewarp_proto::config::v1::{
    AgentCardReport, CatalogReport, McpDefinitionKind, ObservedDefinition,
};
use prost::Message;
use sha2::{Digest, Sha256};
use tokio::sync::Notify;

use crate::targets::{Approved, ReviewMode, MAX_APPROVED_DEFINITIONS};

/// Unreported definitions held per server.
pub const MAX_PENDING_PER_SERVER: usize = 200;

/// Largest definition or agent card the gateway reports (canonical JSON bytes).
pub const MAX_DEFINITION_BYTES: usize = 64 * 1024;
pub const MAX_CARD_BYTES: usize = 64 * 1024;

/// The same hash is reported at most once per gateway in this window.
pub const REPORT_INTERVAL_MS: i64 = 10 * 60 * 1000;

/// Distinct agents with an unreported card.
pub const MAX_PENDING_AGENTS: usize = 200;

/// Largest fetch error the gateway reports. The control plane refuses a whole
/// batch for a longer one, so the gateway truncates on a character boundary
/// before queueing; the two constants must stay equal.
pub const MAX_ERROR_BYTES: usize = 500;

/// One `CatalogReport` message stays at or below this many observations and
/// this many protobuf-encoded bytes, well under the control plane's 2000-item
/// limit and the 64 MiB gRPC message cap (M5). The rest waits for the next one.
pub const MAX_REPORT_ITEMS: usize = 1000;
pub const MAX_REPORT_BYTES: usize = 16 * 1024 * 1024;

/// Entries kept per report-state table (definition dedupe, card reports); the
/// oldest entry is evicted first (M6).
const MAX_TRACKED: usize = 20_000;

/// Why an observation never reached a report.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DropReason {
    /// Larger than [`MAX_DEFINITION_BYTES`] / [`MAX_CARD_BYTES`].
    Oversized,
    /// [`MAX_PENDING_PER_SERVER`] or [`MAX_PENDING_AGENTS`] reached.
    PendingLimit,
    /// Empty server, agent or name, or a definition that cannot be reported.
    Invalid,
}

impl DropReason {
    /// The `reason` label of `lumewarp_mcp_catalog_reports_dropped_total`.
    pub fn label(self) -> &'static str {
        match self {
            Self::Oversized => "oversized",
            Self::PendingLimit => "pending_limit",
            Self::Invalid => "invalid",
        }
    }
}

/// What happened to one observation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Recorded {
    /// Waiting for the next report.
    Queued,
    /// Already queued or reported within [`REPORT_INTERVAL_MS`].
    Suppressed,
    /// Dropped; counted through [`Drops`].
    Dropped(DropReason),
}

/// The counter behind `lumewarp_mcp_catalog_reports_dropped_total`. `Metrics`
/// implements it; tests can pass their own.
pub trait Drops: Send + Sync {
    fn catalog_dropped(&self, reason: &'static str);
}

/// One observed definition, ready to be reported.
#[derive(Clone, Copy, Debug)]
pub struct Observation<'a> {
    /// `McpServer.name`.
    pub server: &'a str,
    /// `McpServer.id` (L3 provenance: the control plane discards a report whose
    /// id no longer matches the name).
    pub server_id: &'a str,
    pub kind: McpDefinitionKind,
    /// Upstream name, without the `<server>__` prefix.
    pub name: &'a str,
    /// JCS canonical JSON of the projection (`mcp_hash::Observed::canonical_json`).
    pub canonical_json: &'a str,
    /// `mcp_hash::Observed::hash`.
    pub hash: [u8; 32],
    /// The gateway serves it (a TOFU first sight) rather than withholding it.
    pub auto_approved: bool,
    /// Snapshot version the gateway was serving when it observed this.
    pub config_version: u64,
}

/// One fetched agent card (or the error of a failed fetch).
#[derive(Clone, Copy, Debug)]
pub struct CardObservation<'a> {
    /// `A2aAgent.name`.
    pub agent: &'a str,
    /// `A2aAgent.id` (L3 provenance, see [`Observation::server_id`]).
    pub agent_id: &'a str,
    /// Card JSON as fetched, at most [`MAX_CARD_BYTES`]; empty when the fetch failed.
    pub card_json: &'a str,
    /// Sanitized fetch error, empty on success.
    pub error: &'a str,
    /// Snapshot version the gateway was serving when it fetched this.
    pub config_version: u64,
}

/// Latest card waiting for the next report, with the digest of what was
/// observed (card JSON, NUL, error text) so a repeat is recognised (m7).
struct PendingCard {
    report: AgentCardReport,
    digest: [u8; 32],
}

/// The registry of observed definitions and cards.
pub struct Registry {
    inner: Mutex<Inner>,
    changed: Arc<Notify>,
    drops: Option<Arc<dyn Drops>>,
}

#[derive(Default)]
struct Inner {
    definitions: VecDeque<ObservedDefinition>,
    pending_per_server: HashMap<String, usize>,
    /// (server, hash) already in `definitions`, so a repeated observation does
    /// not queue the same definition twice before a report is drained.
    queued: HashSet<(String, [u8; 32])>,
    /// (server, hash) -> unix ms it left the queue in a report. Marked by
    /// [`Registry::drain_report`] (M5: a report that is never sent does not
    /// suppress the next one) and undone by [`Registry::restore`].
    seen: BoundedTable<(String, [u8; 32]), i64>,
    /// Latest card per agent waiting for the next report.
    cards: HashMap<String, PendingCard>,
    /// Digest of the last reported card per agent (m7: report whenever the
    /// content differs from what the control plane last got).
    reported: BoundedTable<String, [u8; 32]>,
}

/// A report-state table that keeps at most [`MAX_TRACKED`] entries, evicting
/// the oldest first (M6). `order` holds every key inserted since it was last
/// popped, so eviction never scans the map; a key already in the map keeps its
/// position.
struct BoundedTable<K, V> {
    map: HashMap<K, V>,
    order: VecDeque<K>,
}

impl<K, V> Default for BoundedTable<K, V> {
    fn default() -> Self {
        Self {
            map: HashMap::new(),
            order: VecDeque::new(),
        }
    }
}

impl<K: std::hash::Hash + Eq + Clone, V> BoundedTable<K, V> {
    fn insert(&mut self, key: K, value: V) {
        if self.map.insert(key.clone(), value).is_none() {
            self.order.push_back(key);
        }
        while self.map.len() > MAX_TRACKED {
            // `order` never has fewer live keys than the map: only this loop
            // and `remove` shrink it, and `remove` leaves the key in `order`.
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            self.map.remove(&oldest);
        }
    }

    fn get<Q>(&self, key: &Q) -> Option<&V>
    where
        K: std::borrow::Borrow<Q>,
        Q: std::hash::Hash + Eq + ?Sized,
    {
        self.map.get(key)
    }

    fn remove<Q>(&mut self, key: &Q) -> Option<V>
    where
        K: std::borrow::Borrow<Q>,
        Q: std::hash::Hash + Eq + ?Sized,
    {
        self.map.remove(key)
    }

    fn len(&self) -> usize {
        self.map.len()
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self {
            inner: Mutex::new(Inner::default()),
            changed: Arc::new(Notify::new()),
            drops: None,
        }
    }
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    /// A registry that counts drops (`runtime.metrics` implements [`Drops`]).
    pub fn with_drops(drops: Arc<dyn Drops>) -> Self {
        Self {
            drops: Some(drops),
            ..Self::default()
        }
    }

    /// Signalled when something was queued for the next report. One stored
    /// permit, so a change before the call is not lost and bursts coalesce.
    pub async fn changed(&self) {
        self.changed.notified().await;
    }

    /// The next batch of observations: at most `max_items` and `max_bytes`
    /// (protobuf-encoded, definitions plus cards), so one message stays under
    /// what the control plane accepts (M5). Whatever does not fit stays queued.
    ///
    /// A drained definition is remembered as reported for
    /// [`REPORT_INTERVAL_MS`]; [`Self::restore`] undoes that if the message
    /// never reached the control plane.
    pub fn drain_report(&self, max_items: usize, max_bytes: usize) -> CatalogReport {
        self.drain_at(now_ms(), max_items, max_bytes)
    }

    /// [`Self::drain_report`] with a caller-supplied clock (tests).
    #[cfg(test)]
    fn drain_report_at(&self, now: i64, max_items: usize, max_bytes: usize) -> CatalogReport {
        self.drain_at(now, max_items, max_bytes)
    }

    fn drain_at(&self, now: i64, max_items: usize, max_bytes: usize) -> CatalogReport {
        let max_items = max_items.max(1);
        let mut inner = lock_inner(&self.inner);
        let mut definitions = Vec::with_capacity(max_items.min(inner.definitions.len()));
        let mut bytes = 0usize;
        while definitions.len() < max_items {
            let Some(front) = inner.definitions.front() else {
                break;
            };
            let size = front.encoded_len();
            // Always take at least one item: a batch with an impossible byte
            // budget must still make progress.
            if !definitions.is_empty() && bytes.saturating_add(size) > max_bytes {
                break;
            }
            bytes = bytes.saturating_add(size);
            let definition = inner.definitions.pop_front().expect("checked above");
            let drained = match inner.pending_per_server.get_mut(&definition.server) {
                Some(count) => {
                    *count = count.saturating_sub(1);
                    *count == 0
                }
                None => false,
            };
            if drained {
                inner.pending_per_server.remove(&definition.server);
            }
            if let Ok(hash) = <[u8; 32]>::try_from(definition.hash.as_slice()) {
                let key = (definition.server.clone(), hash);
                inner.queued.remove(&key);
                inner.seen.insert(key, now);
            }
            definitions.push(definition);
        }
        let mut cards = Vec::new();
        let room = max_items - definitions.len();
        if room > 0 && !inner.cards.is_empty() {
            // Deterministic order; at most MAX_PENDING_AGENTS (200) agents.
            let mut agents: Vec<String> = inner.cards.keys().cloned().collect();
            agents.sort();
            for agent in agents {
                if cards.len() >= room {
                    break;
                }
                let Some(pending) = inner.cards.get(&agent) else {
                    continue;
                };
                let size = pending.report.encoded_len();
                if bytes.saturating_add(size) > max_bytes {
                    break;
                }
                bytes = bytes.saturating_add(size);
                let pending = inner.cards.remove(&agent).expect("present");
                inner.reported.insert(agent, pending.digest);
                cards.push(pending.report);
            }
        }
        CatalogReport { definitions, cards }
    }

    /// Puts a drained report back after its message could not be sent: the
    /// definitions are queued again at the front (their suppression window
    /// starts at the next drain) and the cards are pending again, so nothing
    /// drained from the registry is lost on a broken stream.
    ///
    /// Accepted while the batch was in flight can briefly push a server past
    /// [`MAX_PENDING_PER_SERVER`]; the next drain takes it back down.
    pub fn restore(&self, report: CatalogReport) {
        let mut inner = lock_inner(&self.inner);
        for definition in report.definitions.into_iter().rev() {
            if let Ok(hash) = <[u8; 32]>::try_from(definition.hash.as_slice()) {
                let key = (definition.server.clone(), hash);
                // Not reported after all.
                inner.seen.remove(&key);
                if inner.queued.insert(key) {
                    *inner
                        .pending_per_server
                        .entry(definition.server.clone())
                        .or_default() += 1;
                    inner.definitions.push_front(definition);
                }
                continue;
            }
            inner.definitions.push_front(definition);
        }
        for card in report.cards {
            let agent = card.agent.clone();
            // The digest is recomputed from the reported bytes; a fetch error
            // was truncated to MAX_ERROR_BYTES, so a repeat of a longer error is
            // reported once more instead of being suppressed. Harmless (the
            // control plane keeps the last card) and only after a failed send.
            let digest = card_digest(&card.card_json, &card.error);
            inner.reported.remove(&agent);
            if inner.cards.len() < MAX_PENDING_AGENTS || inner.cards.contains_key(&agent) {
                inner.cards.insert(
                    agent,
                    PendingCard {
                        report: card,
                        digest,
                    },
                );
            }
        }
        drop(inner);
        self.changed.notify_one();
    }

    /// Definitions and cards waiting for the next report.
    pub fn pending(&self) -> usize {
        let inner = lock_inner(&self.inner);
        inner.definitions.len() + inner.cards.len()
    }

    /// Queues a definition for the next report, unless it is already queued or
    /// was reported within [`REPORT_INTERVAL_MS`] (counted from the moment it
    /// left the queue, see [`Self::drain_report`]).
    pub fn observe(&self, observation: &Observation<'_>) -> Recorded {
        self.observe_at(now_ms(), observation)
    }

    /// [`Self::observe`] with a caller-supplied clock (tests).
    pub fn observe_at(&self, now: i64, observation: &Observation<'_>) -> Recorded {
        let Observation {
            server,
            server_id,
            kind,
            name,
            canonical_json,
            hash,
            auto_approved,
            config_version,
        } = *observation;
        if server.is_empty() || name.is_empty() || kind == McpDefinitionKind::Unspecified {
            return self.drop(DropReason::Invalid);
        }
        if canonical_json.len() > MAX_DEFINITION_BYTES {
            return self.drop(DropReason::Oversized);
        }
        let key = (server.to_owned(), hash);
        let mut inner = lock_inner(&self.inner);
        if inner.queued.contains(&key) {
            return Recorded::Suppressed;
        }
        if let Some(&at) = inner.seen.get(&key) {
            if now.saturating_sub(at) < REPORT_INTERVAL_MS {
                return Recorded::Suppressed;
            }
        }
        let pending = inner.pending_per_server.get(server).copied().unwrap_or(0);
        if pending >= MAX_PENDING_PER_SERVER {
            return self.drop(DropReason::PendingLimit);
        }
        inner.queued.insert(key);
        *inner
            .pending_per_server
            .entry(server.to_owned())
            .or_default() += 1;
        inner.definitions.push_back(ObservedDefinition {
            server: server.to_owned(),
            kind: kind as i32,
            name: name.to_owned(),
            hash: hash.to_vec(),
            definition_json: canonical_json.to_owned(),
            auto_approved,
            observed_at_unix_ms: now,
            server_id: server_id.to_owned(),
            config_version,
        });
        drop(inner);
        self.changed.notify_one();
        Recorded::Queued
    }

    /// Queues an agent card (or the error of a failed fetch) for the next
    /// report. The latest card of an agent replaces an older, still unreported
    /// one: the control plane keeps the last card anyway.
    ///
    /// Unlike a definition, a card is not suppressed by a time window: it is
    /// reported whenever its content differs from the last report (m7), so a
    /// card that changes A -> B -> A reaches the control plane three times.
    pub fn card(&self, card: &CardObservation<'_>) -> Recorded {
        self.card_at(now_ms(), card)
    }

    /// [`Self::card`] with a caller-supplied clock (tests).
    pub fn card_at(&self, now: i64, card: &CardObservation<'_>) -> Recorded {
        let CardObservation {
            agent,
            agent_id,
            card_json,
            error,
            config_version,
        } = *card;
        if agent.is_empty() || (card_json.is_empty() && error.is_empty()) {
            return self.drop(DropReason::Invalid);
        }
        if card_json.len() > MAX_CARD_BYTES {
            return self.drop(DropReason::Oversized);
        }
        let digest = card_digest(card_json, error);
        let mut inner = lock_inner(&self.inner);
        if inner
            .cards
            .get(agent)
            .is_some_and(|card| card.digest == digest)
        {
            return Recorded::Suppressed;
        }
        if inner.reported.get(agent) == Some(&digest) {
            return Recorded::Suppressed;
        }
        if !inner.cards.contains_key(agent) && inner.cards.len() >= MAX_PENDING_AGENTS {
            return self.drop(DropReason::PendingLimit);
        }
        inner.cards.insert(
            agent.to_owned(),
            PendingCard {
                report: AgentCardReport {
                    agent: agent.to_owned(),
                    card_json: card_json.to_owned(),
                    error: truncate(error, MAX_ERROR_BYTES),
                    fetched_at_unix_ms: now,
                    agent_id: agent_id.to_owned(),
                    config_version,
                },
                digest,
            },
        );
        drop(inner);
        self.changed.notify_one();
        Recorded::Queued
    }

    /// (definition, card) report-state entries held; proves the bounds.
    #[cfg(test)]
    fn tracked(&self) -> (usize, usize) {
        let inner = lock_inner(&self.inner);
        (inner.seen.len(), inner.reported.len())
    }

    fn drop(&self, reason: DropReason) -> Recorded {
        if let Some(drops) = &self.drops {
            drops.catalog_dropped(reason.label());
        }
        Recorded::Dropped(reason)
    }
}

/// What a card report carries as identity: the card exactly as fetched and the
/// (untruncated) fetch error.
fn card_digest(card_json: &str, error: &str) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(card_json.as_bytes());
    digest.update([0u8]);
    digest.update(error.as_bytes());
    digest.finalize().into()
}

fn lock_inner(inner: &Mutex<Inner>) -> std::sync::MutexGuard<'_, Inner> {
    inner.lock().unwrap_or_else(|error| error.into_inner())
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_millis() as i64)
}

/// Truncates on a character boundary.
fn truncate(text: &str, limit: usize) -> String {
    if text.len() <= limit {
        return text.to_owned();
    }
    let mut end = limit;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

/// What the gateway does with one observed definition.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Review {
    /// Include it in `tools/list` / `prompts/list` responses.
    pub serve: bool,
    /// Report it, so the control plane learns the definition (and can approve
    /// a withheld one).
    pub report: bool,
    /// The gateway approves it itself (a TOFU first sight); the control plane
    /// records it as approved.
    pub auto_approved: bool,
}

/// The review decision of `docs/plugins.md` ("Review modes").
///
/// `known`: the snapshot has a definition of this name in **any** state
/// (`targets::Approved::knows`) — TOFU's first sight is exactly `!known` (L2).
/// `hash_approved`: it has *this* hash (`Approved::accepts`).
pub fn review(mode: ReviewMode, known: bool, hash_approved: bool) -> Review {
    let serve = Review {
        serve: true,
        report: false,
        auto_approved: false,
    };
    match mode {
        ReviewMode::Off => serve,
        ReviewMode::Manual => Review {
            serve: hash_approved,
            report: !hash_approved,
            auto_approved: false,
        },
        ReviewMode::Tofu => {
            if hash_approved {
                serve
            } else if known {
                // A changed definition of a known name, or a name the control
                // plane reviewed without approving it (pending/rejected):
                // withheld until approved.
                Review {
                    serve: false,
                    report: true,
                    auto_approved: false,
                }
            } else {
                // First sight of a name: served and reported once.
                Review {
                    serve: true,
                    report: true,
                    auto_approved: true,
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Call gate (L1)

/// One server's observed definitions: kind -> upstream name -> last hash.
///
/// Two levels so a lookup with a `&str` borrows instead of allocating; only
/// first sight of a name allocates.
type ServerObserved = HashMap<McpDefinitionKind, HashMap<Arc<str>, [u8; 32]>>;

/// Per-gateway memory of the last definition hash the upstream sent for
/// (server, kind, name).
///
/// Definition review ([`review`], [`Registry::observe`]) decides what a *list
/// response* contains. [`Self::may_call`] answers a different question: may
/// `tools/call` / `prompts/get` name this tool or prompt at all? A definition
/// the upstream changed is withheld from lists but its *earlier* approved hash
/// is still in the snapshot, so "some approved hash exists for this name"
/// would let a client that already knows the name call the changed
/// implementation — exactly the rug-pull the review mode exists to stop (L1).
///
/// The rules, in order:
/// - `off`: allowed;
/// - a hash observed for this exact (server, kind, name) in this process: only
///   when it is an approved hash;
/// - never observed: an approved name is allowed (a client may call before any
///   `tools/list` reached the gateway); under `manual` anything without an
///   approved hash is refused; under `tofu` a name the snapshot has never seen
///   is a first sight and allowed, while one it knows with another state
///   (pending/rejected) is refused.
///
/// Bounded memory: at most [`MAX_APPROVED_DEFINITIONS`] names per server, and
/// [`Self::retain_servers`] drops whole servers when a snapshot stops
/// configuring them. Called from the apply task and the MCP proxy only.
pub struct Observed {
    servers: Mutex<HashMap<Arc<str>, ServerObserved>>,
    first: crate::mcp::First,
}

impl Default for Observed {
    fn default() -> Self {
        Self {
            servers: Mutex::new(HashMap::new()),
            first: crate::mcp::First::default(),
        }
    }
}

impl Observed {
    pub fn new() -> Self {
        Self::default()
    }

    /// The runtime's first hash, bounded independently per server and pruned
    /// on snapshot application together with the L1 memory.
    pub fn first(
        &self,
        server: &str,
        server_id: &str,
        kind: McpDefinitionKind,
        name: &str,
        hash: [u8; 32],
    ) -> Option<[u8; 32]> {
        self.first.record(server, server_id, kind, name, hash)
    }

    /// Records the definition the upstream just sent (list response, or the
    /// `tools/call` / `prompts/get` it answered). The latest observation wins.
    pub fn record(&self, server: &str, kind: McpDefinitionKind, name: &str, hash: [u8; 32]) {
        if server.is_empty() || name.is_empty() || kind == McpDefinitionKind::Unspecified {
            return;
        }
        let mut servers = lock(&self.servers);
        let kinds = servers.entry(server.into()).or_default();
        if let Some(names) = kinds.get_mut(&kind) {
            if let Some(last) = names.get_mut(name) {
                *last = hash;
                return;
            }
            if names.len() >= MAX_APPROVED_DEFINITIONS {
                // Bounded: a server with more names should split.
                return;
            }
        }
        kinds.entry(kind).or_default().insert(name.into(), hash);
    }

    /// The last hash observed for (server, kind, name), if any.
    pub fn last(&self, server: &str, kind: McpDefinitionKind, name: &str) -> Option<[u8; 32]> {
        let servers = lock(&self.servers);
        servers.get(server)?.get(&kind)?.get(name).copied()
    }

    /// The call gate: whether a call naming this tool or prompt may reach the
    /// upstream. See the type docs for the rules.
    pub fn may_call(
        &self,
        mode: ReviewMode,
        server: &str,
        approved: &Approved,
        kind: McpDefinitionKind,
        name: &str,
    ) -> bool {
        if mode == ReviewMode::Off {
            return true;
        }
        if let Some(hash) = self.last(server, kind, name) {
            // The upstream last sent this definition: only an approved one may
            // run, however the list response happened to look.
            return approved.accepts(kind, name, &hash);
        }
        if approved.has_approved(kind, name) {
            return true;
        }
        match mode {
            ReviewMode::Manual => false,
            // A name the snapshot has never reviewed is a first sight; one it
            // knows but does not approve (pending/rejected) is not.
            ReviewMode::Tofu => !approved.knows(kind, name),
            ReviewMode::Off => true,
        }
    }

    /// Drops every entry of a server that is no longer configured. Called by
    /// the apply task after a snapshot is accepted.
    pub fn retain_servers<'a>(&self, servers: impl IntoIterator<Item = &'a str>) {
        let keep: HashSet<&str> = servers.into_iter().collect();
        let mut memory = lock(&self.servers);
        memory.retain(|server, _| keep.contains(server.as_ref()));
        self.first.retain_servers(&keep);
    }

    /// Servers and names held; proves pruning bounds memory.
    #[cfg(test)]
    fn sizes(&self) -> (usize, usize) {
        let memory = lock(&self.servers);
        let names = memory
            .values()
            .flat_map(|kinds| kinds.values())
            .map(HashMap::len)
            .sum::<usize>();
        (memory.len(), names)
    }
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|error| error.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn tofu_first_hashes_are_runtime_owned_per_server_bounded_and_snapshot_pruned() {
        let memory = Observed::new();
        for i in 0..MAX_APPROVED_DEFINITIONS {
            assert_eq!(
                memory.first(
                    "flood",
                    "id-flood",
                    McpDefinitionKind::Tool,
                    &format!("tool-{i}"),
                    [1; 32]
                ),
                Some([1; 32])
            );
        }
        assert!(memory
            .first(
                "flood",
                "id-flood",
                McpDefinitionKind::Prompt,
                "overflow",
                [2; 32]
            )
            .is_none());
        assert_eq!(
            memory.first(
                "healthy",
                "id-healthy",
                McpDefinitionKind::Tool,
                "echo",
                [3; 32]
            ),
            Some([3; 32])
        );
        // First sight is immutable and one Runtime cannot poison another's TOFU.
        assert_eq!(
            memory.first(
                "healthy",
                "id-healthy",
                McpDefinitionKind::Tool,
                "echo",
                [4; 32]
            ),
            Some([3; 32])
        );
        assert_eq!(
            Observed::new().first(
                "healthy",
                "id-healthy",
                McpDefinitionKind::Tool,
                "echo",
                [4; 32]
            ),
            Some([4; 32])
        );
        memory.retain_servers(["healthy"]);
        assert_eq!(
            memory.first(
                "flood",
                "id-flood",
                McpDefinitionKind::Tool,
                "tool-0",
                [5; 32]
            ),
            Some([5; 32])
        );
        // Same-name replacement with a new stable id is a distinct server.
        assert_eq!(
            memory.first(
                "healthy",
                "replacement",
                McpDefinitionKind::Tool,
                "echo",
                [6; 32]
            ),
            Some([6; 32])
        );
        memory.retain_servers([]);
        assert_eq!(
            memory.first(
                "healthy",
                "replacement",
                McpDefinitionKind::Tool,
                "echo",
                [7; 32]
            ),
            Some([7; 32])
        );
    }

    fn observed<'a>(server: &'a str, hash: u8, json: &'a str) -> Observation<'a> {
        Observation {
            server,
            server_id: "id-s1",
            kind: McpDefinitionKind::Tool,
            name: "echo",
            canonical_json: json,
            hash: [hash; 32],
            auto_approved: false,
            config_version: 1,
        }
    }

    fn card<'a>(agent: &'a str, card_json: &'a str, error: &'a str) -> CardObservation<'a> {
        CardObservation {
            agent,
            agent_id: "id-a1",
            card_json,
            error,
            config_version: 1,
        }
    }

    /// A full batch at a fixed clock, so the suppression windows are testable.
    fn drain(registry: &Registry) -> CatalogReport {
        registry.drain_report_at(0, MAX_REPORT_ITEMS, MAX_REPORT_BYTES)
    }

    fn hash_of(count: usize) -> Vec<[u8; 32]> {
        (0..count)
            .map(|i| {
                let mut hash = [0u8; 32];
                hash[..8].copy_from_slice(&(i as u64).to_le_bytes());
                hash
            })
            .collect()
    }

    #[test]
    fn queues_once_and_drops_from_the_report() {
        let registry = Registry::new();
        assert_eq!(
            registry.observe_at(1_000, &observed("s1", 1, "{}")),
            Recorded::Queued
        );
        let report = drain(&registry);
        assert_eq!(report.definitions.len(), 1);
        let definition = &report.definitions[0];
        assert_eq!(definition.server, "s1");
        assert_eq!(definition.kind, McpDefinitionKind::Tool as i32);
        assert_eq!(definition.name, "echo");
        assert_eq!(definition.hash, vec![1; 32]);
        assert_eq!(definition.definition_json, "{}");
        assert!(!definition.auto_approved);
        assert_eq!(definition.observed_at_unix_ms, 1_000);
        assert!(report.cards.is_empty());
        assert_eq!(registry.pending(), 0);
        // An empty registry drains an empty report.
        let empty = drain(&registry);
        assert!(empty.definitions.is_empty() && empty.cards.is_empty());
    }

    #[test]
    fn a_queued_definition_is_never_queued_twice() {
        let registry = Registry::new();
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, "{}")),
            Recorded::Queued
        );
        // Even after the dedupe window it is suppressed while it waits.
        assert_eq!(
            registry.observe_at(REPORT_INTERVAL_MS * 2, &observed("s1", 1, "{}")),
            Recorded::Suppressed
        );
        assert_eq!(drain(&registry).definitions.len(), 1);
    }

    #[test]
    fn the_same_hash_is_reported_at_most_once_per_ten_minutes() {
        let registry = Registry::new();
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, "{}")),
            Recorded::Queued
        );
        drain(&registry);
        assert_eq!(
            registry.observe_at(REPORT_INTERVAL_MS - 1, &observed("s1", 1, "{}")),
            Recorded::Suppressed
        );
        assert_eq!(
            registry.observe_at(REPORT_INTERVAL_MS, &observed("s1", 1, "{}")),
            Recorded::Queued,
            "the window is inclusive at 10 minutes"
        );
        // A new hash of the same server and name is a different definition.
        assert_eq!(
            registry.observe_at(REPORT_INTERVAL_MS, &observed("s1", 2, "{}")),
            Recorded::Queued
        );
        // And the same hash on another server is separate too.
        assert_eq!(
            registry.observe_at(REPORT_INTERVAL_MS, &observed("s2", 1, "{}")),
            Recorded::Queued
        );
        let report = drain(&registry);
        assert_eq!(report.definitions.len(), 3);
    }

    #[test]
    fn a_server_holds_at_most_200_unreported_definitions() {
        let registry = Registry::new();
        let hashes = hash_of(MAX_PENDING_PER_SERVER + 1);
        for hash in &hashes[..MAX_PENDING_PER_SERVER] {
            let observation = Observation {
                server: "s1",
                server_id: "id-s1",
                kind: McpDefinitionKind::Tool,
                name: "tool",
                canonical_json: "{}",
                hash: *hash,
                auto_approved: false,
                config_version: 1,
            };
            assert_eq!(registry.observe_at(0, &observation), Recorded::Queued);
        }
        let observation = Observation {
            server: "s1",
            server_id: "id-s1",
            kind: McpDefinitionKind::Tool,
            name: "tool",
            canonical_json: "{}",
            hash: hashes[MAX_PENDING_PER_SERVER],
            auto_approved: false,
            config_version: 1,
        };
        assert_eq!(
            registry.observe_at(0, &observation),
            Recorded::Dropped(DropReason::PendingLimit)
        );
        // Another server is unaffected.
        let other = Observation {
            server: "s2",
            ..observation
        };
        assert_eq!(registry.observe_at(0, &other), Recorded::Queued);
        assert_eq!(registry.pending(), MAX_PENDING_PER_SERVER + 1);
        // Once a report is drained the rejected definition can be queued.
        drain(&registry);
        assert_eq!(registry.observe_at(1, &observation), Recorded::Queued);
    }

    #[test]
    fn definitions_and_cards_are_capped_at_64_kib() {
        let registry = Registry::new();
        let at_limit = "x".repeat(MAX_DEFINITION_BYTES);
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, &at_limit)),
            Recorded::Queued
        );
        let over = "x".repeat(MAX_DEFINITION_BYTES + 1);
        assert_eq!(
            registry.observe_at(0, &observed("s1", 2, &over)),
            Recorded::Dropped(DropReason::Oversized)
        );
        let card_at_limit = "y".repeat(MAX_CARD_BYTES);
        assert_eq!(
            registry.card_at(0, &card("a1", &card_at_limit, "")),
            Recorded::Queued
        );
        let card_over = "y".repeat(MAX_CARD_BYTES + 1);
        assert_eq!(
            registry.card_at(0, &card("a1", &card_over, "")),
            Recorded::Dropped(DropReason::Oversized)
        );
        let report = drain(&registry);
        assert_eq!(report.definitions.len(), 1);
        assert_eq!(
            report.definitions[0].definition_json.len(),
            MAX_DEFINITION_BYTES
        );
        assert_eq!(report.cards[0].card_json.len(), MAX_CARD_BYTES);
    }

    #[test]
    fn invalid_observations_are_dropped() {
        let registry = Registry::new();
        let mut observation = observed("", 1, "{}");
        assert_eq!(
            registry.observe_at(0, &observation),
            Recorded::Dropped(DropReason::Invalid)
        );
        observation = observed("s1", 1, "{}");
        observation.name = "";
        assert_eq!(
            registry.observe_at(0, &observation),
            Recorded::Dropped(DropReason::Invalid)
        );
        observation.name = "echo";
        observation.kind = McpDefinitionKind::Unspecified;
        assert_eq!(
            registry.observe_at(0, &observation),
            Recorded::Dropped(DropReason::Invalid)
        );
        assert_eq!(
            registry.card_at(0, &card("", "{}", "")),
            Recorded::Dropped(DropReason::Invalid)
        );
        assert_eq!(
            registry.card_at(0, &card("a1", "", "")),
            Recorded::Dropped(DropReason::Invalid)
        );
        assert_eq!(registry.pending(), 0);
    }

    #[test]
    fn a_card_is_reported_whenever_its_content_differs_from_the_last_report() {
        let registry = Registry::new();
        assert_eq!(
            registry.card_at(0, &card("a2", r#"{"name":"a2"}"#, "")),
            Recorded::Queued
        );
        // The same content while it waits is not queued twice.
        assert_eq!(
            registry.card_at(0, &card("a2", r#"{"name":"a2"}"#, "")),
            Recorded::Suppressed
        );
        // A newer card replaces the queued one.
        assert_eq!(
            registry.card_at(0, &card("a2", r#"{"name":"a2b"}"#, "")),
            Recorded::Queued
        );
        assert_eq!(
            registry.card_at(0, &card("a1", "", "connect refused")),
            Recorded::Queued
        );
        let report = drain(&registry);
        assert_eq!(report.cards.len(), 2);
        assert_eq!(report.cards[0].agent, "a1");
        assert_eq!(report.cards[0].card_json, "");
        assert_eq!(report.cards[0].error, "connect refused");
        assert_eq!(report.cards[1].agent, "a2");
        assert_eq!(report.cards[1].card_json, r#"{"name":"a2b"}"#);
        assert_eq!(report.cards[0].fetched_at_unix_ms, 0);
        assert_eq!(report.cards[1].fetched_at_unix_ms, 0);
        // m7: A -> B -> A. The second A differs from the last report (B), so it
        // is reported; the old 10-minute window lost it and left the control
        // plane on B.
        assert_eq!(
            registry.card_at(1, &card("a2", r#"{"name":"a2"}"#, "")),
            Recorded::Queued
        );
        let again = drain(&registry);
        assert_eq!(again.cards.len(), 1);
        assert_eq!(again.cards[0].card_json, r#"{"name":"a2"}"#);
        assert_eq!(again.cards[0].fetched_at_unix_ms, 1);
        // Unchanged content after a report is not re-sent.
        assert_eq!(
            registry.card_at(2, &card("a2", r#"{"name":"a2"}"#, "")),
            Recorded::Suppressed
        );
        // A failed fetch reports its error, and repeating the same error is not
        // re-sent until the error text changes.
        assert_eq!(
            registry.card_at(2, &card("a2", "", "timeout")),
            Recorded::Queued
        );
        drain(&registry);
        assert_eq!(
            registry.card_at(3, &card("a2", "", "timeout")),
            Recorded::Suppressed
        );
        assert_eq!(
            registry.card_at(3, &card("a2", "", "refused")),
            Recorded::Queued
        );
    }

    #[test]
    fn card_errors_are_truncated_on_a_char_boundary() {
        // The control plane refuses a whole batch for a longer error, so the
        // two limits must agree (M4).
        assert_eq!(MAX_ERROR_BYTES, 500);
        let registry = Registry::new();
        let error = "錯".repeat(MAX_ERROR_BYTES);
        assert_eq!(
            registry.card_at(0, &card("a1", "", &error)),
            Recorded::Queued
        );
        let report = drain(&registry);
        let reported = &report.cards[0].error;
        assert!(reported.len() <= MAX_ERROR_BYTES);
        assert!(reported.is_char_boundary(reported.len()));
        assert!(error.starts_with(reported));
    }

    #[test]
    fn at_most_200_agents_have_an_unreported_card() {
        let registry = Registry::new();
        for i in 0..MAX_PENDING_AGENTS {
            assert_eq!(
                registry.card_at(0, &card(&format!("a{i}"), "{}", "")),
                Recorded::Queued
            );
        }
        assert_eq!(
            registry.card_at(0, &card("one-too-many", "{}", "")),
            Recorded::Dropped(DropReason::PendingLimit)
        );
        assert_eq!(drain(&registry).cards.len(), MAX_PENDING_AGENTS);
    }

    struct Counting(Mutex<Vec<&'static str>>);

    impl Drops for Counting {
        fn catalog_dropped(&self, reason: &'static str) {
            self.0
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(reason);
        }
    }

    #[test]
    fn drops_are_counted_with_their_reason() {
        let drops = Arc::new(Counting(Mutex::new(Vec::new())));
        let registry = Registry::with_drops(drops.clone());
        let over = "x".repeat(MAX_DEFINITION_BYTES + 1);
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, &over)),
            Recorded::Dropped(DropReason::Oversized)
        );
        assert_eq!(
            registry.observe_at(0, &observed("", 1, "{}")),
            Recorded::Dropped(DropReason::Invalid)
        );
        let hashes = hash_of(MAX_PENDING_PER_SERVER + 1);
        for hash in &hashes {
            let observation = Observation {
                server: "s1",
                server_id: "id-s1",
                kind: McpDefinitionKind::Tool,
                name: "tool",
                canonical_json: "{}",
                hash: *hash,
                auto_approved: false,
                config_version: 1,
            };
            let _ = registry.observe_at(0, &observation);
        }
        let counted = drops.0.lock().unwrap_or_else(|error| error.into_inner());
        assert_eq!(
            counted.as_slice(),
            ["oversized", "invalid", "pending_limit"]
        );
        assert_eq!(DropReason::Oversized.label(), "oversized");
        assert_eq!(DropReason::PendingLimit.label(), "pending_limit");
        assert_eq!(DropReason::Invalid.label(), "invalid");
    }

    #[test]
    fn tracked_tables_evict_the_oldest_entry_without_scanning() {
        let registry = Registry::new();
        let hashes = hash_of(MAX_TRACKED + 1);
        let observation = |hash: [u8; 32]| Observation {
            server: "s1",
            server_id: "id-s1",
            kind: McpDefinitionKind::Tool,
            name: "tool",
            canonical_json: "{}",
            hash,
            auto_approved: false,
            config_version: 1,
        };
        // Fill well past the bound; draining every 200 keeps the queue, not the
        // tracked table, from being the limit under test.
        for hash in &hashes {
            assert_eq!(
                registry.observe_at(0, &observation(*hash)),
                Recorded::Queued
            );
            if registry.pending() >= MAX_PENDING_PER_SERVER {
                assert_eq!(drain(&registry).definitions.len(), MAX_PENDING_PER_SERVER);
                assert!(registry.tracked().0 <= MAX_TRACKED);
            }
        }
        // The last observation is still queued; draining it pushes the table
        // one entry past its bound, which evicts the oldest.
        drain(&registry);
        assert_eq!(
            registry.tracked().0,
            MAX_TRACKED,
            "the table never grows past its bound"
        );
        // The oldest entry was evicted first, so it is reported again; a recent
        // one is still suppressed.
        assert_eq!(
            registry.observe_at(0, &observation(hashes[0])),
            Recorded::Queued
        );
        assert_eq!(
            registry.observe_at(0, &observation(hashes[MAX_TRACKED - 1])),
            Recorded::Suppressed
        );
        // Clear the observation queued above so the card loop starts empty.
        assert_eq!(drain(&registry).definitions.len(), 1);
        // The card table is bounded the same way.
        for i in 0..=MAX_TRACKED {
            assert_eq!(
                registry.card_at(0, &card(&format!("a{i}"), "{}", "")),
                Recorded::Queued
            );
            if registry.pending() >= MAX_PENDING_AGENTS {
                assert_eq!(drain(&registry).cards.len(), MAX_PENDING_AGENTS);
            }
        }
        drain(&registry);
        assert_eq!(
            registry.tracked().1,
            MAX_TRACKED,
            "the card table never grows past its bound"
        );
        // The oldest card report was evicted too: the same content is sent again.
        assert_eq!(registry.card_at(0, &card("a0", "{}", "")), Recorded::Queued);
    }

    #[test]
    fn drain_report_batches_by_item_count_and_bytes() {
        // Six servers of 200 pending definitions: 1200 > MAX_REPORT_ITEMS.
        let registry = Registry::new();
        for server in ["s1", "s2", "s3", "s4", "s5", "s6"] {
            for i in 0..MAX_PENDING_PER_SERVER {
                let mut hash = [0u8; 32];
                hash[..8].copy_from_slice(&(i as u64).to_le_bytes());
                assert_eq!(
                    registry.observe_at(0, &observed(server, hash[0], "{}")),
                    Recorded::Queued
                );
            }
        }
        let total = registry.pending();
        assert_eq!(total, MAX_PENDING_PER_SERVER * 6);
        let first = drain(&registry);
        assert_eq!(first.definitions.len(), MAX_REPORT_ITEMS);
        assert_eq!(registry.pending(), total - MAX_REPORT_ITEMS);
        let rest = drain(&registry);
        assert_eq!(rest.definitions.len(), total - MAX_REPORT_ITEMS);
        assert_eq!(registry.pending(), 0);

        // A short byte budget takes only what fits (each definition is a 64 KiB
        // JSON body plus fields).
        let registry = Registry::new();
        let json = "x".repeat(MAX_DEFINITION_BYTES);
        for hash in 1..=3u8 {
            assert_eq!(
                registry.observe_at(0, &observed("s1", hash, &json)),
                Recorded::Queued
            );
        }
        let batch = registry.drain_report_at(0, MAX_REPORT_ITEMS, 3 * MAX_DEFINITION_BYTES);
        assert_eq!(batch.definitions.len(), 2, "the third body does not fit");
        let bytes: usize = batch
            .definitions
            .iter()
            .map(prost::Message::encoded_len)
            .sum();
        assert!(bytes <= 3 * MAX_DEFINITION_BYTES);
        assert_eq!(registry.pending(), 1);
        // Even an impossible budget makes progress: one item per batch.
        let one = registry.drain_report_at(0, MAX_REPORT_ITEMS, 1);
        assert_eq!(one.definitions.len(), 1);
        assert_eq!(registry.pending(), 0);
    }

    #[test]
    fn restore_requeues_a_report_that_could_not_be_sent() {
        let registry = Registry::new();
        for (hash, server) in [(1u8, "s1"), (2, "s2"), (3, "s3")] {
            assert_eq!(
                registry.observe_at(0, &observed(server, hash, "{}")),
                Recorded::Queued
            );
        }
        assert_eq!(
            registry.card_at(0, &card("a1", r#"{"a":1}"#, "")),
            Recorded::Queued
        );
        let report = drain(&registry);
        assert_eq!(report.definitions.len(), 3);
        assert_eq!(report.cards.len(), 1);
        assert_eq!(registry.pending(), 0);
        // A drained, but unsent, definition must not be suppressed.
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, "{}")),
            Recorded::Suppressed,
            "the definition is still remembered as reported until it is restored"
        );
        registry.restore(report);
        assert_eq!(registry.pending(), 4, "everything is queued again");
        // While queued, a repeat is suppressed by the queue, not by the window.
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, "{}")),
            Recorded::Suppressed
        );
        assert_eq!(
            registry.card_at(0, &card("a1", r#"{"a":1}"#, "")),
            Recorded::Suppressed
        );
        let again = drain(&registry);
        assert_eq!(again.definitions.len(), 3);
        assert_eq!(again.cards.len(), 1);
        assert_eq!(again.cards[0].card_json, r#"{"a":1}"#);
        assert_eq!(registry.pending(), 0);
        // Restoring an empty report is a no-op.
        registry.restore(CatalogReport::default());
        assert_eq!(registry.pending(), 0);
    }

    #[test]
    fn the_call_gate_refuses_changed_definitions_even_when_the_name_is_approved() {
        use crate::targets::{McpTarget, Targets};
        use lumewarp_proto::config::v1::{McpDefinition, McpReviewMode, McpServer};
        let server = |review_mode: McpReviewMode,
                      definitions: Vec<(
            McpDefinitionKind,
            &str,
            u8,
            lumewarp_proto::config::v1::McpDefinitionState,
        )>| {
            McpServer {
                id: "id-s1".into(),
                name: "s1".into(),
                url: "https://x.test/mcp".into(),
                auth: None,
                timeout_ms: 0,
                review_mode: review_mode as i32,
                definitions: definitions
                    .into_iter()
                    .map(|(kind, name, byte, state)| McpDefinition {
                        kind: kind as i32,
                        name: name.into(),
                        hash: vec![byte; 32],
                        state: state as i32,
                    })
                    .collect(),
            }
        };
        let targets = |srv: McpServer| -> Targets {
            let snapshot = lumewarp_proto::config::v1::Snapshot {
                mcp_servers: vec![srv],
                ..Default::default()
            };
            Targets::compile(&snapshot, None).unwrap()
        };
        let target = |targets: &Targets| -> std::sync::Arc<McpTarget> {
            targets.server("s1").unwrap().clone()
        };
        use lumewarp_proto::config::v1::McpDefinitionState::{Approved, Pending, Rejected};

        // TOFU, never observed: an approved name may be called, an unseen name
        // is a first sight, a reviewed-but-not-approved name is refused.
        let tofu = target(&targets(server(
            McpReviewMode::Tofu,
            vec![
                (McpDefinitionKind::Tool, "echo", 1, Approved),
                (McpDefinitionKind::Tool, "slow", 2, Pending),
                (McpDefinitionKind::Tool, "bad", 3, Rejected),
            ],
        )));
        let memory = Observed::new();
        let gate = |name: &str| {
            memory.may_call(
                tofu.review,
                "s1",
                tofu.approved(),
                McpDefinitionKind::Tool,
                name,
            )
        };
        assert!(gate("echo"), "approved name, never observed");
        assert!(gate("brand-new"), "first sight under tofu");
        assert!(!gate("slow"), "a pending name is not a first sight");
        assert!(!gate("bad"), "a rejected name stays refused");
        // Once the upstream changed an approved definition the call is refused,
        // even though the old hash is still approved and the name is known.
        memory.record("s1", McpDefinitionKind::Tool, "echo", [2u8; 32]);
        assert!(!gate("echo"), "a changed definition is not callable");
        // The old definition seen again is callable again.
        memory.record("s1", McpDefinitionKind::Tool, "echo", [1u8; 32]);
        assert!(gate("echo"));
        // A first sight that the gateway itself served (hash recorded) stays
        // callable before the control plane approves it only if it is not in
        // the snapshot; under manual it is not.
        memory.record("s1", McpDefinitionKind::Tool, "brand-new", [9u8; 32]);
        assert!(!gate("brand-new"), "observed, not approved");

        // manual: nothing without an approved hash.
        let manual = target(&targets(server(
            McpReviewMode::Manual,
            vec![(McpDefinitionKind::Tool, "echo", 1, Approved)],
        )));
        let gate = |name: &str| {
            memory.may_call(
                manual.review,
                "s1",
                manual.approved(),
                McpDefinitionKind::Tool,
                name,
            )
        };
        assert!(gate("echo"), "approved, never observed");
        assert!(!gate("brand-new"), "manual refuses what it has no hash for");
        memory.record("s1", McpDefinitionKind::Tool, "echo", [5u8; 32]);
        assert!(!gate("echo"), "observed hash is not approved");

        // off: not checked at all.
        let off = target(&targets(server(McpReviewMode::Off, vec![])));
        memory.record("s1", McpDefinitionKind::Tool, "echo", [7u8; 32]);
        assert!(memory.may_call(
            off.review,
            "s1",
            off.approved(),
            McpDefinitionKind::Tool,
            "echo"
        ));
        // Kinds are separate: a tool observation says nothing about a prompt.
        assert!(memory.may_call(
            off.review,
            "s1",
            off.approved(),
            McpDefinitionKind::Prompt,
            "echo"
        ));
        let tofu_prompt = target(&targets(server(McpReviewMode::Tofu, vec![])));
        assert!(memory.may_call(
            tofu_prompt.review,
            "s1",
            tofu_prompt.approved(),
            McpDefinitionKind::Prompt,
            "echo"
        ));
    }

    #[test]
    fn observed_definitions_are_bounded_per_server_and_dropped_with_the_server() {
        let memory = Observed::new();
        memory.record("s1", McpDefinitionKind::Tool, "echo", [1u8; 32]);
        memory.record("s1", McpDefinitionKind::Prompt, "greet", [2u8; 32]);
        memory.record("s2", McpDefinitionKind::Tool, "echo", [3u8; 32]);
        // Empty inputs are ignored rather than stored.
        memory.record("", McpDefinitionKind::Tool, "echo", [4u8; 32]);
        memory.record("s1", McpDefinitionKind::Tool, "", [4u8; 32]);
        memory.record("s1", McpDefinitionKind::Unspecified, "echo", [4u8; 32]);
        assert_eq!(memory.sizes(), (2, 3));
        assert_eq!(
            memory.last("s1", McpDefinitionKind::Tool, "echo"),
            Some([1u8; 32])
        );
        assert_eq!(memory.last("s1", McpDefinitionKind::Tool, "greet"), None);
        assert_eq!(memory.last("s3", McpDefinitionKind::Tool, "echo"), None);
        // The latest observation of one name replaces the earlier hash.
        memory.record("s1", McpDefinitionKind::Tool, "echo", [5u8; 32]);
        assert_eq!(
            memory.last("s1", McpDefinitionKind::Tool, "echo"),
            Some([5u8; 32])
        );
        assert_eq!(memory.sizes(), (2, 3), "replacing a name keeps the size");
        // A snapshot that no longer configures s2 drops its entries.
        memory.retain_servers(["s1"]);
        assert_eq!(memory.sizes(), (1, 2));
        assert_eq!(memory.last("s2", McpDefinitionKind::Tool, "echo"), None);
        memory.retain_servers([]);
        assert_eq!(memory.sizes(), (0, 0));
        // Per-server bound: one more name than the snapshot may approve.
        for i in 0..=MAX_APPROVED_DEFINITIONS {
            memory.record("s1", McpDefinitionKind::Tool, &format!("t{i}"), [1u8; 32]);
        }
        assert_eq!(memory.sizes(), (1, MAX_APPROVED_DEFINITIONS));
    }

    #[test]
    fn review_decisions_follow_the_mode_table() {
        // off: serve everything, report nothing.
        for (known, hash) in [(false, false), (true, false), (true, true)] {
            assert_eq!(
                review(ReviewMode::Off, known, hash),
                Review {
                    serve: true,
                    report: false,
                    auto_approved: false
                }
            );
        }
        // manual: only approved hashes, everything else withheld and reported.
        assert_eq!(
            review(ReviewMode::Manual, true, true),
            Review {
                serve: true,
                report: false,
                auto_approved: false
            }
        );
        for (known, hash) in [(false, false), (true, false)] {
            assert_eq!(
                review(ReviewMode::Manual, known, hash),
                Review {
                    serve: false,
                    report: true,
                    auto_approved: false
                }
            );
        }
        // tofu: approved hash served silently, changed definition withheld,
        // first sight served and auto-approved.
        assert_eq!(
            review(ReviewMode::Tofu, true, true),
            Review {
                serve: true,
                report: false,
                auto_approved: false
            }
        );
        assert_eq!(
            review(ReviewMode::Tofu, true, false),
            Review {
                serve: false,
                report: true,
                auto_approved: false
            }
        );
        assert_eq!(
            review(ReviewMode::Tofu, false, false),
            Review {
                serve: true,
                report: true,
                auto_approved: true
            }
        );
    }

    #[tokio::test]
    async fn changes_wake_the_config_stream() {
        let registry = Registry::new();
        assert!(
            tokio::time::timeout(Duration::from_millis(50), registry.changed())
                .await
                .is_err(),
            "no permit before anything was observed"
        );
        registry.observe_at(1, &observed("s1", 1, "{}"));
        tokio::time::timeout(Duration::from_secs(1), registry.changed())
            .await
            .expect("a queued definition wakes the stream");
        // Bursts coalesce into one permit; the next change still wakes.
        drain(&registry);
        registry.card_at(2, &card("a1", "{}", ""));
        registry.card_at(2, &card("a2", "{}", ""));
        tokio::time::timeout(Duration::from_secs(1), registry.changed())
            .await
            .expect("a queued card wakes the stream");
    }

    #[test]
    fn the_registry_is_shareable_across_tasks() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<Registry>();
        assert_send_sync::<crate::targets::Targets>();
        assert_send_sync::<crate::targets::McpTarget>();
        assert_send_sync::<crate::targets::A2aTarget>();
        assert_send_sync::<crate::grants::Grants>();
        assert_send_sync::<crate::a2a_ids::IdKey>();
    }

    #[test]
    fn truncate_keeps_utf8_valid() {
        assert_eq!(truncate("abc", 10), "abc");
        assert_eq!(truncate("abcdef", 3), "abc");
        assert_eq!(truncate("a錯", 2), "a");
        assert_eq!(truncate("錯", 1), "");
        assert_eq!(truncate("", 0), "");
    }
}

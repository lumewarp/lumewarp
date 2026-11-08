//! What the gateway learned from upstream and has to tell the control plane:
//! MCP tool/prompt definitions and A2A agent cards, bounded and deduplicated
//! (`docs/plugins.md`, "Catalog reports").
//!
//! The control plane cannot fetch these itself when a server uses a WASM auth
//! plugin or a client's token, so the gateway reports them in `CatalogReport` on
//! the config stream. Everything upstream sends is untrusted:
//!
//! - a definition or card over 64 KiB is dropped and counted;
//! - at most 200 unreported definitions are held per server (and 200 cards);
//! - the same (server, hash) is queued at most once per 10 minutes;
//! - nothing is reported twice while it waits in a report.
//!
//! Like `health::Registry`, the registry lives in `Runtime` for the life of the
//! process: the config stream waits on [`Registry::changed`] and drains a report
//! with [`Registry::drain_report`]. Reports add to what was reported before, so
//! draining never loses older entries.

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
use sha2::{Digest, Sha256};
use tokio::sync::Notify;

use crate::targets::ReviewMode;

/// Unreported definitions held per server.
pub const MAX_PENDING_PER_SERVER: usize = 200;

/// Largest definition or agent card the gateway reports (canonical JSON bytes).
pub const MAX_DEFINITION_BYTES: usize = 64 * 1024;
pub const MAX_CARD_BYTES: usize = 64 * 1024;

/// The same hash is reported at most once per gateway in this window.
pub const REPORT_INTERVAL_MS: i64 = 10 * 60 * 1000;

/// Distinct agents with an unreported card.
pub const MAX_PENDING_AGENTS: usize = 200;

/// Fetched cards hold a single upstream error string; it is truncated here
/// rather than reported at an unbounded size.
pub const MAX_ERROR_BYTES: usize = 1024;

/// Dedupe entries kept before stale ones are pruned.
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
    pub kind: McpDefinitionKind,
    /// Upstream name, without the `<server>__` prefix.
    pub name: &'a str,
    /// JCS canonical JSON of the projection (`mcp_hash::Observed::canonical_json`).
    pub canonical_json: &'a str,
    /// `mcp_hash::Observed::hash`.
    pub hash: [u8; 32],
    /// The gateway serves it (a TOFU first sight) rather than withholding it.
    pub auto_approved: bool,
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
    /// (server, hash) -> unix ms it was last queued for a report.
    seen: HashMap<(String, [u8; 32]), i64>,
    /// Latest card per agent; a newer fetch replaces an undrained one.
    cards: HashMap<String, AgentCardReport>,
    /// (agent, digest) -> unix ms it was last queued for a report.
    card_seen: HashMap<(String, [u8; 32]), i64>,
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

    /// Everything queued since the last call, and an empty queue afterwards.
    pub fn drain_report(&self) -> CatalogReport {
        let mut inner = lock_inner(&self.inner);
        let definitions: Vec<ObservedDefinition> = inner.definitions.drain(..).collect();
        inner.pending_per_server.clear();
        for definition in &definitions {
            if let Ok(hash) = <[u8; 32]>::try_from(definition.hash.as_slice()) {
                inner.queued.remove(&(definition.server.clone(), hash));
            }
        }
        let mut cards: Vec<AgentCardReport> = inner.cards.drain().map(|(_, card)| card).collect();
        cards.sort_by(|a, b| a.agent.cmp(&b.agent));
        CatalogReport { definitions, cards }
    }

    /// Definitions and cards waiting for the next report.
    pub fn pending(&self) -> usize {
        let inner = lock_inner(&self.inner);
        inner.definitions.len() + inner.cards.len()
    }

    /// Queues a definition for the next report, unless it was already reported
    /// or queued within [`REPORT_INTERVAL_MS`].
    pub fn observe(&self, observation: &Observation<'_>) -> Recorded {
        self.observe_at(now_ms(), observation)
    }

    /// [`Self::observe`] with a caller-supplied clock (tests).
    pub fn observe_at(&self, now: i64, observation: &Observation<'_>) -> Recorded {
        let Observation {
            server,
            kind,
            name,
            canonical_json,
            hash,
            auto_approved,
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
        inner.seen.insert(key.clone(), now);
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
        });
        if inner.seen.len() > MAX_TRACKED {
            inner
                .seen
                .retain(|_, at| now.saturating_sub(*at) < REPORT_INTERVAL_MS);
        }
        drop(inner);
        self.changed.notify_one();
        Recorded::Queued
    }

    /// Queues an agent card (or the error of a failed fetch) for the next
    /// report. The latest card of an agent replaces an older, still unreported
    /// one: the control plane keeps the last card anyway.
    pub fn card(&self, agent: &str, card_json: &str, error: &str) -> Recorded {
        self.card_at(now_ms(), agent, card_json, error)
    }

    /// [`Self::card`] with a caller-supplied clock (tests).
    pub fn card_at(&self, now: i64, agent: &str, card_json: &str, error: &str) -> Recorded {
        if agent.is_empty() || (card_json.is_empty() && error.is_empty()) {
            return self.drop(DropReason::Invalid);
        }
        if card_json.len() > MAX_CARD_BYTES {
            return self.drop(DropReason::Oversized);
        }
        let mut digest = Sha256::new();
        digest.update(card_json.as_bytes());
        digest.update([0u8]);
        digest.update(error.as_bytes());
        let digest: [u8; 32] = digest.finalize().into();
        let key = (agent.to_owned(), digest);
        let mut inner = lock_inner(&self.inner);
        if let Some(&at) = inner.card_seen.get(&key) {
            if now.saturating_sub(at) < REPORT_INTERVAL_MS {
                return Recorded::Suppressed;
            }
        }
        if !inner.cards.contains_key(agent) && inner.cards.len() >= MAX_PENDING_AGENTS {
            return self.drop(DropReason::PendingLimit);
        }
        inner.card_seen.insert(key, now);
        inner.cards.insert(
            agent.to_owned(),
            AgentCardReport {
                agent: agent.to_owned(),
                card_json: card_json.to_owned(),
                error: truncate(error, MAX_ERROR_BYTES),
                fetched_at_unix_ms: now,
            },
        );
        if inner.card_seen.len() > MAX_TRACKED {
            inner
                .card_seen
                .retain(|_, at| now.saturating_sub(*at) < REPORT_INTERVAL_MS);
        }
        drop(inner);
        self.changed.notify_one();
        Recorded::Queued
    }

    /// (definition, card) dedupe entries held; proves pruning bounds memory.
    #[cfg(test)]
    fn tracked(&self) -> (usize, usize) {
        let inner = lock_inner(&self.inner);
        (inner.seen.len(), inner.card_seen.len())
    }

    fn drop(&self, reason: DropReason) -> Recorded {
        if let Some(drops) = &self.drops {
            drops.catalog_dropped(reason.label());
        }
        Recorded::Dropped(reason)
    }
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
/// `known_approved`: the snapshot has an approved definition of this name.
/// `hash_approved`: it has *this* hash. The caller takes both from
/// `targets::Approved` (`has_name` / `accepts`).
pub fn review(mode: ReviewMode, known_approved: bool, hash_approved: bool) -> Review {
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
            } else if known_approved {
                // A changed definition of a known name: withheld until the
                // control plane approves the new hash.
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn observed<'a>(server: &'a str, hash: u8, json: &'a str) -> Observation<'a> {
        Observation {
            server,
            kind: McpDefinitionKind::Tool,
            name: "echo",
            canonical_json: json,
            hash: [hash; 32],
            auto_approved: false,
        }
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
        let report = registry.drain_report();
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
        let empty = registry.drain_report();
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
        assert_eq!(registry.drain_report().definitions.len(), 1);
    }

    #[test]
    fn the_same_hash_is_reported_at_most_once_per_ten_minutes() {
        let registry = Registry::new();
        assert_eq!(
            registry.observe_at(0, &observed("s1", 1, "{}")),
            Recorded::Queued
        );
        registry.drain_report();
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
        let report = registry.drain_report();
        assert_eq!(report.definitions.len(), 3);
    }

    #[test]
    fn a_server_holds_at_most_200_unreported_definitions() {
        let registry = Registry::new();
        let hashes = hash_of(MAX_PENDING_PER_SERVER + 1);
        for hash in &hashes[..MAX_PENDING_PER_SERVER] {
            let observation = Observation {
                server: "s1",
                kind: McpDefinitionKind::Tool,
                name: "tool",
                canonical_json: "{}",
                hash: *hash,
                auto_approved: false,
            };
            assert_eq!(registry.observe_at(0, &observation), Recorded::Queued);
        }
        let observation = Observation {
            server: "s1",
            kind: McpDefinitionKind::Tool,
            name: "tool",
            canonical_json: "{}",
            hash: hashes[MAX_PENDING_PER_SERVER],
            auto_approved: false,
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
        registry.drain_report();
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
            registry.card_at(0, "a1", &card_at_limit, ""),
            Recorded::Queued
        );
        let card_over = "y".repeat(MAX_CARD_BYTES + 1);
        assert_eq!(
            registry.card_at(0, "a1", &card_over, ""),
            Recorded::Dropped(DropReason::Oversized)
        );
        let report = registry.drain_report();
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
            registry.card_at(0, "", "{}", ""),
            Recorded::Dropped(DropReason::Invalid)
        );
        assert_eq!(
            registry.card_at(0, "a1", "", ""),
            Recorded::Dropped(DropReason::Invalid)
        );
        assert_eq!(registry.pending(), 0);
    }

    #[test]
    fn cards_keep_the_latest_fetch_and_dedupe() {
        let registry = Registry::new();
        assert_eq!(
            registry.card_at(0, "a2", r#"{"name":"a2"}"#, ""),
            Recorded::Queued
        );
        assert_eq!(
            registry.card_at(0, "a2", r#"{"name":"a2"}"#, ""),
            Recorded::Suppressed
        );
        // Unchanged content after the window is reported again.
        assert_eq!(
            registry.card_at(REPORT_INTERVAL_MS, "a2", r#"{"name":"a2"}"#, ""),
            Recorded::Queued
        );
        // A newer card replaces the queued one.
        assert_eq!(
            registry.card_at(REPORT_INTERVAL_MS, "a2", r#"{"name":"a2b"}"#, ""),
            Recorded::Queued
        );
        assert_eq!(
            registry.card_at(0, "a1", "", "connect refused"),
            Recorded::Queued
        );
        let report = registry.drain_report();
        assert_eq!(report.cards.len(), 2);
        assert_eq!(report.cards[0].agent, "a1");
        assert_eq!(report.cards[0].card_json, "");
        assert_eq!(report.cards[0].error, "connect refused");
        assert_eq!(report.cards[1].agent, "a2");
        assert_eq!(report.cards[1].card_json, r#"{"name":"a2b"}"#);
        assert_eq!(report.cards[1].fetched_at_unix_ms, REPORT_INTERVAL_MS);
    }

    #[test]
    fn card_errors_are_truncated_on_a_char_boundary() {
        let registry = Registry::new();
        let error = "錯".repeat(MAX_ERROR_BYTES);
        assert_eq!(registry.card_at(0, "a1", "", &error), Recorded::Queued);
        let report = registry.drain_report();
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
                registry.card_at(0, &format!("a{i}"), "{}", ""),
                Recorded::Queued
            );
        }
        assert_eq!(
            registry.card_at(0, "one-too-many", "{}", ""),
            Recorded::Dropped(DropReason::PendingLimit)
        );
        assert_eq!(registry.drain_report().cards.len(), MAX_PENDING_AGENTS);
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
                kind: McpDefinitionKind::Tool,
                name: "tool",
                canonical_json: "{}",
                hash: *hash,
                auto_approved: false,
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
    fn dedupe_entries_are_pruned() {
        let registry = Registry::new();
        // Fill past the tracking bound at t=0. The pending queue is drained so
        // the per-server cap never interferes.
        for i in 0..=MAX_TRACKED {
            let mut hash = [0u8; 32];
            hash[..8].copy_from_slice(&(i as u64).to_le_bytes());
            let observation = Observation {
                server: "s1",
                kind: McpDefinitionKind::Tool,
                name: "tool",
                canonical_json: "{}",
                hash,
                auto_approved: false,
            };
            let _ = registry.observe_at(0, &observation);
            if registry.pending() >= MAX_PENDING_PER_SERVER {
                registry.drain_report();
            }
        }
        assert!(registry.tracked().0 > MAX_TRACKED);
        // One observation after the window prunes every stale entry; only the
        // new one stays.
        let _ = registry.observe_at(REPORT_INTERVAL_MS, &observed("s1", 1, "{}"));
        assert_eq!(registry.tracked().0, 1, "stale dedupe entries are pruned");
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
        registry.drain_report();
        registry.card_at(2, "a1", "{}", "");
        registry.card_at(2, "a2", "{}", "");
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

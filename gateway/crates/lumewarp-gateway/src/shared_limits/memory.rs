//! In-memory store for unit tests.
//!
//! It mirrors the Redis script's semantics (per-minute per-gateway counts,
//! gateway counts, in-flight fields that go stale) without a server, and
//! shares state through a [`MemoryHub`] so several stores can act as several
//! gateways.

use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
};

use crate::ratelimit::{ScopeCounts, ScopeView};

use super::{Error, Store};

const STALE_IN_FLIGHT_MS: u64 = 3_000;

#[derive(Clone, Default)]
pub struct MemoryHub {
    state: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    /// `(namespace, scope, dimension, minute)` -> gateway -> count.
    counts: HashMap<(String, String, &'static str, u64), HashMap<String, i64>>,
    /// `(namespace, scope)` -> gateway -> `(in_flight, unix-ish ms)`.
    in_flight: HashMap<(String, String), HashMap<String, (u32, u64)>>,
    /// The test clock; only in-flight freshness depends on it.
    now_ms: u64,
    failing: bool,
}

impl MemoryHub {
    pub fn store(&self, namespace: &str, gateway: &str) -> MemoryStore {
        MemoryStore {
            hub: self.clone(),
            namespace: namespace.to_owned(),
            gateway: gateway.to_owned(),
        }
    }

    /// Moves the test clock; use it to age in-flight entries past 3 s.
    pub fn advance(&self, ms: u64) {
        let mut state = self.state.lock().unwrap();
        state.now_ms = state.now_ms.saturating_add(ms);
    }

    /// Makes the next syncs fail without touching the state, so the retry
    /// path can be exercised.
    pub fn fail(&self, failing: bool) {
        self.state.lock().unwrap().failing = failing;
    }
}

#[derive(Clone)]
pub struct MemoryStore {
    hub: MemoryHub,
    namespace: String,
    gateway: String,
}

impl Store for MemoryStore {
    async fn sync(&self, batch: &[ScopeCounts]) -> Result<Vec<ScopeView>, Error> {
        self.sync_inner(batch)
    }
}

impl MemoryStore {
    fn sync_inner(&self, batch: &[ScopeCounts]) -> Result<Vec<ScopeView>, Error> {
        let mut state = self.hub.state.lock().unwrap();
        if state.failing {
            return Err(Error::connect("in-memory store is failing"));
        }
        if batch.is_empty() {
            return Ok(Vec::new());
        }
        let now = state.now_ms;
        for counts in batch {
            let scope = counts.id.to_string();
            if counts.write {
                // Absolute values: a retried batch overwrites, never adds.
                let prev_minute = counts.minute.saturating_sub(1);
                for (dimension, minute, value) in [
                    ("r", prev_minute, counts.rpm_prev),
                    ("r", counts.minute, counts.rpm_cur),
                    ("t", prev_minute, counts.tpm_prev),
                    ("t", counts.minute, counts.tpm_cur),
                ] {
                    state
                        .counts
                        .entry((self.namespace.clone(), scope.clone(), dimension, minute))
                        .or_default()
                        .insert(
                            self.gateway.clone(),
                            i64::try_from(value).unwrap_or(i64::MAX),
                        );
                }
            }
            state
                .in_flight
                .entry((self.namespace.clone(), scope))
                .or_default()
                .insert(self.gateway.clone(), (counts.in_flight, now));
        }
        let views = batch
            .iter()
            .map(|counts| self.view(&state, counts))
            .collect();
        Ok(views)
    }

    fn view(&self, state: &State, counts: &ScopeCounts) -> ScopeView {
        let scope = counts.id.to_string();
        let prev_minute = counts.minute.saturating_sub(1);
        let totals = |dimension: &'static str| {
            let role = |minute: u64| {
                state
                    .counts
                    .get(&(self.namespace.clone(), scope.clone(), dimension, minute))
                    .map(|per| {
                        per.iter()
                            .filter(|(gateway, count)| *gateway != &self.gateway && **count > 0)
                            .fold(0u64, |total, (_, count)| {
                                total.saturating_add(u64::try_from(*count).unwrap_or(0))
                            })
                    })
                    .unwrap_or(0)
            };
            let mut seen = HashSet::new();
            let mut gateways = 0u32;
            for minute in [prev_minute, counts.minute] {
                if let Some(per) =
                    state
                        .counts
                        .get(&(self.namespace.clone(), scope.clone(), dimension, minute))
                {
                    for (gateway, count) in per {
                        if *count > 0 && seen.insert(gateway.clone()) {
                            gateways += 1;
                        }
                    }
                }
            }
            (role(prev_minute), role(counts.minute), gateways)
        };
        let (rpm_prev, rpm_cur, rpm_gateways) = totals("r");
        let (tpm_prev, tpm_cur, tpm_gateways) = totals("t");

        let mut in_flight = 0u32;
        let mut in_flight_gateways = 0u32;
        if let Some(per) = state
            .in_flight
            .get(&(self.namespace.clone(), scope.clone()))
        {
            for (gateway, (count, stamped)) in per {
                if state.now_ms.saturating_sub(*stamped) > STALE_IN_FLIGHT_MS {
                    continue;
                }
                if gateway != &self.gateway {
                    in_flight = in_flight.saturating_add(*count);
                }
                in_flight_gateways += 1;
            }
        }
        ScopeView {
            id: counts.id.clone(),
            minute: counts.minute,
            rpm_prev,
            rpm_cur,
            tpm_prev,
            tpm_cur,
            rpm_gateways,
            tpm_gateways,
            in_flight,
            in_flight_gateways,
        }
    }
}

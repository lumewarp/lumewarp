//! Version 1 of the compiled-in routing strategy API.
//!
//! `configure` runs once per route on snapshot application. `pick` must not do
//! I/O, block, or allocate in the common case. Given the same candidate state,
//! request and RNG stream it must be deterministic. Return `None` only when no
//! candidate is acceptable; the gateway then tries its existing fall-through
//! paths. Deployment eligibility, retries, splits and breakers belong to the
//! gateway, not to a strategy. Candidates expose eligible deployments only.
//!
//! Request headers are readable only when named in `params.headers` (an array
//! of HTTP header names). There is deliberately no prompt or body interface.
//! Strategies are linked at compile time with [`register!`]; inventory avoids
//! requiring edits to the gateway's dispatcher when adding a strategy crate.

use std::ops::Range;

pub const API_VERSION: u32 = 1;
pub type Params = serde_json::Value;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Needs {
    pub latency: bool,
    pub vllm_load: bool,
}

pub struct RouteInfo<'a> {
    pub name: &'a str,
    pub deployments: &'a [&'a str],
}

/// Range draws, rather than a particular RNG implementation or version.
/// The gateway adapter preserves its pre-interface fastrand draws exactly.
pub trait RngCore {
    fn u64(&mut self, range: Range<u64>) -> u64;
    fn usize(&mut self, range: Range<usize>) -> usize;
    fn u32(&mut self, range: Range<u32>) -> u32;
}

pub struct ThreadRng;

impl RngCore for ThreadRng {
    fn u64(&mut self, range: Range<u64>) -> u64 {
        fastrand::u64(range)
    }
    fn usize(&mut self, range: Range<usize>) -> usize {
        fastrand::usize(range)
    }
    fn u32(&mut self, range: Range<u32>) -> u32 {
        fastrand::u32(range)
    }
}

/// The lookup itself is private: callers cannot bypass the allowlist.
pub struct RequestInfo<'a> {
    pub requested: &'a str,
    pub team_id: &'a str,
    pub key_id: &'a str,
    pub estimated_input_tokens: Option<u64>,
    allowed_headers: &'a [String],
    lookup: &'a dyn Fn(&str) -> Option<&'a [u8]>,
}

impl<'a> RequestInfo<'a> {
    #[inline]
    pub fn new(
        requested: &'a str,
        team_id: &'a str,
        key_id: &'a str,
        estimated_input_tokens: Option<u64>,
        allowed_headers: &'a [String],
        lookup: &'a dyn Fn(&str) -> Option<&'a [u8]>,
    ) -> Self {
        Self {
            requested,
            team_id,
            key_id,
            estimated_input_tokens,
            allowed_headers,
            lookup,
        }
    }

    pub fn header(&self, name: &str) -> Option<&'a [u8]> {
        self.allowed_headers
            .iter()
            .any(|h| h.eq_ignore_ascii_case(name))
            .then(|| (self.lookup)(name))
            .flatten()
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Load {
    pub waiting: u64,
    /// KV usage in basis points; built-ins compare 5% buckets.
    pub kv_usage_bp: u32,
    pub fresh: bool,
}

/// Read-only backing state. The gateway lends this for the duration of a pick.
/// Strategies receive only `Candidates`, which applies the eligibility filter.
pub trait CandidateSource {
    fn len(&self) -> usize;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    fn name(&self, index: usize) -> &str;
    fn weight(&self, index: usize) -> u64;
    fn in_flight(&self, index: usize) -> usize;
    fn latency_ewma_us(&self, index: usize) -> Option<u64>;
    fn load(&self, index: usize) -> Load;
    fn total_weight(&self) -> u64;
    fn weighted_index(&self, draw: u64) -> usize;
}

pub struct Candidate<'a, S: CandidateSource + ?Sized = dyn CandidateSource + 'a> {
    /// Index in the current pool, not in RouteInfo's route-wide deployment list.
    pub index: usize,
    source: &'a S,
}

impl<S: CandidateSource + ?Sized> Copy for Candidate<'_, S> {}
impl<S: CandidateSource + ?Sized> Clone for Candidate<'_, S> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<S: CandidateSource + ?Sized> Candidate<'_, S> {
    #[inline]
    pub fn name(&self) -> &str {
        self.source.name(self.index)
    }
    #[inline]
    pub fn weight(&self) -> u64 {
        self.source.weight(self.index)
    }
    #[inline]
    pub fn in_flight(&self) -> usize {
        self.source.in_flight(self.index)
    }
    #[inline]
    pub fn latency_ewma_us(&self) -> Option<u64> {
        self.source.latency_ewma_us(self.index)
    }
    #[inline]
    pub fn load(&self) -> Load {
        self.source.load(self.index)
    }
}

pub struct Candidates<
    'a,
    S: CandidateSource + ?Sized = dyn CandidateSource + 'a,
    E: Fn(usize) -> bool + ?Sized = dyn Fn(usize) -> bool + 'a,
> {
    source: &'a S,
    eligible: &'a E,
}

impl<'a, S: CandidateSource + ?Sized, E: Fn(usize) -> bool + ?Sized> Candidates<'a, S, E> {
    #[inline]
    pub fn erased(&self) -> Candidates<'a>
    where
        S: Sized,
        E: Sized,
    {
        Candidates::new(self.source, self.eligible)
    }

    #[inline]
    pub fn new(source: &'a S, eligible: &'a E) -> Self {
        Self { source, eligible }
    }

    #[inline]
    pub fn iter(&self) -> impl Iterator<Item = Candidate<'a, S>> + '_ {
        (0..self.source.len())
            .filter(|&i| (self.eligible)(i))
            .map(|index| Candidate {
                index,
                source: self.source,
            })
    }

    #[inline]
    pub fn get(&self, index: usize) -> Option<Candidate<'a, S>> {
        (index < self.source.len() && (self.eligible)(index)).then_some(Candidate {
            index,
            source: self.source,
        })
    }

    #[inline]
    pub fn random(&self, rng: &mut (impl RngCore + ?Sized)) -> Option<usize> {
        let count = self.iter().count();
        if count == 0 {
            return None;
        }
        self.iter().nth(rng.usize(0..count)).map(|d| d.index)
    }

    #[inline]
    pub fn two_choices(
        &self,
        rng: &mut (impl RngCore + ?Sized),
        better: impl Fn(Candidate<'a, S>, Candidate<'a, S>) -> bool,
    ) -> Option<usize> {
        let count = self.iter().count();
        match count {
            0 => None,
            1 => self.iter().next().map(|d| d.index),
            _ => {
                let a = rng.usize(0..count);
                let mut b = rng.usize(0..count - 1);
                if b >= a {
                    b += 1;
                }
                let a = self.iter().nth(a)?;
                let b = self.iter().nth(b)?;
                Some(if better(b, a) { b.index } else { a.index })
            }
        }
    }

    #[inline]
    pub fn weighted(&self, rng: &mut (impl RngCore + ?Sized)) -> Option<usize> {
        let total: u64 = self.iter().map(|d| d.weight()).sum();
        if total == 0 {
            return None;
        }
        if total == self.source.total_weight() {
            return Some(self.source.weighted_index(rng.u64(0..total)));
        }
        let mut draw = rng.u64(0..total);
        self.iter()
            .find(|d| {
                let weight = d.weight();
                if draw < weight {
                    return true;
                }
                draw -= weight;
                false
            })
            .map(|d| d.index)
    }
}

pub trait Strategy: Send + Sync + 'static {
    fn name(&self) -> &'static str;
    fn needs(&self) -> Needs;
    fn configure(route: &RouteInfo<'_>, params: &Params) -> Result<Self, ConfigError>
    where
        Self: Sized;
    fn pick(
        &self,
        req: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        rng: &mut dyn RngCore,
    ) -> Option<usize>;
}

pub type CreateStrategy = fn(&RouteInfo<'_>, &Params) -> Result<Box<dyn Strategy>, ConfigError>;

pub struct StrategyFactory {
    pub name: &'static str,
    pub create: CreateStrategy,
}

inventory::collect!(StrategyFactory);
#[doc(hidden)]
pub use inventory as __inventory;

#[macro_export]
macro_rules! register {
    ($name:literal, $strategy:ty) => {
        $crate::__inventory::submit! {
            $crate::StrategyFactory {
                name: $name,
                create: |route, params| {
                    <$strategy as $crate::Strategy>::configure(route, params)
                        .map(|s| Box::new(s) as Box<dyn $crate::Strategy>)
                },
            }
        }
    };
}

pub fn registered() -> impl Iterator<Item = &'static StrategyFactory> {
    inventory::iter::<StrategyFactory>.into_iter()
}

pub fn lookup(name: &str) -> Result<Option<&'static StrategyFactory>, ConfigError> {
    let mut matches = registered().filter(|factory| factory.name == name);
    let first = matches.next();
    if matches.next().is_some() {
        return Err(ConfigError(format!(
            "duplicate strategy registration {name:?}"
        )));
    }
    Ok(first)
}

/// Inline enum dispatch for stateless built-ins; custom instances alone are boxed.
#[derive(Clone, Copy, Debug)]
pub enum Builtin {
    Weighted,
    LeastInFlight,
    LowestLatency,
    VllmLoad,
}

macro_rules! builtin_strategy {
    ($type:ident, $variant:ident) => {
        pub struct $type;
        impl Strategy for $type {
            fn name(&self) -> &'static str {
                Builtin::$variant.name()
            }
            fn needs(&self) -> Needs {
                Builtin::$variant.needs()
            }
            fn configure(_: &RouteInfo<'_>, _: &Params) -> Result<Self, ConfigError> {
                Ok(Self)
            }
            fn pick(
                &self,
                req: &RequestInfo<'_>,
                c: &Candidates<'_>,
                rng: &mut dyn RngCore,
            ) -> Option<usize> {
                Builtin::$variant.pick(req, c, rng)
            }
        }
    };
}

builtin_strategy!(Weighted, Weighted);
builtin_strategy!(LeastInFlight, LeastInFlight);
builtin_strategy!(LowestLatency, LowestLatency);
builtin_strategy!(VllmLoad, VllmLoad);

impl Strategy for Builtin {
    fn name(&self) -> &'static str {
        match self {
            Self::Weighted => "weighted",
            Self::LeastInFlight => "least_in_flight",
            Self::LowestLatency => "lowest_latency",
            Self::VllmLoad => "vllm_load",
        }
    }

    fn needs(&self) -> Needs {
        Needs {
            latency: matches!(self, Self::LowestLatency),
            vllm_load: matches!(self, Self::VllmLoad),
        }
    }

    fn configure(_: &RouteInfo<'_>, _: &Params) -> Result<Self, ConfigError> {
        Err(ConfigError("select a built-in by its enum variant".into()))
    }

    fn pick(
        &self,
        _: &RequestInfo<'_>,
        c: &Candidates<'_>,
        rng: &mut dyn RngCore,
    ) -> Option<usize> {
        self.pick_from(c, rng)
    }
}

impl Builtin {
    /// Same implementation as Strategy::pick, monomorphized for built-in gateway state.
    #[inline]
    pub fn pick_from<S: CandidateSource + ?Sized, E: Fn(usize) -> bool + ?Sized>(
        &self,
        c: &Candidates<'_, S, E>,
        rng: &mut (impl RngCore + ?Sized),
    ) -> Option<usize> {
        match self {
            Self::Weighted => c.weighted(rng),
            Self::LeastInFlight => {
                let len = c.source.len();
                if len == 0 {
                    return None;
                }
                let start = rng.usize(0..len);
                (0..len)
                    .map(|offset| (start + offset) % len)
                    .filter_map(|i| c.get(i))
                    .min_by_key(|d| d.in_flight())
                    .map(|d| d.index)
            }
            Self::LowestLatency => {
                if rng.u32(0..100) < 5 {
                    return c.random(rng);
                }
                let cost = |d: Candidate<'_, S>| {
                    u128::from(d.latency_ewma_us().unwrap_or(0)) * (d.in_flight() as u128 + 1)
                };
                c.two_choices(rng, |a, b| cost(a) < cost(b))
            }
            Self::VllmLoad => c.two_choices(rng, |a, b| {
                let (la, lb) = (a.load(), b.load());
                if la.fresh && lb.fresh {
                    (la.waiting, la.kv_usage_bp / 500, a.in_flight())
                        < (lb.waiting, lb.kv_usage_bp / 500, b.in_flight())
                } else {
                    a.in_flight() < b.in_flight()
                }
            }),
        }
    }
}

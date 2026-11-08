//! Native, allocation-free candidate views and built-in router dispatch.
//!
//! Custom policies are WASM components authored with `lumewarp-plugin-sdk`,
//! never gateway feature flags or linked strategy crates. The SDK reuses these
//! read-only policy views inside the guest. Registration remains an internal
//! mechanism for built-in metadata and isolated policy tests; the production
//! gateway accepts only built-ins and components from its startup registry.
//!
//! Eligibility, retries, splits and breakers belong to the gateway. Headers
//! are available only through the configured allowlist; bodies are not exposed.

use lumewarp_plugin_schema as schema;

use std::ops::Range;

use schemars::JsonSchema;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub use schema::{assert_schema_subset, schema_for, subset_problem, MAX_SCHEMA_BYTES};

pub const API_VERSION: u32 = 2;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

impl ConfigError {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Needs {
    pub latency: bool,
    pub vllm_load: bool,
}

impl Needs {
    fn names(self) -> Vec<&'static str> {
        let mut names = Vec::new();
        if self.latency {
            names.push("latency");
        }
        if self.vllm_load {
            names.push("vllm_load");
        }
        names
    }
}

/// One member entry of a router. With a traffic split, the same model can
/// appear once per split, as separate entries with their own settings.
#[derive(Clone, Copy, Debug)]
pub struct MemberInfo<'a> {
    pub model: &'a str,
    pub weight: u32,
    /// `None` without a traffic split.
    pub split: Option<&'a str>,
}

pub struct RouterInfo<'a> {
    pub name: &'a str,
    /// Every member across all splits, in declaration order. A position here
    /// is what [`Candidate::member_index`] returns.
    pub members: &'a [MemberInfo<'a>],
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

    pub fn headers(&self) -> impl Iterator<Item = (&str, &'a [u8])> {
        self.allowed_headers
            .iter()
            .filter_map(|name| (self.lookup)(name).map(|value| (name.as_str(), value)))
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
    /// Position of pool entry `index` in [`RouterInfo::members`].
    fn member_index(&self, index: usize) -> usize;
    fn weight(&self, index: usize) -> u64;
    fn in_flight(&self, index: usize) -> usize;
    fn latency_ewma_us(&self, index: usize) -> Option<u64>;
    fn load(&self, index: usize) -> Load;
    fn total_weight(&self) -> u64;
    fn weighted_index(&self, draw: u64) -> usize;
}

pub struct Candidate<'a, S: CandidateSource + ?Sized = dyn CandidateSource + 'a> {
    /// Index in the current pool, not stable across traffic splits: return
    /// this from `pick`, and key `configure`'s tables by [`Self::member_index`].
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
    /// Position in [`RouterInfo::members`] and in `configure`'s member params,
    /// the same whichever traffic-split pool the candidate is in.
    #[inline]
    pub fn member_index(&self) -> usize {
        self.source.member_index(self.index)
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
    fn pick(
        &self,
        req: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        rng: &mut dyn RngCore,
    ) -> Option<usize>;
}

/// Builds a strategy from its typed settings. `P` and `M` are the `params` and
/// `member_params` types named in [`register!`], so what is validated and what
/// is read are the same structs. `members[i]` belongs to `router.members[i]`.
pub trait Configure<P, M>: Strategy + Sized {
    fn configure(router: &RouterInfo<'_>, params: P, members: &[M]) -> Result<Self, ConfigError>;
}

/// `params` has already lost the reserved `headers` field. `member_params`
/// holds one JSON object per [`RouterInfo::members`] entry.
pub type CreateStrategy =
    fn(&RouterInfo<'_>, &Value, &[Value]) -> Result<Box<dyn Strategy>, ConfigError>;

/// What [`register!`] submits. Function pointers, because schemas and `Needs`
/// are not constants; none of them runs on the request path.
pub struct StrategyFactory {
    pub name: &'static str,
    pub title: &'static str,
    pub description: &'static str,
    pub uses_weight: bool,
    pub needs: fn() -> Needs,
    pub params_schema: fn() -> Value,
    pub member_schema: fn() -> Value,
    pub create: CreateStrategy,
}

inventory::collect!(StrategyFactory);
#[doc(hidden)]
pub use inventory as __inventory;

#[doc(hidden)]
pub fn __create<S, P, M>(
    router: &RouterInfo<'_>,
    params: &Value,
    members: &[Value],
) -> Result<Box<dyn Strategy>, ConfigError>
where
    S: Configure<P, M>,
    P: DeserializeOwned,
    M: DeserializeOwned,
{
    let params = P::deserialize(params).map_err(|e| ConfigError(format!("params: {e}")))?;
    let members = members
        .iter()
        .map(M::deserialize)
        .collect::<Result<Vec<M>, _>>()
        .map_err(|e| ConfigError(format!("member params: {e}")))?;
    S::configure(router, params, &members).map(|s| Box::new(s) as Box<dyn Strategy>)
}

#[doc(hidden)]
pub fn __schema<T: JsonSchema>() -> Value {
    schema_for::<T>()
}

/// Declares a strategy. Generates a test that fails the strategy crate's
/// `cargo test` when a schema leaves the subset, so use at most one `register!`
/// per module. See `docs/router-strategies.md`.
#[macro_export]
macro_rules! register {
    (
        name: $name:literal,
        title: $title:literal,
        description: $description:literal,
        uses_weight: $uses_weight:expr,
        needs: $needs:expr,
        params: $params:ty,
        member_params: $member:ty,
        strategy: $strategy:ty $(,)?
    ) => {
        $crate::__inventory::submit! {
            $crate::StrategyFactory {
                name: $name,
                title: $title,
                description: $description,
                uses_weight: $uses_weight,
                needs: || $needs,
                params_schema: $crate::__schema::<$params>,
                member_schema: $crate::__schema::<$member>,
                create: $crate::__create::<$strategy, $params, $member>,
            }
        }

        #[cfg(test)]
        mod lumewarp_strategy_schema {
            #[allow(unused_imports)]
            use super::*;

            #[test]
            fn is_within_the_subset() {
                $crate::assert_schema_subset::<$params>();
                $crate::assert_schema_subset::<$member>();
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

/// What a gateway tells the control plane about one compiled strategy.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Descriptor {
    pub name: &'static str,
    pub title: &'static str,
    pub description: &'static str,
    pub uses_weight: bool,
    pub needs: Vec<&'static str>,
    pub params_schema: Value,
    pub member_schema: Value,
    /// Hex SHA-256 of the fields above; changes whenever any of them does.
    pub digest: String,
}

impl Descriptor {
    pub fn of(factory: &StrategyFactory) -> Self {
        let needs = (factory.needs)().names();
        let (params_schema, member_schema) = ((factory.params_schema)(), (factory.member_schema)());
        // serde_json objects are sorted by key, so this encoding is canonical.
        let canonical = serde_json::json!({
            "name": factory.name,
            "title": factory.title,
            "description": factory.description,
            "uses_weight": factory.uses_weight,
            "needs": needs,
            "params_schema": params_schema,
            "member_schema": member_schema,
        });
        let digest = Sha256::digest(canonical.to_string().as_bytes());
        Self {
            name: factory.name,
            title: factory.title,
            description: factory.description,
            uses_weight: factory.uses_weight,
            needs,
            params_schema,
            member_schema,
            digest: hex::encode(digest),
        }
    }
}

/// Every compiled strategy, sorted by name.
pub fn descriptors() -> Vec<Descriptor> {
    let mut descriptors: Vec<_> = registered().map(Descriptor::of).collect();
    descriptors.sort_by_key(|d| d.name);
    descriptors
}

/// `lumewarp strategies --json`. An error when two crates registered one name.
pub fn manifest() -> Result<Value, ConfigError> {
    let strategies = descriptors();
    if let Some(pair) = strategies.windows(2).find(|p| p[0].name == p[1].name) {
        return Err(ConfigError(format!(
            "duplicate strategy registration {:?}",
            pair[0].name
        )));
    }
    Ok(serde_json::json!({ "api_version": API_VERSION, "strategies": strategies }))
}

// The settings of a strategy without any: every field is refused. A plain
// comment, because a doc comment here would end up in each schema's description.
#[derive(Debug, Default, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct NoParams {}

/// Inline enum dispatch for stateless built-ins; custom instances alone are boxed.
#[derive(Clone, Copy, Debug)]
pub enum Builtin {
    Weighted,
    LeastInFlight,
    LowestLatency,
    VllmLoad,
}

macro_rules! builtin_strategy {
    (
        $module:ident, $type:ident, $variant:ident, $name:literal, $title:literal,
        $description:literal, $uses_weight:literal, $needs:expr
    ) => {
        pub struct $type;
        impl Strategy for $type {
            fn pick(
                &self,
                req: &RequestInfo<'_>,
                c: &Candidates<'_>,
                rng: &mut dyn RngCore,
            ) -> Option<usize> {
                Builtin::$variant.pick(req, c, rng)
            }
        }
        impl Configure<NoParams, NoParams> for $type {
            fn configure(
                _: &RouterInfo<'_>,
                _: NoParams,
                _: &[NoParams],
            ) -> Result<Self, ConfigError> {
                Ok(Self)
            }
        }
        mod $module {
            use super::*;
            crate::register! {
                name: $name,
                title: $title,
                description: $description,
                uses_weight: $uses_weight,
                needs: $needs,
                params: NoParams,
                member_params: NoParams,
                strategy: $type,
            }
        }
    };
}

builtin_strategy!(
    weighted,
    Weighted,
    Weighted,
    "weighted",
    "Weighted",
    "Split requests by member weight.",
    true,
    Needs::default()
);
builtin_strategy!(
    least_in_flight,
    LeastInFlight,
    LeastInFlight,
    "least_in_flight",
    "Least in flight",
    "Use the member with the fewest requests in flight.",
    false,
    Needs::default()
);
builtin_strategy!(
    lowest_latency,
    LowestLatency,
    LowestLatency,
    "lowest_latency",
    "Lowest latency",
    "Prefer the member with the lowest time to response headers under load.",
    false,
    Needs {
        latency: true,
        vllm_load: false
    }
);
builtin_strategy!(
    vllm_load,
    VllmLoad,
    VllmLoad,
    "vllm_load",
    "vLLM load",
    "Prefer the member whose vLLM reports the shortest queue and the least KV cache usage.",
    false,
    Needs {
        latency: false,
        vllm_load: true
    }
);

impl Strategy for Builtin {
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
    pub fn needs(&self) -> Needs {
        Needs {
            latency: matches!(self, Self::LowestLatency),
            vllm_load: matches!(self, Self::VllmLoad),
        }
    }

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

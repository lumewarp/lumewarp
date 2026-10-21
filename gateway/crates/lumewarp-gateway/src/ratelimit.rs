//! Local per-instance rate limiting by `RateLimitScope.id`.
//!
//! W1 stub with the frozen interface: `admit` always succeeds and
//! `charge_tokens` is a no-op. The real limiter (rpm token bucket,
//! max_parallel permits, retrospective tpm) lands in W2.

use crate::auth::Caller;

/// Limiter state keyed by scope id; lives in `Runtime`, so it survives swaps.
pub struct Registry {
    _private: (),
}

/// Held for the lifetime of a request (until its response body ends or is
/// dropped); releases parallel-request slots on drop.
pub struct Permit {
    _private: (),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RateLimited {
    pub retry_after_secs: u64,
}

impl Registry {
    pub fn new() -> Self {
        Self { _private: () }
    }

    pub fn admit(&self, _caller: &Caller) -> Result<Permit, RateLimited> {
        Ok(Permit { _private: () })
    }

    #[allow(dead_code)] // Called from usage finalization (W2).
    pub fn charge_tokens(&self, _caller: &Caller, _tokens: u64) {}
}

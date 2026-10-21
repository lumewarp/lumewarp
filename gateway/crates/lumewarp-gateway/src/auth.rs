//! Virtual-key authentication against the compiled snapshot.
//!
//! The bearer secret is hashed with SHA-256 and the 32-byte digest is looked
//! up in a `HashMap`. No constant-time comparison is needed: the secret itself
//! is never compared, and the lookup's timing can at most reveal information
//! about the attacker's own digest relative to stored digests. Learning digest
//! prefixes does not help find a preimage (keys are high-entropy random
//! strings), so the timing side channel is not exploitable.

use std::sync::Arc;

use hyper::{header, HeaderMap, Response, StatusCode};
use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1::KeyStatus;
use sha2::{Digest, Sha256};

use crate::{
    error,
    proxy::ProxyBody,
    ratelimit::clock,
    router::{KeyContext, Router},
};

/// Who is calling. Passed by value into the proxy (request extensions are
/// cleared before forwarding).
#[derive(Clone)]
pub enum Caller {
    /// Open standalone mode only.
    Anonymous,
    Key(Arc<KeyContext>),
}

impl Caller {
    /// Interned team label; 0 is the anonymous / team-less label `""`.
    pub fn team_label(&self) -> crate::metrics::LabelId {
        match self {
            Self::Anonymous => 0,
            Self::Key(key) => key.team_label,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthError {
    /// Missing, malformed, unknown, revoked, suspended or expired key.
    InvalidApiKey,
    ModelNotAllowed,
    /// The control plane flipped the key to BUDGET_EXCEEDED.
    InsufficientQuota,
}

impl AuthError {
    pub fn status(self) -> StatusCode {
        match self {
            Self::InvalidApiKey => StatusCode::UNAUTHORIZED,
            Self::ModelNotAllowed => StatusCode::FORBIDDEN,
            Self::InsufficientQuota => StatusCode::TOO_MANY_REQUESTS,
        }
    }
}

impl From<AuthError> for Response<ProxyBody> {
    fn from(err: AuthError) -> Self {
        match err {
            AuthError::InvalidApiKey => error::json(
                err.status(),
                "invalid_api_key",
                "Invalid or missing API key",
            ),
            AuthError::ModelNotAllowed => error::json(
                err.status(),
                "model_not_allowed",
                "This API key is not allowed to use the requested model",
            ),
            AuthError::InsufficientQuota => error::json(
                err.status(),
                "insufficient_quota",
                "Budget exceeded for this API key",
            ),
        }
    }
}

/// A failed `authenticate`. `key` is set when the bearer matched a known key
/// that was rejected (expired, revoked, suspended → 401; budget → 429), so
/// the caller can report a REJECTED usage event; it is `None` for missing,
/// malformed or unknown keys, which are never reported.
pub struct AuthRejection {
    pub error: AuthError,
    pub key: Option<Arc<KeyContext>>,
}

impl std::fmt::Debug for AuthRejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AuthRejection")
            .field("error", &self.error)
            .field("key_id", &self.key.as_ref().map(|key| &key.key_id))
            .finish()
    }
}

impl From<AuthError> for AuthRejection {
    fn from(error: AuthError) -> Self {
        Self { error, key: None }
    }
}

impl From<AuthRejection> for Response<ProxyBody> {
    fn from(rejection: AuthRejection) -> Self {
        rejection.error.into()
    }
}

fn bearer(headers: &HeaderMap) -> Option<&str> {
    let mut values = headers.get_all(header::AUTHORIZATION).iter();
    let value = values.next()?;
    if values.next().is_some() {
        return None;
    }
    let (scheme, key) = value.to_str().ok()?.split_once(' ')?;
    let key = key.trim_start();
    (scheme.eq_ignore_ascii_case("Bearer") && !key.is_empty()).then_some(key)
}

/// One sha256 and one map lookup; no allocation besides the `Arc` clone.
/// Expiry uses the coarse clock (an atomic load, at most a few ms stale).
pub fn authenticate(router: &Router, headers: &HeaderMap) -> Result<Caller, AuthRejection> {
    if router.auth_mode == AuthMode::Open {
        return Ok(Caller::Anonymous);
    }
    let key = bearer(headers).ok_or(AuthError::InvalidApiKey)?;
    let hash: [u8; 32] = Sha256::digest(key.as_bytes()).into();
    let key = router.keys.get(&hash).ok_or(AuthError::InvalidApiKey)?;
    let reject = |error| AuthRejection {
        error,
        key: Some(key.clone()),
    };
    // Lifecycle and expiry take priority over quota.
    if key.expires_at_ms != 0 && clock::unix_ms() >= key.expires_at_ms {
        return Err(reject(AuthError::InvalidApiKey));
    }
    match key.status {
        KeyStatus::Active => Ok(Caller::Key(key.clone())),
        KeyStatus::BudgetExceeded => Err(reject(AuthError::InsufficientQuota)),
        KeyStatus::Revoked | KeyStatus::Suspended | KeyStatus::Unspecified => {
            Err(reject(AuthError::InvalidApiKey))
        }
    }
}

/// Must run before a deployment is chosen. `allow_all_models=false` with an
/// empty list denies everything.
pub fn check_model(caller: &Caller, alias: &str) -> Result<(), AuthError> {
    match caller {
        Caller::Anonymous => Ok(()),
        Caller::Key(key) if key.allow_all_models || key.allowed_models.contains(alias) => Ok(()),
        Caller::Key(_) => Err(AuthError::ModelNotAllowed),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        runtime::Runtime,
        snapshot::tests::{base, key},
    };
    use lumewarp_proto::config::v1 as pb;

    /// The error of a failed authentication, `None` on success.
    fn err(result: Result<Caller, AuthRejection>) -> Option<AuthError> {
        result.err().map(|rejection| rejection.error)
    }

    fn headers(value: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(header::AUTHORIZATION, value.parse().unwrap());
        headers
    }

    fn router(snapshot: &pb::Snapshot, mode: AuthMode) -> Router {
        Router::compile(snapshot, mode, &Runtime::new()).unwrap()
    }

    #[test]
    fn bearer_parsing() {
        let router = router(&base(), AuthMode::RequireKey);
        assert_eq!(
            err(authenticate(&router, &HeaderMap::new())),
            Some(AuthError::InvalidApiKey)
        );
        for bad in ["Bearer wrong", "Basic s1", "Bearer ", "s1"] {
            assert_eq!(
                err(authenticate(&router, &headers(bad))),
                Some(AuthError::InvalidApiKey),
                "{bad}"
            );
        }
        for good in ["Bearer s1", "bearer  s1", "BEARER s1"] {
            assert!(authenticate(&router, &headers(good)).is_ok(), "{good}");
        }
        let mut duplicate = headers("Bearer s1");
        duplicate.append(header::AUTHORIZATION, "Bearer s1".parse().unwrap());
        assert_eq!(
            err(authenticate(&router, &duplicate)),
            Some(AuthError::InvalidApiKey)
        );
    }

    #[test]
    fn open_mode_is_anonymous_and_require_key_with_no_keys_rejects_all() {
        let mut s = base();
        s.api_keys.clear();
        let open = router(&s, AuthMode::Open);
        assert!(matches!(
            authenticate(&open, &HeaderMap::new()),
            Ok(Caller::Anonymous)
        ));
        assert!(check_model(&Caller::Anonymous, "anything").is_ok());
        let closed = router(&s, AuthMode::RequireKey);
        for h in [HeaderMap::new(), headers("Bearer s1"), headers("Bearer ")] {
            assert_eq!(
                err(authenticate(&closed, &h)),
                Some(AuthError::InvalidApiKey)
            );
        }
    }

    #[test]
    fn status_expiry_and_models() {
        let mut s = base();
        let mut push = |id: &str, f: &dyn Fn(&mut pb::ApiKey)| {
            let mut k = key(id, id, "t1", &["m1"]);
            f(&mut k);
            s.api_keys.push(k);
        };
        push("revoked", &|k| k.status = pb::KeyStatus::Revoked as i32);
        push("suspended", &|k| k.status = pb::KeyStatus::Suspended as i32);
        push("budget", &|k| {
            k.status = pb::KeyStatus::BudgetExceeded as i32
        });
        push("expired", &|k| k.expires_at_unix_ms = 1);
        push("expired-budget", &|k| {
            k.expires_at_unix_ms = 1;
            k.status = pb::KeyStatus::BudgetExceeded as i32;
        });
        push("future", &|k| k.expires_at_unix_ms = i64::MAX);
        push("all", &|k| {
            k.policy.as_mut().unwrap().allow_all_models = true;
        });
        let router = router(&s, AuthMode::RequireKey);
        let auth = |secret: &str| authenticate(&router, &headers(&format!("Bearer {secret}")));
        let rejected_key = |secret: &str| auth(secret).err().unwrap().key.map(|k| k.key_id.clone());
        for id in ["revoked", "suspended", "expired", "expired-budget"] {
            assert_eq!(err(auth(id)), Some(AuthError::InvalidApiKey), "{id}");
        }
        assert_eq!(err(auth("budget")), Some(AuthError::InsufficientQuota));
        // Known-but-rejected keys carry their context; unknown keys do not.
        for id in [
            "revoked",
            "suspended",
            "expired",
            "expired-budget",
            "budget",
        ] {
            assert_eq!(rejected_key(id).as_deref(), Some(id));
        }
        assert!(rejected_key("unknown").is_none());
        let future = auth("future").unwrap();
        assert!(check_model(&future, "m1").is_ok());
        assert_eq!(
            check_model(&future, "m2").err(),
            Some(AuthError::ModelNotAllowed)
        );
        // k2 in base(): allow_all_models=false, no models => deny-all.
        let deny_all = auth("s2").unwrap();
        for model in ["m1", "m2", "", "unknown"] {
            assert_eq!(
                check_model(&deny_all, model).err(),
                Some(AuthError::ModelNotAllowed)
            );
        }
        let all = auth("all").unwrap();
        assert!(check_model(&all, "m2").is_ok());
        assert!(check_model(&all, "not-a-route").is_ok());
        assert_eq!(AuthError::ModelNotAllowed.status(), 403);
        assert_eq!(
            Response::from(AuthError::InsufficientQuota).status(),
            StatusCode::TOO_MANY_REQUESTS
        );
    }

    /// `cargo test --release --bin lumewarp bench_authenticate_and_admit -- --ignored --nocapture`
    #[test]
    #[ignore = "timing benchmark; run explicitly in release mode"]
    fn bench_authenticate_and_admit() {
        use std::{hint::black_box, time::Instant};
        let scope = |id: &str| pb::RateLimitScope {
            id: id.into(),
            limit: Some(pb::RateLimit {
                requests_per_minute: u32::MAX,
                tokens_per_minute: u64::MAX,
                max_parallel_requests: u32::MAX,
            }),
        };
        let mut s = base();
        s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes =
            vec![scope("team:t1"), scope("key:k1")];
        let open = router(&s, AuthMode::Open);
        let keyed = router(&s, AuthMode::RequireKey);
        let cases = [
            ("open (anonymous)", &open, headers("Bearer s1")),
            ("key, no scopes", &keyed, headers("Bearer s2")),
            ("key, 2 scopes", &keyed, headers("Bearer s1")),
            ("unknown key (401)", &keyed, headers("Bearer nope")),
        ];
        const N: u32 = 2_000_000;
        for (name, router, headers) in cases {
            let limiters = &router.runtime.limiters;
            for _ in 0..10_000 {
                if let Ok(caller) = authenticate(router, &headers) {
                    drop(black_box(limiters.admit(&caller)));
                }
            }
            let start = Instant::now();
            for _ in 0..N {
                if let Ok(caller) = authenticate(black_box(router), black_box(&headers)) {
                    drop(black_box(limiters.admit(&caller)));
                }
            }
            let ns = start.elapsed().as_nanos() as f64 / f64::from(N);
            println!("authenticate+admit {name}: {ns:.1} ns/op");
        }
    }
}

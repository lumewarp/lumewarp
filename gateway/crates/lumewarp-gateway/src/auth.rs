//! Virtual-key authentication against the compiled snapshot.
//!
//! The bearer secret is hashed with SHA-256 and the 32-byte digest is looked
//! up in a `HashMap`. No constant-time comparison is needed: the secret itself
//! is never compared, and the lookup's timing can at most reveal information
//! about the attacker's own digest relative to stored digests. Learning digest
//! prefixes does not help find a preimage (keys are high-entropy random
//! strings), so the timing side channel is not exploitable.

use std::sync::Arc;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hyper::{header, HeaderMap, Response, StatusCode};
use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1::KeyStatus;
use sha2::{Digest, Sha256};

use crate::{
    error,
    proxy::ProxyBody,
    ratelimit::clock,
    router::{KeyContext, RoutingTable},
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
pub fn authenticate(
    routing_table: &RoutingTable,
    headers: &HeaderMap,
) -> Result<Caller, AuthRejection> {
    if routing_table.auth_mode == AuthMode::Open {
        return Ok(Caller::Anonymous);
    }
    let key = bearer(headers).ok_or(AuthError::InvalidApiKey)?;
    if key.starts_with("lwpg.") {
        let result = authenticate_playground(routing_table, key);
        routing_table.runtime.metrics.playground[usize::from(result.is_err())].inc();
        return result;
    }
    let hash: [u8; 32] = Sha256::digest(key.as_bytes()).into();
    let key = routing_table
        .keys
        .get(&hash)
        .ok_or(AuthError::InvalidApiKey)?;
    authenticate_key(key)
}

/// Verify the signed delegation before resolving a key, so forged tokens never
/// produce attributed rejection usage. `ring::hmac::verify` compares in constant time.
fn authenticate_playground(table: &RoutingTable, token: &str) -> Result<Caller, AuthRejection> {
    let invalid = AuthError::InvalidApiKey;
    let signing_key = table.playground_key.as_ref().ok_or(invalid)?;
    let (message, mac) = token.rsplit_once('.').ok_or(invalid)?;
    let rest = message.strip_prefix("lwpg.").ok_or(invalid)?;
    let (id, expiry) = rest.split_once('.').ok_or(invalid)?;
    if id.len() != 36
        || !id.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
        || expiry.is_empty()
        || !expiry.bytes().all(|b| b.is_ascii_digit())
        || (expiry.len() > 1 && expiry.starts_with('0'))
        || mac.len() != 43
    {
        return Err(invalid.into());
    }
    let expires_at: i64 = expiry.parse().map_err(|_| invalid)?;
    let now = clock::unix_ms();
    // Bound delegated credential lifetime even if the issuer signs a bad expiry.
    if expires_at <= now || expires_at > now.saturating_add(300_000) {
        return Err(invalid.into());
    }
    let mut signature = [0u8; 32];
    let len = URL_SAFE_NO_PAD
        .decode_slice(mac, &mut signature)
        .map_err(|_| invalid)?;
    if len != 32 || ring::hmac::verify(signing_key, message.as_bytes(), &signature).is_err() {
        return Err(invalid.into());
    }
    authenticate_key(table.keys_by_id.get(id).ok_or(invalid)?)
}

#[inline]
fn authenticate_key(key: &Arc<KeyContext>) -> Result<Caller, AuthRejection> {
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

/// Must run before a model is chosen. `allow_all_models=false` with an
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

    fn routing_table(snapshot: &pb::Snapshot, mode: AuthMode) -> RoutingTable {
        RoutingTable::compile(snapshot, mode, &Runtime::new()).unwrap()
    }

    const PLAYGROUND_SECRET: &str = "test-encryption-key-0123456789abcdef";
    const PLAYGROUND_ID: &str = "00000000-0000-0000-0000-000000000001";
    const PLAYGROUND_VECTOR: &str = "lwpg.00000000-0000-0000-0000-000000000001.4102444800000.9q0Juo2V6Fupxe8pQezrHCPUq0zIgfqz-fmT_azFrrE";

    fn signed(id: &str, expiry: i64) -> String {
        let message = format!("lwpg.{id}.{expiry}");
        let key = crate::sealed::playground_key(PLAYGROUND_SECRET);
        let mac = ring::hmac::sign(&key, message.as_bytes());
        format!("{message}.{}", URL_SAFE_NO_PAD.encode(mac.as_ref()))
    }

    fn playground_table() -> RoutingTable {
        let mut s = base();
        s.api_keys[0].key_id = PLAYGROUND_ID.into();
        let mut table = routing_table(&s, AuthMode::RequireKey);
        table.playground_key = Some(crate::sealed::playground_key(PLAYGROUND_SECRET));
        table
    }

    #[test]
    fn playground_vector_and_normal_key() {
        assert_eq!(signed(PLAYGROUND_ID, 4_102_444_800_000), PLAYGROUND_VECTOR);
        let table = playground_table();
        let token = signed(PLAYGROUND_ID, clock::unix_ms() + 60_000);
        let caller = authenticate(&table, &headers(&format!("Bearer {token}"))).unwrap();
        assert!(matches!(caller, Caller::Key(k) if &*k.key_id == PLAYGROUND_ID));
        assert!(authenticate(&table, &headers("Bearer s1")).is_ok());
        let encoded = table
            .runtime
            .metrics
            .encode(&table.runtime.inflight)
            .unwrap();
        assert!(encoded.contains("lumewarp_playground_requests_total{result=\"ok\"} 1"));
    }

    #[test]
    fn playground_rejects_invalid_tokens() {
        let table = playground_table();
        let expiry = clock::unix_ms() + 60_000;
        let token = signed(PLAYGROUND_ID, expiry);
        let mut tampered = token.clone();
        let replacement = if tampered.ends_with('A') { "E" } else { "A" };
        tampered.replace_range(tampered.len() - 1.., replacement);
        for token in [
            signed(PLAYGROUND_ID, 1),
            signed("00000000-0000-0000-0000-000000000002", expiry),
            tampered,
            "lwpg.".into(),
            "lwpg.id.exp.mac".into(),
            format!("{token}.extra"),
            token.replace(&expiry.to_string(), &format!("0{expiry}")),
            token.replace(&expiry.to_string(), "9223372036854775808"),
        ] {
            let rejection = authenticate(&table, &headers(&format!("Bearer {token}")))
                .err()
                .unwrap();
            assert_eq!(rejection.error, AuthError::InvalidApiKey);
            assert!(rejection.key.is_none());
        }
        let mut table = table;
        table.playground_key = None;
        assert_eq!(
            err(authenticate(&table, &headers(&format!("Bearer {token}")))),
            Some(AuthError::InvalidApiKey)
        );
        assert!(authenticate(&table, &headers("Bearer s1")).is_ok());
    }

    #[test]
    fn playground_rejects_expiry_more_than_five_minutes_ahead() {
        let table = playground_table();
        let now = clock::unix_ms();
        for expiry in [now + 360_000, 4_102_444_800_000, i64::MAX] {
            let token = signed(PLAYGROUND_ID, expiry);
            let rejection = authenticate(&table, &headers(&format!("Bearer {token}")))
                .err()
                .unwrap();
            assert_eq!(rejection.error, AuthError::InvalidApiKey);
            assert!(rejection.key.is_none());
        }
        let token = signed(PLAYGROUND_ID, now + 300_000);
        assert!(authenticate(&table, &headers(&format!("Bearer {token}"))).is_ok());
    }

    #[test]
    fn playground_applies_current_key_lifecycle_and_budget() {
        for (status, expiry, expected) in [
            (KeyStatus::Revoked, 0, AuthError::InvalidApiKey),
            (KeyStatus::Suspended, 0, AuthError::InvalidApiKey),
            (KeyStatus::Active, 1, AuthError::InvalidApiKey),
            (KeyStatus::BudgetExceeded, 0, AuthError::InsufficientQuota),
            (KeyStatus::BudgetExceeded, 1, AuthError::InvalidApiKey),
        ] {
            let mut s = base();
            s.api_keys[0].key_id = PLAYGROUND_ID.into();
            s.api_keys[0].status = status as i32;
            s.api_keys[0].expires_at_unix_ms = expiry;
            let mut table = routing_table(&s, AuthMode::RequireKey);
            table.playground_key = Some(crate::sealed::playground_key(PLAYGROUND_SECRET));
            let token = signed(PLAYGROUND_ID, clock::unix_ms() + 60_000);
            let rejection = authenticate(&table, &headers(&format!("Bearer {token}")))
                .err()
                .unwrap();
            assert_eq!(rejection.error, expected);
            assert_eq!(&*rejection.key.unwrap().key_id, PLAYGROUND_ID);
        }
    }

    #[test]
    fn bearer_parsing() {
        let routing_table = routing_table(&base(), AuthMode::RequireKey);
        assert_eq!(
            err(authenticate(&routing_table, &HeaderMap::new())),
            Some(AuthError::InvalidApiKey)
        );
        for bad in ["Bearer wrong", "Basic s1", "Bearer ", "s1"] {
            assert_eq!(
                err(authenticate(&routing_table, &headers(bad))),
                Some(AuthError::InvalidApiKey),
                "{bad}"
            );
        }
        for good in ["Bearer s1", "bearer  s1", "BEARER s1"] {
            assert!(
                authenticate(&routing_table, &headers(good)).is_ok(),
                "{good}"
            );
        }
        let mut duplicate = headers("Bearer s1");
        duplicate.append(header::AUTHORIZATION, "Bearer s1".parse().unwrap());
        assert_eq!(
            err(authenticate(&routing_table, &duplicate)),
            Some(AuthError::InvalidApiKey)
        );
    }

    #[test]
    fn open_mode_is_anonymous_and_require_key_with_no_keys_rejects_all() {
        let mut s = base();
        s.api_keys.clear();
        let open = routing_table(&s, AuthMode::Open);
        assert!(matches!(
            authenticate(&open, &HeaderMap::new()),
            Ok(Caller::Anonymous)
        ));
        assert!(check_model(&Caller::Anonymous, "anything").is_ok());
        let closed = routing_table(&s, AuthMode::RequireKey);
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
        let routing_table = routing_table(&s, AuthMode::RequireKey);
        let auth =
            |secret: &str| authenticate(&routing_table, &headers(&format!("Bearer {secret}")));
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
        assert!(check_model(&all, "not-a-router").is_ok());
        assert_eq!(AuthError::ModelNotAllowed.status(), 403);
        assert_eq!(
            Response::from(AuthError::InsufficientQuota).status(),
            StatusCode::TOO_MANY_REQUESTS
        );
    }

    fn with_previous_hash(previous_expiry: i64) -> pb::Snapshot {
        let mut s = base();
        s.required_capabilities.push("rotation_grace".into());
        let mut k = key("k1", "current", "t1", &["m1"]);
        k.previous_key_hash = Sha256::digest(b"previous").to_vec();
        k.previous_key_expires_at_unix_ms = previous_expiry;
        s.api_keys[0] = k;
        s
    }

    #[test]
    fn previous_hash_is_the_same_key_until_its_own_expiry() {
        let table = routing_table(&with_previous_hash(i64::MAX), AuthMode::RequireKey);
        let current = authenticate(&table, &headers("Bearer current")).unwrap();
        let previous = authenticate(&table, &headers("Bearer previous")).unwrap();
        let id = |caller: Caller| match caller {
            Caller::Key(key) => key.key_id.clone(),
            Caller::Anonymous => unreachable!(),
        };
        assert_eq!(&*id(current), "k1");
        assert_eq!(&*id(previous), "k1", "usage stays on one key");

        let expired = routing_table(&with_previous_hash(1), AuthMode::RequireKey);
        assert_eq!(
            err(authenticate(&expired, &headers("Bearer previous"))),
            Some(AuthError::InvalidApiKey)
        );
        assert!(authenticate(&expired, &headers("Bearer current")).is_ok());
    }

    #[test]
    fn previous_hash_expiry_survives_a_persisted_boot() {
        let dir = tempfile::tempdir().unwrap();
        let boot = |snapshot: &pb::Snapshot| {
            crate::snapshot::store(dir.path(), snapshot).unwrap();
            let loaded = crate::snapshot::load(dir.path(), "scope-a")
                .unwrap()
                .unwrap();
            crate::snapshot::validate(&loaded, crate::snapshot::SUPPORTED_CAPABILITIES).unwrap();
            routing_table(&loaded, AuthMode::RequireKey)
        };

        // The control plane is unreachable; the persisted snapshot is all the gateway has.
        let live = boot(&with_previous_hash(i64::MAX));
        assert!(authenticate(&live, &headers("Bearer previous")).is_ok());

        let stale = boot(&with_previous_hash(1));
        assert_eq!(
            err(authenticate(&stale, &headers("Bearer previous"))),
            Some(AuthError::InvalidApiKey)
        );
        assert!(authenticate(&stale, &headers("Bearer current")).is_ok());
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
        let open = routing_table(&s, AuthMode::Open);
        let keyed = routing_table(&s, AuthMode::RequireKey);
        let cases = [
            ("open (anonymous)", &open, headers("Bearer s1")),
            ("key, no scopes", &keyed, headers("Bearer s2")),
            ("key, 2 scopes", &keyed, headers("Bearer s1")),
            ("unknown key (401)", &keyed, headers("Bearer nope")),
        ];
        const N: u32 = 2_000_000;
        for (name, routing_table, headers) in cases {
            let limiters = &routing_table.runtime.limiters;
            for _ in 0..10_000 {
                if let Ok(caller) = authenticate(routing_table, &headers) {
                    drop(black_box(limiters.admit(&caller)));
                }
            }
            let start = Instant::now();
            for _ in 0..N {
                if let Ok(caller) = authenticate(black_box(routing_table), black_box(&headers)) {
                    drop(black_box(limiters.admit(&caller)));
                }
            }
            let ns = start.elapsed().as_nanos() as f64 / f64::from(N);
            println!("authenticate+admit {name}: {ns:.1} ns/op");
        }
    }
}

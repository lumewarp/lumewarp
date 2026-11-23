use std::{sync::Arc, time::Duration};

use hyper::{
    header::{HeaderName, HeaderValue},
    HeaderMap,
};

use super::*;
use crate::balance;

fn headers(values: &[(&'static str, &str)]) -> HeaderMap {
    let mut map = HeaderMap::new();
    for (name, value) in values {
        map.insert(*name, HeaderValue::from_str(value).unwrap());
    }
    map
}

#[test]
fn session_sources_are_checked_in_order() {
    let custom = HeaderName::from_static("x-conversation-id");
    let map = headers(&[(SESSION_HEADER, "main"), ("x-conversation-id", "custom")]);
    assert_eq!(
        session_hash(&map, Some(&custom), || Some("body".into())),
        Some(balance::sticky_hash(b"main"))
    );
    let map = headers(&[("x-conversation-id", "custom")]);
    assert_eq!(
        session_hash(&map, Some(&custom), || Some("body".into())),
        Some(balance::sticky_hash(b"custom"))
    );
    assert_eq!(
        session_hash(&HeaderMap::new(), None, || Some("body".into())),
        Some(balance::sticky_hash(b"body"))
    );
    assert_eq!(session_hash(&HeaderMap::new(), None, || None), None);
    // An empty or oversized value is no match; the next source is tried.
    let map = headers(&[(SESSION_HEADER, ""), ("x-conversation-id", "custom")]);
    assert_eq!(
        session_hash(&map, Some(&custom), || None),
        Some(balance::sticky_hash(b"custom"))
    );
    let huge = "s".repeat(MAX_SESSION_BYTES + 1);
    let map = headers(&[(SESSION_HEADER, &huge)]);
    assert_eq!(
        session_hash(&map, None, || Some(Cow::Borrowed("body"))),
        Some(balance::sticky_hash(b"body"))
    );
    assert_eq!(
        session_hash(&map, None, || Some(Cow::Borrowed(huge.as_str()))),
        None
    );
}

#[test]
fn overrides_expire_after_an_idle_ttl() {
    let overrides = Overrides::new(Config {
        max_entries: 4,
        ttl: Duration::from_secs(60),
    });
    let router = overrides.router_id("public");
    let name: Arc<str> = "public".into();
    overrides.remember(router, &name, 42, "b", 1_000);
    assert_eq!(overrides.lookup(router, 42, 1_000).as_deref(), Some("b"));
    assert_eq!(overrides.lookup(router, 42, 61_000), None);
    // A different session is a different entry.
    assert_eq!(overrides.lookup(router, 43, 1_000), None);
}

#[test]
fn merge_keeps_the_newest_expiry() {
    let overrides = Overrides::new(Config {
        max_entries: 16,
        ttl: Duration::from_secs(1),
    });
    let router = overrides.router_id("public");
    overrides.merge(router, 7, "b".into(), 2_000, 0);
    overrides.merge(router, 7, "c".into(), 1_000, 0);
    assert_eq!(overrides.lookup(router, 7, 0).as_deref(), Some("b"));
    overrides.merge(router, 7, "c".into(), 3_000, 0);
    assert_eq!(overrides.lookup(router, 7, 0).as_deref(), Some("c"));
}

#[test]
fn a_newer_remember_is_published_over_a_merged_override() {
    let overrides = Overrides::new(Config {
        max_entries: 16,
        ttl: Duration::from_secs(60),
    });
    let router = overrides.router_id("public");
    let name: Arc<str> = "public".into();
    // A peer's entry is newer than the local one, then a local move wins.
    overrides.merge(router, 7, "b".into(), 2_000, 0);
    overrides.remember(router, &name, 7, "c", 10_000);
    let pending = overrides.take_pending(8);
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].model.as_ref(), "c");
    assert_eq!(pending[0].expires_ms, 70_000);
}

#[test]
fn the_table_is_bounded_per_shard() {
    let overrides = Overrides::new(Config {
        max_entries: SHARDS,
        ttl: Duration::from_secs(60),
    });
    let router = overrides.router_id("public");
    let name: Arc<str> = "public".into();
    for session in 0..(SHARDS as u64 * 4) {
        let model = format!("m{session}");
        overrides.remember(router, &name, session, &model, 1_000);
    }
    assert!(overrides.len() <= SHARDS, "{}", overrides.len());
}

#[test]
fn an_expired_remote_override_is_not_merged() {
    let overrides = Overrides::new(Config {
        max_entries: 16,
        ttl: Duration::from_secs(1),
    });
    let router = overrides.router_id("public");
    overrides.merge(router, 7, "b".into(), 2_000, 2_000);
    assert_eq!(overrides.len(), 0);
    assert_eq!(overrides.lookup(router, 7, 0), None);
}

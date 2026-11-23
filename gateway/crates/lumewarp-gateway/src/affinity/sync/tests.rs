use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

use super::*;

#[test]
fn values_round_trip() {
    let (field, value) = encode(0xdead_beef_1234_5678, "model a", 9_999);
    assert_eq!(field, "deadbeef12345678");
    assert_eq!(
        decode(&field, &value),
        Some((0xdead_beef_1234_5678, "model a".into(), 9_999))
    );
    assert_eq!(decode("not-hex", &value), None);
    assert_eq!(decode(&field, "not json"), None);
}

#[derive(Clone, Default)]
struct MemoryHub {
    state: Arc<Mutex<HashMap<String, HashMap<String, String>>>>,
}

#[derive(Clone, Default)]
struct MemoryStore {
    hub: MemoryHub,
}

impl Store for MemoryStore {
    async fn exchange(
        &self,
        pushes: &[Publish],
        routers: &[Arc<str>],
    ) -> Result<Vec<Remote>, Error> {
        let mut state = self.hub.state.lock().unwrap();
        for push in pushes {
            let (field, value) = encode(push.session, &push.model, push.expires_ms);
            state
                .entry(push.router.to_string())
                .or_default()
                .insert(field, value);
        }
        let mut remotes = Vec::new();
        for router in routers {
            if let Some(fields) = state.get(router.as_ref()) {
                for (field, value) in fields {
                    if let Some((session, model, expires_ms)) = decode(field, value) {
                        remotes.push(Remote {
                            router: router.clone(),
                            session,
                            model: model.into(),
                            expires_ms,
                        });
                    }
                }
            }
        }
        Ok(remotes)
    }
}

#[tokio::test]
async fn one_gateway_publishes_and_another_merges() {
    let source = crate::runtime::Runtime::new();
    let target = crate::runtime::Runtime::new();
    let source_router = source.affinity.router_id("public");
    let target_router = target.affinity.router_id("public");
    let name: Arc<str> = "public".into();
    source
        .affinity
        .remember(source_router, &name, 9, "b", 1_000);

    let store = MemoryStore::default();
    let pushes = source.affinity.take_pending(16);
    let remotes = store
        .exchange(&pushes, &target.affinity.router_names())
        .await
        .unwrap();
    for remote in remotes {
        target.affinity.merge(
            target_router,
            remote.session,
            remote.model,
            remote.expires_ms,
            0,
        );
    }
    assert_eq!(
        target.affinity.lookup(target_router, 9, 1_000).as_deref(),
        Some("b")
    );
    // The receiving gateway does not queue what it learned for republishing.
    assert!(target.affinity.take_pending(16).is_empty());
}

#[tokio::test]
async fn the_newest_expiry_wins_across_gateways() {
    let target = crate::runtime::Runtime::new();
    let router = target.affinity.router_id("public");
    target.affinity.merge(router, 9, "old".into(), 2_000, 0);
    target.affinity.merge(router, 9, "new".into(), 3_000, 0);
    target.affinity.merge(router, 9, "older".into(), 1_000, 0);
    assert_eq!(target.affinity.lookup(router, 9, 0).as_deref(), Some("new"));
}

/// Against a real server when `LUMEWARP_TEST_REDIS_URL` is set (CI runs one);
/// skipped otherwise. One pipelined exchange publishes, reads back only live
/// entries, and deletes expired fields from the hash.
#[tokio::test]
async fn redis_exchange_publishes_reads_and_deletes_expired() {
    let Some(url) = std::env::var("LUMEWARP_TEST_REDIS_URL")
        .ok()
        .filter(|url| !url.trim().is_empty())
    else {
        return;
    };
    let namespace = format!("test-{}", uuid::Uuid::now_v7());
    let store = RedisStore::new(&url, &namespace, 60_000).unwrap();
    let router: Arc<str> = "public".into();
    let now = crate::affinity::now_ms();
    let publish = |session, model: &str, expires_ms| Publish {
        router: router.clone(),
        session,
        model: model.into(),
        expires_ms,
    };
    // A peer's stale field already sits in the hash.
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut raw = client.get_multiplexed_async_connection().await.unwrap();
    let (stale_field, stale_value) = encode(3, "gone", now - 1);
    let _: () = redis::cmd("HSET")
        .arg(store.key(&router))
        .arg(&stale_field)
        .arg(stale_value)
        .query_async(&mut raw)
        .await
        .unwrap();

    let remotes = store
        .exchange(
            &[
                publish(1, "a", now + 60_000),
                publish(2, "expired", now - 1),
            ],
            std::slice::from_ref(&router),
        )
        .await
        .unwrap();
    let sessions: Vec<_> = remotes
        .iter()
        .map(|remote| (remote.session, &*remote.model))
        .collect();
    assert_eq!(sessions, vec![(1, "a")]);
    let fields: HashMap<String, String> = redis::cmd("HGETALL")
        .arg(store.key(&router))
        .query_async(&mut raw)
        .await
        .unwrap();
    assert_eq!(
        fields.keys().cloned().collect::<Vec<_>>(),
        vec![format!("{:016x}", 1u64)]
    );
    let ttl: i64 = redis::cmd("PTTL")
        .arg(store.key(&router))
        .query_async(&mut raw)
        .await
        .unwrap();
    assert!(ttl > 0 && ttl <= 60_000, "{ttl}");
}

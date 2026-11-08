//! Bounded process-local observations and notification routing, never request bodies.
use crate::targets::McpTarget;
use lumewarp_proto::config::v1::McpDefinitionKind;
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
};
type DefinitionKey = (String, i32, String);
static FIRST: OnceLock<Mutex<HashMap<DefinitionKey, [u8; 32]>>> = OnceLock::new();
type NotificationRoutes = HashMap<(String, String), String>;
static ROUTES: OnceLock<Mutex<NotificationRoutes>> = OnceLock::new();
const LIMIT: usize = 20_000;
pub fn first(
    target: &McpTarget,
    kind: McpDefinitionKind,
    name: &str,
    hash: [u8; 32],
) -> Option<[u8; 32]> {
    let mut map = FIRST
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    let key = (target.id.to_string(), kind as i32, name.into());
    if let Some(hash) = map.get(&key) {
        return Some(*hash);
    }
    if map.len() >= LIMIT {
        return None;
    }
    map.insert(key, hash);
    Some(hash)
}
pub fn remember(session: &str, id: &Value, server: &str) {
    if id.is_null() {
        return;
    }
    let text = id.to_string();
    if text.len() > 4096 {
        return;
    }
    let mut map = ROUTES
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    if map.len() >= LIMIT {
        map.clear();
    }
    map.insert((session.into(), text), server.into());
}
pub fn lookup(session: &str, id: &Value) -> Option<String> {
    ROUTES
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(&(session.into(), id.to_string()))
        .cloned()
}

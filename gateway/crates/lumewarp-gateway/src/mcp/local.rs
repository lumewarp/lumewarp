//! Bounded process-local observations and notification routing, never request bodies.
use lumewarp_proto::config::v1::McpDefinitionKind;
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    sync::{Mutex, OnceLock},
};
type DefinitionKey = (McpDefinitionKind, String);
#[derive(Default)]
struct ServerFirst {
    id: String,
    definitions: HashMap<DefinitionKey, [u8; 32]>,
    limit_logged: bool,
}
/// Runtime-owned TOFU observations, pruned by the same snapshot hook as L1.
#[derive(Default)]
pub(crate) struct First {
    servers: Mutex<HashMap<String, ServerFirst>>,
}
impl First {
    pub fn record(
        &self,
        server: &str,
        server_id: &str,
        kind: McpDefinitionKind,
        name: &str,
        hash: [u8; 32],
    ) -> Option<[u8; 32]> {
        let mut servers = self.servers.lock().unwrap_or_else(|e| e.into_inner());
        let entry = servers.entry(server.into()).or_default();
        if entry.id != server_id {
            *entry = ServerFirst {
                id: server_id.into(),
                ..Default::default()
            };
        }
        let key = (kind, name.into());
        if let Some(first) = entry.definitions.get(&key) {
            return Some(*first);
        }
        if entry.definitions.len() >= crate::targets::MAX_APPROVED_DEFINITIONS {
            if !entry.limit_logged {
                tracing::warn!(server, "MCP per-server TOFU observation limit reached");
                entry.limit_logged = true;
            }
            return None;
        }
        entry.definitions.insert(key, hash);
        Some(hash)
    }
    pub fn retain_servers(&self, keep: &HashSet<&str>) {
        self.servers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .retain(|server, _| keep.contains(server.as_str()));
    }
}
type NotificationRoutes = HashMap<(String, String), String>;
static ROUTES: OnceLock<Mutex<NotificationRoutes>> = OnceLock::new();
const LIMIT: usize = 20_000;
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

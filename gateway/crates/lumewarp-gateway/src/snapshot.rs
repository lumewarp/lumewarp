//! Snapshot validation, delta application and on-disk persistence, per
//! `docs/config-protocol.md`. Everything here is off the request path: the
//! apply task calls `apply` -> `validate` -> `RoutingTable::compile` -> `store` ->
//! swap -> ACK.

use std::{
    collections::{HashMap, HashSet},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::Path,
};

use lumewarp_proto::config::v1::{
    self as pb, config_update::Update, ConfigUpdate, NackCode, Snapshot,
};
use prost::Message;
use sha2::{Digest, Sha256};

/// Capabilities this gateway understands (`Snapshot.required_capabilities`).
pub const SUPPORTED_CAPABILITIES: &[&str] = &[
    "retry",
    "fallbacks",
    "health_check",
    "circuit_breaker",
    "traffic_split",
    "vllm_load",
    "rotation_grace",
    "encrypted_api_key",
    "model_limits",
    "default_body",
    "reasoning_effort",
    "model_chat_check",
    "strategy:weighted",
    "strategy:least_in_flight",
    "strategy:lowest_latency",
    "strategy:vllm_load",
    "mcp",
    "a2a",
];

/// The strategy of a pool that came from a `Model`, not a `Router`.
pub const DEFAULT_STRATEGY: &str = "weighted";

/// Built-in strategies by name; dispatched as an enum, without a vtable.
pub fn builtin_strategy(name: &str) -> Option<lumewarp_router_api::Builtin> {
    use lumewarp_router_api::Builtin;
    match name {
        "weighted" => Some(Builtin::Weighted),
        "least_in_flight" => Some(Builtin::LeastInFlight),
        "lowest_latency" => Some(Builtin::LowestLatency),
        "vllm_load" => Some(Builtin::VllmLoad),
        _ => None,
    }
}

pub fn supported_capabilities() -> Vec<String> {
    let mut caps: Vec<_> = SUPPORTED_CAPABILITIES
        .iter()
        .map(|c| c.to_string())
        .collect();
    caps.extend(lumewarp_plugin_host::global().capabilities());
    #[cfg(test)]
    caps.extend(lumewarp_router_api::registered().map(|f| format!("strategy:{}", f.name)));
    caps.sort();
    caps.dedup();
    caps
}

pub fn valid_strategy_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

fn params_object(text: &str, what: &str) -> Result<serde_json::Value, ApplyError> {
    if text.len() > 16 * 1024 {
        return Err(ApplyError::invalid(format!("{what} exceed 16 KiB")));
    }
    if text.is_empty() {
        return Ok(serde_json::json!({}));
    }
    let params: serde_json::Value = serde_json::from_str(text)
        .map_err(|_| ApplyError::invalid(format!("{what} must be a JSON object")))?;
    if !params.is_object() {
        return Err(ApplyError::invalid(format!("{what} must be a JSON object")));
    }
    Ok(params)
}

pub fn strategy_params(r: &pb::Router) -> Result<serde_json::Value, ApplyError> {
    params_object(&r.params_json, "strategy params")
}

pub fn strategy_headers(r: &pb::Router) -> Result<Vec<String>, ApplyError> {
    let params = strategy_params(r)?;
    let Some(headers) = params.get("headers") else {
        return Ok(Vec::new());
    };
    let headers = headers
        .as_array()
        .ok_or_else(|| ApplyError::invalid("params.headers must be an array"))?;
    headers
        .iter()
        .map(|header| {
            let name = header
                .as_str()
                .ok_or_else(|| ApplyError::invalid("params.headers must contain header names"))?;
            let name = hyper::header::HeaderName::from_bytes(name.as_bytes()).map_err(|_| {
                ApplyError::invalid("params.headers contains an invalid header name")
            })?;
            // Credentials a client may send to this gateway, including the
            // MCP/A2A key (D5, m8): a router strategy must never read them out
            // of `params.headers` and act on them.
            if matches!(
                name.as_str(),
                "authorization"
                    | "proxy-authorization"
                    | "cookie"
                    | "set-cookie"
                    | "x-api-key"
                    | "x-lumewarp-key"
            ) {
                return Err(ApplyError::invalid(
                    "params.headers must not expose credential headers",
                ));
            }
            Ok(name.as_str().to_owned())
        })
        .collect()
}
/// Upper bounds that keep split labels and per-request work bounded.
pub const MAX_SPLITS: usize = 16;
pub const MAX_SPLIT_NAME: usize = 32;
/// org, team, owner (member/service account) and key: the four levels of the quota
/// hierarchy. A key with more is rejected instead of costing extra limiter lookups.
pub const MAX_RATE_LIMIT_SCOPES: usize = 4;
pub const MAX_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_KEYS: usize = 200_000;
pub const FILE_NAME: &str = "snapshot.pb";
const TMP_NAME: &str = "snapshot.pb.tmp";
const DIGEST_LEN: usize = 32;
const LEN_FIELD: usize = 4;
/// First bytes of `snapshot.pb`. Bump with any change an older payload could
/// decode under and mean something else: protobuf skips unknown fields, so
/// decoding alone cannot tell the formats apart.
const FILE_MAGIC: &[u8; 8] = b"LWSNAP02";
/// Upper bound for the opaque control-plane digest.
const MAX_DIGEST_BYTES: u32 = 1024;

/// Becomes a NACK. `reason` is sanitized: names and ids only, never secrets.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{}: {reason}", code.as_str_name())]
pub struct ApplyError {
    pub code: NackCode,
    pub reason: String,
}

impl ApplyError {
    pub fn invalid(reason: impl Into<String>) -> Self {
        Self {
            code: NackCode::InvalidConfig,
            reason: reason.into(),
        }
    }

    pub fn unsupported(reason: impl Into<String>) -> Self {
        Self {
            code: NackCode::UnsupportedFeature,
            reason: reason.into(),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum LoadError {
    #[error("reading snapshot: {0}")]
    Io(#[from] io::Error),
    #[error("snapshot file exceeds {MAX_SNAPSHOT_BYTES} bytes")]
    TooLarge,
    #[error("snapshot file is truncated or corrupt")]
    Corrupt,
    #[error("snapshot file has an older format")]
    OldFormat,
    #[error("snapshot scope {found:?} does not match the configured scope")]
    WrongScope { found: String },
}

fn unique<'a>(
    names: impl Iterator<Item = &'a str>,
    kind: &str,
) -> Result<HashSet<&'a str>, ApplyError> {
    let mut seen = HashSet::new();
    for name in names {
        if name.is_empty() {
            return Err(ApplyError::invalid(format!("{kind} with empty name")));
        }
        if !seen.insert(name) {
            return Err(ApplyError::invalid(format!("duplicate {kind} {name:?}")));
        }
    }
    Ok(seen)
}

fn known<E: TryFrom<i32>>(value: i32, unspecified_ok: bool, what: &str) -> Result<E, ApplyError> {
    if value == 0 && !unspecified_ok {
        return Err(ApplyError::invalid(format!("{what} is unspecified")));
    }
    E::try_from(value).map_err(|_| ApplyError::invalid(format!("{what} has unknown value {value}")))
}

/// Checks capabilities, references, enums, hash sizes and uniqueness.
pub fn validate(snapshot: &Snapshot, supported_caps: &[&str]) -> Result<(), ApplyError> {
    if let Some(cap) = snapshot
        .required_capabilities
        .iter()
        .find(|cap| !supported_caps.contains(&cap.as_str()))
    {
        return Err(ApplyError::unsupported(format!(
            "unsupported capability {cap:?}"
        )));
    }
    let require = |used: bool, cap: &str| -> Result<(), ApplyError> {
        if used && !snapshot.required_capabilities.iter().any(|c| c == cap) {
            return Err(ApplyError::invalid(format!(
                "field requires capability {cap:?}"
            )));
        }
        Ok(())
    };
    if snapshot.api_keys.len() > MAX_KEYS {
        return Err(ApplyError::invalid(format!(
            "snapshot has more than {MAX_KEYS} keys"
        )));
    }
    if snapshot.encoded_len() > MAX_SNAPSHOT_BYTES {
        return Err(ApplyError::invalid(format!(
            "snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
        )));
    }
    let upstreams = unique(
        snapshot.upstreams.iter().map(|u| u.name.as_str()),
        "upstream",
    )?;
    for u in &snapshot.upstreams {
        require(u.health_check.is_some(), "health_check")?;
        require(u.circuit_breaker.is_some(), "circuit_breaker")?;
        require(!u.metrics_url.is_empty(), "vllm_load")?;
        require(!u.api_key_ciphertext.is_empty(), "encrypted_api_key")?;
        if !u.api_key_ref.is_empty() && !u.api_key_ciphertext.is_empty() {
            return Err(ApplyError::invalid(format!(
                "upstream {:?} sets both api_key_ref and api_key_ciphertext",
                u.name
            )));
        }
        if let Some(h) = &u.health_check {
            if h.interval_ms < 1000 || h.timeout_ms == 0 || h.timeout_ms >= h.interval_ms {
                return Err(ApplyError::invalid(
                    "health interval must be >= 1000 ms and 0 < timeout < interval",
                ));
            }
            if !h.url.is_empty() && crate::router::plain_http_uri(&h.url).is_none() {
                return Err(ApplyError::invalid(
                    "health url must be an absolute http(s) URL without userinfo",
                ));
            }
        }
        known::<pb::UpstreamKind>(u.kind, false, &format!("upstream {:?} kind", u.name))?;
        // UNSPECIFIED is documented as INCLUDE_USAGE.
        known::<pb::StreamUsageMode>(
            u.stream_usage_mode,
            true,
            &format!("upstream {:?} stream_usage_mode", u.name),
        )?;
    }
    let models = unique(snapshot.models.iter().map(|d| d.name.as_str()), "model")?;
    for d in &snapshot.models {
        require(d.max_output_tokens > 0, "model_limits")?;
        require(!d.default_body_json.is_empty(), "default_body")?;
        require(d.reasoning_effort.is_some(), "reasoning_effort")?;
        require(d.chat_check.is_some(), "model_chat_check")?;
        if let Some(c) = &d.chat_check {
            if c.interval_ms < 1000 || c.timeout_ms == 0 || c.timeout_ms >= c.interval_ms {
                return Err(ApplyError::invalid(format!(
                    "model {:?} chat_check interval must be >= 1000 ms and 0 < timeout < interval",
                    d.name
                )));
            }
        }
        if !upstreams.contains(d.upstream.as_str()) {
            return Err(ApplyError::invalid(format!(
                "model {:?} references unknown upstream {:?}",
                d.name, d.upstream
            )));
        }
        if d.upstream_model.is_empty() {
            return Err(ApplyError::invalid(format!(
                "model {:?} has an empty upstream_model",
                d.name
            )));
        }
    }
    // Models, aliases and routers share one name space.
    let names = unique(
        snapshot
            .models
            .iter()
            .map(|d| d.name.as_str())
            .chain(snapshot.aliases.iter().map(|a| a.name.as_str()))
            .chain(snapshot.routers.iter().map(|r| r.name.as_str())),
        "name",
    )?;
    if names.contains("unknown") {
        return Err(ApplyError::invalid(
            "the name 'unknown' is reserved for metrics",
        ));
    }
    let routers: HashSet<&str> = snapshot.routers.iter().map(|r| r.name.as_str()).collect();
    for a in &snapshot.aliases {
        if !models.contains(a.target.as_str()) && !routers.contains(a.target.as_str()) {
            return Err(ApplyError::invalid(format!(
                "alias {:?} target {:?} is not a model or router",
                a.name, a.target
            )));
        }
    }
    for r in &snapshot.routers {
        require(r.retry.is_some(), "retry")?;
        require(!r.fallbacks.is_empty(), "fallbacks")?;
        require(
            !r.traffic_split.is_empty() || r.sticky != 0 || !r.sticky_header.is_empty(),
            "traffic_split",
        )?;
        let sticky = known::<pb::StickyBy>(r.sticky, true, "sticky")?;
        validate_split(r, sticky, &models)?;
        if let Some(p) = &r.retry {
            if p.max_attempts > 5 || p.retry_on_status.iter().any(|s| !(400..=599).contains(s)) {
                return Err(ApplyError::invalid(
                    "retry max_attempts must be <= 5 and statuses in 400..=599",
                ));
            }
        }
        for fallback in &r.fallbacks {
            if !models.contains(fallback.as_str()) {
                return Err(ApplyError::invalid(format!(
                    "router {:?} fallback {:?} is not a model",
                    r.name, fallback
                )));
            }
        }
        validate_strategy(r, &require)?;
        // With traffic_split the router's own member list is ignored.
        if r.traffic_split.is_empty() {
            validate_members(&r.name, "", &r.members, &models)?;
        }
    }
    // MCP servers and A2A agents: names, URLs, auth plugins, reviewed
    // definitions and cards (`targets::validate`). Everything they use needs a
    // declared capability: "mcp", "a2a", "auth:<plugin>",
    // "a2a_middleware:<plugin>".
    crate::targets::validate(snapshot)?;
    for cap in crate::targets::capabilities(snapshot) {
        require(true, &cap)?;
    }
    unique(snapshot.teams.iter().map(|t| t.id.as_str()), "team")?;
    let teams: HashMap<&str, &pb::Team> =
        snapshot.teams.iter().map(|t| (t.id.as_str(), t)).collect();
    unique(
        snapshot.api_keys.iter().map(|k| k.key_id.as_str()),
        "key id",
    )?;
    let mut hashes = HashSet::with_capacity(snapshot.api_keys.len());
    let mut scopes: HashMap<&str, Option<pb::RateLimit>> = HashMap::new();
    for key in &snapshot.api_keys {
        let id = &key.key_id;
        if key.key_hash.len() != DIGEST_LEN {
            return Err(ApplyError::invalid(format!(
                "key {id:?} hash must be 32 bytes"
            )));
        }
        if !hashes.insert(key.key_hash.as_slice()) {
            return Err(ApplyError::invalid(format!(
                "key {id:?} has a duplicate hash"
            )));
        }
        if key.previous_key_hash.is_empty() {
            if key.previous_key_expires_at_unix_ms != 0 {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} has a previous expiry without a hash"
                )));
            }
        } else {
            require(true, "rotation_grace")?;
            if key.previous_key_hash.len() != DIGEST_LEN {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} previous hash must be 32 bytes"
                )));
            }
            if key.previous_key_expires_at_unix_ms <= 0 {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} previous hash needs a positive expiry"
                )));
            }
            if !hashes.insert(key.previous_key_hash.as_slice()) {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} has a duplicate previous hash"
                )));
            }
        }
        known::<pb::KeyStatus>(key.status, false, &format!("key {id:?} status"))?;
        if key.expires_at_unix_ms < 0 {
            return Err(ApplyError::invalid(format!(
                "key {id:?} has a negative expiry"
            )));
        }
        if !key.team_id.is_empty() && !teams.contains_key(key.team_id.as_str()) {
            return Err(ApplyError::invalid(format!(
                "key {id:?} references unknown team {:?}",
                key.team_id
            )));
        }
        let policy = key
            .policy
            .as_ref()
            .ok_or_else(|| ApplyError::invalid(format!("key {id:?} has no policy")))?;
        if policy.allowed_models.iter().any(String::is_empty) {
            return Err(ApplyError::invalid(format!(
                "key {id:?} allows an empty model name"
            )));
        }
        if policy.rate_limit_scopes.len() > MAX_RATE_LIMIT_SCOPES {
            return Err(ApplyError::invalid(format!(
                "key {id:?} has {} rate limit scopes, over the limit of {MAX_RATE_LIMIT_SCOPES}",
                policy.rate_limit_scopes.len()
            )));
        }
        let mut own = HashSet::new();
        for scope in &policy.rate_limit_scopes {
            if scope.id.is_empty() || scope.limit.is_none() {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} has a rate limit scope without id or limit"
                )));
            }
            if !own.insert(scope.id.as_str()) {
                return Err(ApplyError::invalid(format!(
                    "key {id:?} repeats rate limit scope {:?}",
                    scope.id
                )));
            }
            // One scope id names one shared limiter; every key must agree on it.
            if *scopes.entry(&scope.id).or_insert(scope.limit) != scope.limit {
                return Err(ApplyError::invalid(format!(
                    "rate limit scope {:?} has conflicting limits",
                    scope.id
                )));
            }
        }
    }
    Ok(())
}

/// An unknown name is `UNSUPPORTED_FEATURE`, also when the capability is not
/// listed: the gateway cannot serve it either way.
fn validate_strategy(
    r: &pb::Router,
    require: &impl Fn(bool, &str) -> Result<(), ApplyError>,
) -> Result<(), ApplyError> {
    if !valid_strategy_name(&r.strategy) {
        return Err(ApplyError::invalid(format!(
            "router {:?} has an invalid strategy name",
            r.name
        )));
    }
    let builtin = builtin_strategy(&r.strategy).is_some();
    if !builtin
        && lumewarp_plugin_host::global().get(1, &r.strategy).is_none()
        && lumewarp_router_api::lookup(&r.strategy)
            .map_err(|_| ApplyError::invalid("duplicate strategy registration"))?
            .is_none()
    {
        return Err(ApplyError::unsupported(format!(
            "unregistered strategy {:?}",
            r.strategy
        )));
    }
    require(true, &format!("strategy:{}", r.strategy))?;
    strategy_headers(r)?;
    if builtin
        && strategy_params(r)?
            .as_object()
            .is_some_and(|p| !p.is_empty())
    {
        return Err(ApplyError::invalid(format!(
            "router {:?}: built-in strategy {:?} takes no params",
            r.name, r.strategy
        )));
    }
    Ok(())
}

/// A nonempty list of distinct known models with well-formed settings.
fn validate_members(
    router: &str,
    split: &str,
    members: &[pb::RouterMember],
    models: &HashSet<&str>,
) -> Result<(), ApplyError> {
    let fail = |what: String| {
        Err(ApplyError::invalid(format!(
            "router {router:?}{split}: {what}"
        )))
    };
    if members.is_empty() {
        return fail("has no models".into());
    }
    let mut seen = HashSet::new();
    for m in members {
        if !models.contains(m.model.as_str()) {
            return fail(format!("references unknown model {:?}", m.model));
        }
        if !seen.insert(m.model.as_str()) {
            return fail(format!("repeats model {:?}", m.model));
        }
        params_object(&m.params_json, "member params")?;
    }
    Ok(())
}

/// Split names are bounded metric labels; every split names known models
/// and the weights must leave something to router to.
fn validate_split(
    r: &pb::Router,
    sticky: pb::StickyBy,
    models: &HashSet<&str>,
) -> Result<(), ApplyError> {
    let name = &r.name;
    let fail = |what: String| Err(ApplyError::invalid(format!("router {name:?}: {what}")));
    match sticky {
        pb::StickyBy::Header => {
            if r.sticky_header.is_empty()
                || hyper::header::HeaderName::from_bytes(r.sticky_header.as_bytes()).is_err()
            {
                return fail("sticky=header needs a valid sticky_header".into());
            }
        }
        _ if !r.sticky_header.is_empty() => {
            return fail("sticky_header is only used with sticky=header".into());
        }
        _ => {}
    }
    if r.traffic_split.is_empty() {
        if sticky != pb::StickyBy::None {
            return fail("sticky needs a traffic_split".into());
        }
        return Ok(());
    }
    if r.traffic_split.len() > MAX_SPLITS {
        return fail(format!("more than {MAX_SPLITS} traffic splits"));
    }
    let mut names = HashSet::new();
    let mut total = 0u64;
    for split in &r.traffic_split {
        let name = &split.name;
        if name.is_empty()
            || name.len() > MAX_SPLIT_NAME
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        {
            return fail(format!(
                "split name {name:?} must be 1..={MAX_SPLIT_NAME} of [A-Za-z0-9_.-]"
            ));
        }
        if !names.insert(name.as_str()) {
            return fail(format!("duplicate split {name:?}"));
        }
        validate_members(&r.name, &format!(" split {name:?}"), &split.members, models)?;
        total += u64::from(split.weight);
    }
    if total == 0 {
        return fail("traffic split weights must sum to more than 0".into());
    }
    Ok(())
}

/// Removes, then upserts by key; rejects duplicate or contradictory entries.
fn merge<T>(
    items: &mut Vec<T>,
    upserts: Vec<T>,
    removes: &[String],
    key: impl Fn(&T) -> &str,
    kind: &str,
) -> Result<(), ApplyError> {
    {
        let mut seen = HashSet::new();
        for item in &upserts {
            if !seen.insert(key(item)) {
                return Err(ApplyError::invalid(format!(
                    "delta upserts {kind} {:?} twice",
                    key(item)
                )));
            }
        }
        if let Some(name) = removes.iter().find(|name| seen.contains(name.as_str())) {
            return Err(ApplyError::invalid(format!(
                "delta both upserts and removes {kind} {name:?}"
            )));
        }
    }
    if !removes.is_empty() {
        let removes: HashSet<&str> = removes.iter().map(String::as_str).collect();
        items.retain(|item| !removes.contains(key(item)));
    }
    if upserts.is_empty() {
        return Ok(());
    }
    let mut index: HashMap<String, usize> = items
        .iter()
        .enumerate()
        .map(|(i, item)| (key(item).to_owned(), i))
        .collect();
    for item in upserts {
        match index.get(key(&item)) {
            Some(&i) => items[i] = item,
            None => {
                index.insert(key(&item).to_owned(), items.len());
                items.push(item);
            }
        }
    }
    Ok(())
}

/// Produces the target snapshot of an update on a private copy. The caller
/// validates and compiles the result before persisting or swapping.
pub fn apply(base: &Snapshot, update: ConfigUpdate) -> Result<Snapshot, ApplyError> {
    match update.update {
        None => Err(ApplyError::invalid("empty config update")),
        Some(Update::Full(full)) => {
            if !base.scope.is_empty() && full.scope != base.scope {
                return Err(ApplyError::invalid(
                    "full snapshot is for a different scope",
                ));
            }
            Ok(full)
        }
        Some(Update::Delta(delta)) => {
            if delta.base_version != base.version {
                return Err(ApplyError {
                    code: NackCode::BaseMismatch,
                    reason: format!(
                        "delta base {} but running {}",
                        delta.base_version, base.version
                    ),
                });
            }
            if delta.version <= delta.base_version {
                return Err(ApplyError::invalid(format!(
                    "delta version {} does not advance base {}",
                    delta.version, delta.base_version
                )));
            }
            let mut next = base.clone();
            merge(
                &mut next.upstreams,
                delta.upsert_upstreams,
                &delta.remove_upstreams,
                |u| &u.name,
                "upstream",
            )?;
            merge(
                &mut next.models,
                delta.upsert_models,
                &delta.remove_models,
                |d| &d.name,
                "model",
            )?;
            merge(
                &mut next.aliases,
                delta.upsert_aliases,
                &delta.remove_aliases,
                |a| &a.name,
                "alias",
            )?;
            merge(
                &mut next.routers,
                delta.upsert_routers,
                &delta.remove_routers,
                |r| &r.name,
                "router",
            )?;
            merge(
                &mut next.api_keys,
                delta.upsert_api_keys,
                &delta.remove_api_keys,
                |k| &k.key_id,
                "key",
            )?;
            merge(
                &mut next.teams,
                delta.upsert_teams,
                &delta.remove_teams,
                |t| &t.id,
                "team",
            )?;
            // MCP servers and A2A agents are keyed by name: `McpServer.id` /
            // `A2aAgent.id` is the stable identity usage and reports carry, but
            // a rename is an upsert under the new name plus a removal.
            merge(
                &mut next.mcp_servers,
                delta.upsert_mcp_servers,
                &delta.remove_mcp_servers,
                |s| &s.name,
                "mcp server",
            )?;
            merge(
                &mut next.a2a_agents,
                delta.upsert_a2a_agents,
                &delta.remove_a2a_agents,
                |a| &a.name,
                "a2a agent",
            )?;
            next.version = delta.version;
            next.created_at_unix_ms = delta.created_at_unix_ms;
            next.required_capabilities = delta.required_capabilities;
            Ok(next)
        }
    }
}

/// Atomically replaces `<dir>/snapshot.pb`. Equivalent to `store_with_digest`
/// with an empty digest.
#[cfg(test)]
pub fn store(dir: &Path, snapshot: &Snapshot) -> io::Result<()> {
    store_with_digest(dir, snapshot, &[])
}

/// Atomically replaces `<dir>/snapshot.pb`: `FILE_MAGIC || payload || digest ||
/// len(digest) as u32 BE || sha256(everything before)`, written to a 0600 temp file,
/// fsynced, renamed, then the directory fsynced. `digest` is the control
/// plane's opaque `target_digest`, kept in the same file so the two can never
/// diverge. Creates `dir` (0700) if missing. Blocking: call from the apply
/// task only.
pub fn store_with_digest(dir: &Path, snapshot: &Snapshot, digest: &[u8]) -> io::Result<()> {
    if snapshot.encoded_len() > MAX_SNAPSHOT_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "snapshot exceeds the size cap",
        ));
    }
    let digest_len = u32::try_from(digest.len())
        .ok()
        .filter(|&n| n <= MAX_DIGEST_BYTES)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "digest too large"))?;
    let mut data =
        Vec::with_capacity(FILE_MAGIC.len() + snapshot.encoded_len() + digest.len() + LEN_FIELD);
    data.extend_from_slice(FILE_MAGIC);
    snapshot
        .encode(&mut data)
        .expect("the vector has room for the snapshot");
    data.extend_from_slice(digest);
    data.extend_from_slice(&digest_len.to_be_bytes());
    let checksum = Sha256::digest(&data);
    create_dir(dir)?;
    let tmp = dir.join(TMP_NAME);
    {
        let mut options = OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let mut file = options.open(&tmp)?;
        // `mode` applies only on creation; a leftover temp file may differ.
        #[cfg(unix)]
        file.set_permissions(std::os::unix::fs::PermissionsExt::from_mode(0o600))?;
        file.write_all(&data)?;
        file.write_all(&checksum)?;
        file.sync_all()?;
    }
    fs::rename(&tmp, dir.join(FILE_NAME))?;
    File::open(dir)?.sync_all()
}

fn create_dir(dir: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(dir)
}

/// Reads `<dir>/snapshot.pb`. `Ok(None)` when absent; corrupt, oversized,
/// old-format or wrong-scope files are errors: the caller must not serve from
/// them and fetches a full snapshot instead.
#[cfg(test)]
pub fn load(dir: &Path, scope: &str) -> Result<Option<Snapshot>, LoadError> {
    Ok(load_with_digest(dir, scope)?.map(|(snapshot, _)| snapshot))
}

/// Like `load`, also returning the persisted `target_digest`.
pub fn load_with_digest(dir: &Path, scope: &str) -> Result<Option<(Snapshot, Vec<u8>)>, LoadError> {
    let file = match File::open(dir.join(FILE_NAME)) {
        Ok(file) => file,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err.into()),
    };
    let cap =
        FILE_MAGIC.len() + MAX_SNAPSHOT_BYTES + MAX_DIGEST_BYTES as usize + DIGEST_LEN + LEN_FIELD;
    let mut data = Vec::new();
    file.take(cap as u64 + 1).read_to_end(&mut data)?;
    if data.len() > cap {
        return Err(LoadError::TooLarge);
    }
    if data.len() < DIGEST_LEN + LEN_FIELD {
        return Err(LoadError::Corrupt);
    }
    let (body, checksum) = data.split_at(data.len() - DIGEST_LEN);
    if Sha256::digest(body).as_slice() != checksum {
        return Err(LoadError::Corrupt);
    }
    // After the checksum, so that a damaged file stays `Corrupt`.
    let Some(body) = body.strip_prefix(FILE_MAGIC) else {
        return Err(LoadError::OldFormat);
    };
    if body.len() < LEN_FIELD {
        return Err(LoadError::Corrupt);
    }
    let (rest, len) = body.split_at(body.len() - LEN_FIELD);
    let len = u32::from_be_bytes(len.try_into().expect("4 bytes")) as usize;
    if len > rest.len() {
        return Err(LoadError::Corrupt);
    }
    let (payload, digest) = rest.split_at(rest.len() - len);
    let snapshot = Snapshot::decode(payload).map_err(|_| LoadError::Corrupt)?;
    if snapshot.scope != scope {
        return Err(LoadError::WrongScope {
            found: snapshot.scope,
        });
    }
    Ok(Some((snapshot, digest.to_vec())))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use pb::Delta;

    #[test]
    fn strategy_headers_reject_credentials_case_insensitively() {
        for header in [
            "authorization",
            "Authorization",
            "proxy-authorization",
            "Cookie",
            "set-cookie",
            "X-API-Key",
            "x-lumewarp-key",
            "X-Lumewarp-Key",
        ] {
            let router = pb::Router {
                params_json: serde_json::json!({"headers": [header]}).to_string(),
                ..Default::default()
            };
            assert_eq!(
                strategy_headers(&router).unwrap_err().code,
                NackCode::InvalidConfig
            );
        }
        let router = pb::Router {
            params_json: r#"{"headers":["X-Tenant","x-request-id"]}"#.into(),
            ..Default::default()
        };
        assert_eq!(
            strategy_headers(&router).unwrap(),
            vec!["x-tenant", "x-request-id"]
        );
    }

    pub fn upstream(name: &str) -> pb::Upstream {
        pb::Upstream {
            name: name.into(),
            kind: pb::UpstreamKind::Vllm as i32,
            base_url: "http://localhost:8000/v1".into(),
            timeout_ms: 1000,
            ..Default::default()
        }
    }

    pub fn model(name: &str, upstream: &str) -> pb::Model {
        pb::Model {
            name: name.into(),
            upstream: upstream.into(),
            upstream_model: "actual".into(),
            price: None,
            ..Default::default()
        }
    }

    pub fn member(model: &str, weight: u32) -> pb::RouterMember {
        pb::RouterMember {
            model: model.into(),
            weight,
            ..Default::default()
        }
    }

    /// Weighted, every member with weight 1.
    pub fn router(name: &str, models: &[&str]) -> pb::Router {
        pb::Router {
            name: name.into(),
            members: models.iter().map(|d| member(d, 1)).collect(),
            strategy: "weighted".into(),
            ..Default::default()
        }
    }

    pub fn team(id: &str) -> pb::Team {
        pb::Team {
            id: id.into(),
            name: id.into(),
        }
    }

    pub fn key(id: &str, secret: &str, team: &str, models: &[&str]) -> pb::ApiKey {
        pb::ApiKey {
            key_id: id.into(),
            key_hash: Sha256::digest(secret.as_bytes()).to_vec(),
            team_id: team.into(),
            status: pb::KeyStatus::Active as i32,
            expires_at_unix_ms: 0,
            policy: Some(pb::EffectivePolicy {
                allow_all_models: false,
                allowed_models: models.iter().map(|m| m.to_string()).collect(),
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    pub fn base() -> Snapshot {
        Snapshot {
            version: 1,
            upstreams: vec![upstream("u"), upstream("v")],
            models: vec![model("a", "u"), model("b", "v")],
            routers: vec![router("m1", &["a"]), router("m2", &["a", "b"])],
            api_keys: vec![key("k1", "s1", "t1", &["m1"]), key("k2", "s2", "", &[])],
            teams: vec![team("t1"), team("t2")],
            created_at_unix_ms: 10,
            required_capabilities: vec!["strategy:weighted".into()],
            scope: "scope-a".into(),
            aliases: vec![],
            ..Default::default()
        }
    }

    fn delta(version: u64) -> Delta {
        Delta {
            base_version: 1,
            version,
            created_at_unix_ms: 20,
            ..Default::default()
        }
    }

    fn apply_delta(delta: Delta) -> Result<Snapshot, ApplyError> {
        apply(
            &base(),
            ConfigUpdate {
                update: Some(Update::Delta(delta)),
                ..Default::default()
            },
        )
    }

    fn assert_code(result: Result<impl std::fmt::Debug, ApplyError>, code: NackCode) {
        assert_eq!(result.unwrap_err().code, code);
    }

    fn invalid(mutate: impl FnOnce(&mut Snapshot)) -> ApplyError {
        let mut s = base();
        mutate(&mut s);
        let err = validate(&s, SUPPORTED_CAPABILITIES).unwrap_err();
        assert_eq!(err.code, NackCode::InvalidConfig, "{err}");
        err
    }

    #[test]
    fn base_is_valid() {
        validate(&base(), SUPPORTED_CAPABILITIES).unwrap();
        validate(&Snapshot::default(), SUPPORTED_CAPABILITIES).unwrap();
    }

    #[test]
    fn resilience_fields_require_their_capabilities() {
        for cap in SUPPORTED_CAPABILITIES {
            let mut s = base();
            s.required_capabilities.clear();
            s.routers.clear();
            let mut r = router("r", &["a"]);
            r.strategy = "least_in_flight".into();
            s.required_capabilities
                .push("strategy:least_in_flight".into());
            match *cap {
                "retry" => r.retry = Some(pb::RetryPolicy::default()),
                "fallbacks" => r.fallbacks = vec!["b".into()],
                "health_check" => {
                    s.upstreams[0].health_check = Some(pb::HealthCheck {
                        interval_ms: 1000,
                        timeout_ms: 100,
                        ..Default::default()
                    })
                }
                "circuit_breaker" => {
                    s.upstreams[0].circuit_breaker = Some(pb::CircuitBreaker::default())
                }
                "traffic_split" => r.traffic_split = vec![split("stable", &["a"], 1)],
                "vllm_load" => s.upstreams[0].metrics_url = "http://h/metrics".into(),
                "encrypted_api_key" => s.upstreams[0].api_key_ciphertext = vec![1; 40],
                "model_limits" => s.models[0].max_output_tokens = 10,
                "default_body" => s.models[0].default_body_json = r#"{"a":1}"#.into(),
                "model_chat_check" => {
                    s.models[0].chat_check = Some(pb::ChatCheck {
                        interval_ms: 1000,
                        timeout_ms: 100,
                        ..Default::default()
                    })
                }
                "reasoning_effort" => {
                    s.models[0].reasoning_effort = Some(pb::ReasoningEffort::default())
                }
                "mcp" => {
                    s.mcp_servers = vec![pb::McpServer {
                        id: "s1-id".into(),
                        name: "s1".into(),
                        url: "https://mcp.example.test/mcp".into(),
                        ..Default::default()
                    }]
                }
                "a2a" => {
                    s.a2a_agents = vec![pb::A2aAgent {
                        id: "a1-id".into(),
                        name: "a1".into(),
                        url: "https://agent.example.test/rpc".into(),
                        ..Default::default()
                    }]
                }
                "strategy:least_in_flight" | "rotation_grace" => continue,
                strategy => {
                    r.strategy = strategy.strip_prefix("strategy:").unwrap().into();
                    s.required_capabilities.clear();
                }
            }
            s.routers.push(r);
            assert_code(
                validate(&s, SUPPORTED_CAPABILITIES),
                NackCode::InvalidConfig,
            );
            s.required_capabilities.push((*cap).into());
            validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        }
    }

    #[test]
    fn strategy_names_and_params() {
        // A known built-in without its capability is a broken compiler output.
        let mut s = base();
        s.required_capabilities.clear();
        assert_code(
            validate(&s, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
        );
        // An unknown name cannot be served, whatever the capabilities say.
        for caps in [vec![], vec!["strategy:not_compiled".to_owned()]] {
            let mut s = base();
            s.routers[0].strategy = "not_compiled".into();
            s.required_capabilities.extend(caps);
            let supported = ["strategy:weighted", "strategy:not_compiled"];
            assert_code(validate(&s, &supported), NackCode::UnsupportedFeature);
        }
        for bad in ["", "Weighted", "has space", "9lives"] {
            invalid(|s| s.routers[0].strategy = bad.into());
        }
        invalid(|s| s.routers[0].params_json = r#"{"zone":"a"}"#.into());
        invalid(|s| s.routers[0].params_json = "[]".into());
        invalid(|s| s.routers[0].params_json = "{".into());
        invalid(|s| s.routers[0].params_json = format!(r#"{{"x":"{}"}}"#, "y".repeat(16 * 1024)));
        invalid(|s| s.routers[0].members[0].params_json = "[]".into());
        invalid(|s| {
            s.routers[0].members[0].params_json = format!(r#"{{"x":"{}"}}"#, "y".repeat(16 * 1024))
        });
        // Empty object and member settings are fine, also on a built-in: only
        // a strategy that declares them reads them.
        let mut s = base();
        s.routers[0].params_json = "{}".into();
        s.routers[0].members[0].params_json = r#"{"zone":"a"}"#.into();
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
    }

    #[test]
    fn names_are_one_name_space_and_aliases_are_one_level() {
        let alias = |name: &str, target: &str| pb::Alias {
            name: name.into(),
            target: target.into(),
        };
        let mut s = base();
        s.aliases = vec![alias("to-model", "a"), alias("to-router", "m2")];
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        // Across kinds, every pair.
        invalid(|s| s.aliases = vec![alias("a", "m1")]);
        invalid(|s| s.aliases = vec![alias("m1", "a")]);
        invalid(|s| s.routers[0].name = "a".into());
        invalid(|s| s.aliases = vec![alias("x", "a"), alias("x", "b")]);
        // Reserved for every kind.
        invalid(|s| s.aliases = vec![alias("unknown", "a")]);
        invalid(|s| s.models[0].name = "unknown".into());
        // Target must exist and must not be an alias.
        invalid(|s| s.aliases = vec![alias("x", "nope")]);
        invalid(|s| s.aliases = vec![alias("x", "a"), alias("y", "x")]);
        invalid(|s| s.aliases = vec![alias("x", "x")]);
        // Router members and fallbacks are models only.
        invalid(|s| s.routers[0].members = vec![member("m2", 1)]);
        invalid(|s| {
            s.aliases = vec![alias("x", "a")];
            s.routers[0].members = vec![member("x", 1)];
        });
        invalid(|s| s.routers[0].members = vec![member("a", 1), member("a", 1)]);
        invalid(|s| {
            s.required_capabilities.push("fallbacks".into());
            s.routers[0].fallbacks = vec!["m2".into()];
        });
        invalid(|s| {
            s.required_capabilities.push("fallbacks".into());
            s.aliases = vec![alias("x", "a")];
            s.routers[0].fallbacks = vec!["x".into()];
        });
    }

    #[test]
    fn delta_upserts_and_removes_aliases() {
        let mut s = base();
        s.aliases = vec![
            pb::Alias {
                name: "keep".into(),
                target: "a".into(),
            },
            pb::Alias {
                name: "drop".into(),
                target: "a".into(),
            },
        ];
        let d = Delta {
            base_version: 1,
            version: 2,
            required_capabilities: s.required_capabilities.clone(),
            upsert_aliases: vec![pb::Alias {
                name: "keep".into(),
                target: "m1".into(),
            }],
            remove_aliases: vec!["drop".into()],
            ..Default::default()
        };
        let next = apply(
            &s,
            ConfigUpdate {
                update: Some(Update::Delta(d)),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(next.aliases.len(), 1);
        assert_eq!(next.aliases[0].target, "m1");
        validate(&next, SUPPORTED_CAPABILITIES).unwrap();
    }

    #[test]
    fn retry_fallback_and_health_boundaries() {
        for (attempts, statuses, valid) in [
            (0, vec![], true),
            (5, vec![400, 599], true),
            (6, vec![], false),
            (2, vec![399], false),
            (2, vec![600], false),
        ] {
            let mut s = base();
            s.required_capabilities.push("retry".into());
            s.routers[0].retry = Some(pb::RetryPolicy {
                max_attempts: attempts,
                retry_on_status: statuses,
                total_timeout_ms: 10,
            });
            assert_eq!(validate(&s, SUPPORTED_CAPABILITIES).is_ok(), valid);
        }
        for (interval, timeout, valid) in [
            (1000, 999, true),
            (999, 1, false),
            (1000, 1000, false),
            (1000, 0, false),
        ] {
            let mut s = base();
            s.required_capabilities.push("health_check".into());
            s.upstreams[0].health_check = Some(pb::HealthCheck {
                interval_ms: interval,
                timeout_ms: timeout,
                ..Default::default()
            });
            assert_eq!(validate(&s, SUPPORTED_CAPABILITIES).is_ok(), valid);
        }
        for fallback in ["m2", "missing"] {
            let mut s = base();
            s.required_capabilities.push("fallbacks".into());
            s.routers[0].fallbacks.push(fallback.into());
            assert_code(
                validate(&s, SUPPORTED_CAPABILITIES),
                NackCode::InvalidConfig,
            );
        }
    }

    pub fn split(name: &str, models: &[&str], weight: u32) -> pb::TrafficSplit {
        pb::TrafficSplit {
            name: name.into(),
            members: models.iter().map(|d| member(d, 1)).collect(),
            weight,
        }
    }

    #[test]
    fn traffic_split_and_sticky_validation() {
        let check = |mutate: &dyn Fn(&mut pb::Router)| {
            let mut s = base();
            s.required_capabilities.push("traffic_split".into());
            s.routers[1].traffic_split =
                vec![split("stable", &["a"], 9), split("canary", &["b"], 1)];
            mutate(&mut s.routers[1]);
            validate(&s, SUPPORTED_CAPABILITIES)
        };
        check(&|_| {}).unwrap();
        // The router's own list is ignored (may be empty) with splits.
        check(&|r| r.members.clear()).unwrap();
        check(&|r| r.traffic_split[1].weight = 0).unwrap();
        check(&|r| {
            r.sticky = pb::StickyBy::Header as i32;
            r.sticky_header = "X-Session".into();
        })
        .unwrap();
        check(&|r| r.sticky = pb::StickyBy::Team as i32).unwrap();
        check(&|r| r.sticky = pb::StickyBy::Key as i32).unwrap();
        type Mutation = Box<dyn Fn(&mut pb::Router)>;
        let bad: Vec<Mutation> = vec![
            Box::new(|r| {
                r.traffic_split[0].weight = 0;
                r.traffic_split[1].weight = 0;
            }),
            Box::new(|r| r.traffic_split[1].name = "stable".into()),
            Box::new(|r| r.traffic_split[1].name.clear()),
            Box::new(|r| r.traffic_split[1].name = "x".repeat(MAX_SPLIT_NAME + 1)),
            Box::new(|r| r.traffic_split[1].name = "can ary".into()),
            Box::new(|r| r.traffic_split[1].members.clear()),
            Box::new(|r| r.traffic_split[1].members = vec![member("nope", 1)]),
            Box::new(|r| r.traffic_split[1].members = vec![member("b", 1), member("b", 1)]),
            Box::new(|r| r.sticky = pb::StickyBy::Header as i32),
            Box::new(|r| {
                r.sticky = pb::StickyBy::Header as i32;
                r.sticky_header = "bad header".into();
            }),
            Box::new(|r| r.sticky_header = "x-session".into()),
            Box::new(|r| r.sticky = 99),
            Box::new(|r| {
                r.traffic_split = (0..=MAX_SPLITS)
                    .map(|i| split(&format!("s{i}"), &["a"], 1))
                    .collect()
            }),
            Box::new(|r| {
                r.traffic_split.clear();
                r.sticky = pb::StickyBy::Key as i32;
            }),
        ];
        for mutate in &bad {
            assert_code(check(mutate.as_ref()), NackCode::InvalidConfig);
        }
        // Without the capability the fields are an error, not ignored.
        let mut s = base();
        s.routers[0].traffic_split = vec![split("stable", &["a"], 1)];
        assert_code(
            validate(&s, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
        );
        // An unsupported capability is still a NACK of its own kind.
        s.required_capabilities.push("traffic_split".into());
        assert_code(validate(&s, &[]), NackCode::UnsupportedFeature);
    }

    #[test]
    fn delta_upserts_and_removes_every_entity_type() {
        let mut d = delta(2);
        let mut u = upstream("u");
        u.timeout_ms = 5;
        d.upsert_upstreams = vec![u, upstream("w")];
        d.remove_upstreams = vec!["v".into(), "missing".into()];
        let mut a = model("a", "w");
        a.upstream_model = "changed".into();
        d.upsert_models = vec![a, model("c", "u")];
        d.remove_models = vec!["b".into()];
        d.upsert_routers = vec![router("m2", &["c"]), router("m3", &["a"])];
        d.remove_routers = vec!["m1".into()];
        let mut k2 = key("k2", "s2-rotated", "t2", &["m3"]);
        k2.status = pb::KeyStatus::Revoked as i32;
        d.upsert_api_keys = vec![k2, key("k3", "s3", "", &["m2"])];
        d.remove_api_keys = vec!["k1".into()];
        d.upsert_teams = vec![
            team("t3"),
            pb::Team {
                name: "renamed".into(),
                ..team("t2")
            },
        ];
        d.remove_teams = vec!["t1".into()];
        d.required_capabilities = vec!["x".into()];
        let next = apply_delta(d).unwrap();
        assert_eq!(next.version, 2);
        assert_eq!(next.created_at_unix_ms, 20);
        assert_eq!(next.required_capabilities, ["x"]);
        assert_eq!(next.scope, "scope-a");
        let names = |v: Vec<&str>| v.into_iter().map(String::from).collect::<Vec<_>>();
        assert_eq!(
            next.upstreams
                .iter()
                .map(|u| u.name.clone())
                .collect::<Vec<_>>(),
            names(vec!["u", "w"])
        );
        assert_eq!(next.upstreams[0].timeout_ms, 5);
        assert_eq!(
            next.models
                .iter()
                .map(|d| d.name.clone())
                .collect::<Vec<_>>(),
            names(vec!["a", "c"])
        );
        assert_eq!(next.models[0].upstream_model, "changed");
        assert_eq!(
            next.routers
                .iter()
                .map(|r| r.name.clone())
                .collect::<Vec<_>>(),
            names(vec!["m2", "m3"])
        );
        assert_eq!(next.routers[0].members[0].model, "c");
        assert_eq!(
            next.api_keys
                .iter()
                .map(|k| k.key_id.clone())
                .collect::<Vec<_>>(),
            names(vec!["k2", "k3"])
        );
        assert_eq!(next.api_keys[0].status, pb::KeyStatus::Revoked as i32);
        assert_eq!(
            next.teams.iter().map(|t| t.id.clone()).collect::<Vec<_>>(),
            names(vec!["t2", "t3"])
        );
        assert_eq!(next.teams[0].name, "renamed");
        // The required capability makes the result unsupported here.
        assert_code(
            validate(&next, SUPPORTED_CAPABILITIES),
            NackCode::UnsupportedFeature,
        );
        // The delta's capability list replaced the old one, strategy included.
        assert_code(validate(&next, &["x"]), NackCode::InvalidConfig);
        let mut with_strategy = next.clone();
        with_strategy
            .required_capabilities
            .push("strategy:weighted".into());
        validate(&with_strategy, &["x", "strategy:weighted"]).unwrap();
    }

    #[test]
    fn delta_metadata_is_replaced_wholesale() {
        let mut s = base();
        s.required_capabilities = vec!["old".into()];
        let next = apply(
            &s,
            ConfigUpdate {
                update: Some(Update::Delta(delta(3))),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(next.required_capabilities.is_empty());
        assert_eq!(next.created_at_unix_ms, 20);
        assert_eq!(next.api_keys, s.api_keys);
    }

    #[test]
    fn delta_errors() {
        let mut d = delta(2);
        d.base_version = 7;
        assert_code(apply_delta(d), NackCode::BaseMismatch);
        assert_code(apply_delta(delta(1)), NackCode::InvalidConfig);
        let mut d = delta(2);
        d.upsert_api_keys = vec![key("k9", "a", "", &[]), key("k9", "b", "", &[])];
        assert_code(apply_delta(d), NackCode::InvalidConfig);
        let mut d = delta(2);
        d.upsert_teams = vec![team("t2")];
        d.remove_teams = vec!["t2".into()];
        assert_code(apply_delta(d), NackCode::InvalidConfig);
        assert_code(
            apply(&base(), ConfigUpdate::default()),
            NackCode::InvalidConfig,
        );
        // Removing a team still referenced by a key fails validation afterwards.
        let mut d = delta(2);
        d.remove_teams = vec!["t1".into()];
        let next = apply_delta(d).unwrap();
        assert_code(
            validate(&next, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
        );
        // Removing an upstream still used by a model, likewise.
        let mut d = delta(2);
        d.remove_upstreams = vec!["u".into()];
        let next = apply_delta(d).unwrap();
        assert_code(
            validate(&next, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
        );
    }

    #[test]
    fn mcp_servers_and_agents_apply_in_deltas_by_name() {
        let server = |name: &str, url: &str| pb::McpServer {
            id: format!("id-{name}"),
            name: name.into(),
            url: url.into(),
            ..Default::default()
        };
        let agent = |name: &str| pb::A2aAgent {
            id: format!("id-{name}"),
            name: name.into(),
            url: format!("https://{name}.example.test/rpc"),
            ..Default::default()
        };
        let mut d = delta(2);
        d.upsert_mcp_servers = vec![server("s1", "https://a.example.test/mcp")];
        d.upsert_a2a_agents = vec![agent("a1")];
        d.required_capabilities = vec!["strategy:weighted".into(), "mcp".into(), "a2a".into()];
        let next = apply_delta(d).unwrap();
        assert_eq!(next.mcp_servers.len(), 1);
        assert_eq!(next.mcp_servers[0].id, "id-s1");
        assert_eq!(next.a2a_agents[0].name, "a1");
        validate(&next, SUPPORTED_CAPABILITIES).unwrap();

        // An upsert of the same name replaces it in place (ids may change on a
        // control-plane re-create; the name is the key of the delta).
        let mut d = delta(3);
        d.base_version = 2;
        let mut recreated = server("s1", "https://b.example.test/mcp");
        recreated.id = "recreated".into();
        d.upsert_mcp_servers = vec![recreated];
        d.required_capabilities = vec!["strategy:weighted".into(), "mcp".into(), "a2a".into()];
        let next = apply(
            &next,
            ConfigUpdate {
                update: Some(Update::Delta(d)),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(next.mcp_servers.len(), 1);
        assert_eq!(next.mcp_servers[0].id, "recreated");
        assert_eq!(next.mcp_servers[0].url, "https://b.example.test/mcp");

        // A removal drops the whole entity; the capability can go too.
        let mut d = delta(4);
        d.base_version = 3;
        d.remove_mcp_servers = vec!["s1".into()];
        d.remove_a2a_agents = vec!["a1".into()];
        d.required_capabilities = vec!["strategy:weighted".into()];
        let next = apply(
            &next,
            ConfigUpdate {
                update: Some(Update::Delta(d)),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(next.mcp_servers.is_empty());
        assert!(next.a2a_agents.is_empty());
        validate(&next, SUPPORTED_CAPABILITIES).unwrap();

        // Duplicate upserts and upsert-plus-remove are refused like every other
        // entity, keyed by name (not id).
        let mut d = delta(2);
        d.upsert_mcp_servers = vec![
            server("s1", "https://a.example.test/mcp"),
            server("s1", "https://b.example.test/mcp"),
        ];
        assert_code(apply_delta(d), NackCode::InvalidConfig);
        let mut d = delta(2);
        d.upsert_a2a_agents = vec![agent("a1")];
        d.remove_a2a_agents = vec!["a1".into()];
        assert_code(apply_delta(d), NackCode::InvalidConfig);
    }

    #[test]
    fn mcp_and_a2a_entities_need_their_capabilities() {
        // A snapshot carrying them without the capability is a broken compiler
        // output (INVALID_CONFIG), not an unsupported feature.
        let error = invalid(|s| {
            s.mcp_servers = vec![pb::McpServer {
                id: "id-s1".into(),
                name: "s1".into(),
                url: "https://mcp.example.test/mcp".into(),
                ..Default::default()
            }]
        });
        assert!(
            error.reason.contains("capability \"mcp\""),
            "{}",
            error.reason
        );
        let error = invalid(|s| {
            s.a2a_agents = vec![pb::A2aAgent {
                id: "id-a1".into(),
                name: "a1".into(),
                url: "https://agent.example.test/rpc".into(),
                ..Default::default()
            }]
        });
        assert!(
            error.reason.contains("capability \"a2a\""),
            "{}",
            error.reason
        );
        // Target validation itself runs inside `validate`.
        let error = invalid(|s| {
            s.required_capabilities.push("mcp".into());
            s.mcp_servers = vec![pb::McpServer {
                id: "id-s1".into(),
                name: "s__1".into(),
                url: "https://mcp.example.test/mcp".into(),
                ..Default::default()
            }]
        });
        assert!(error.reason.contains("no \"__\""), "{}", error.reason);
        // The plugin capabilities come from the entities, not from the list.
        let error = invalid(|s| {
            s.required_capabilities.push("mcp".into());
            s.mcp_servers = vec![pb::McpServer {
                id: "id-s1".into(),
                name: "s1".into(),
                url: "https://mcp.example.test/mcp".into(),
                auth: Some(pb::UpstreamAuth {
                    plugin: Some(pb::PluginRef {
                        plugin: "hmac_signature".into(),
                        params_json: String::new(),
                        ..Default::default()
                    }),
                    ..Default::default()
                }),
                ..Default::default()
            }]
        });
        assert!(
            error.reason.contains("capability \"auth:hmac_signature\""),
            "{}",
            error.reason
        );
    }

    #[test]
    fn full_replaces_everything_and_checks_scope() {
        let mut full = base();
        full.version = 1;
        full.api_keys.clear();
        let update = |s: Snapshot| ConfigUpdate {
            update: Some(Update::Full(s)),
            ..Default::default()
        };
        // An older version is accepted (control plane restored from backup).
        let mut newer = base();
        newer.version = 9;
        assert_eq!(apply(&newer, update(full.clone())).unwrap(), full);
        // Booting without a snapshot: any scope is accepted.
        let mut other = base();
        other.scope = "scope-b".into();
        assert_eq!(
            apply(&Snapshot::default(), update(other.clone())).unwrap(),
            other
        );
        assert_code(apply(&base(), update(other)), NackCode::InvalidConfig);
    }

    #[test]
    fn unknown_capability_is_unsupported() {
        let mut s = base();
        s.required_capabilities.push("jwt".into());
        assert_code(validate(&s, &[]), NackCode::UnsupportedFeature);
        validate(&s, &["jwt", "strategy:weighted"]).unwrap();
        let mut s = base();
        s.routers[0].strategy = "lowest_latency".into();
        assert_code(
            validate(&s, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
        );
        s.required_capabilities =
            vec!["strategy:weighted".into(), "strategy:lowest_latency".into()];
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        assert_code(validate(&s, &[]), NackCode::UnsupportedFeature);
    }

    #[test]
    fn rotation_grace_is_valid_with_its_capability() {
        let mut s = base();
        s.required_capabilities.push("rotation_grace".into());
        s.api_keys[0].previous_key_hash = vec![7; 32];
        s.api_keys[0].previous_key_expires_at_unix_ms = 1;
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
    }

    #[test]
    fn invalid_references() {
        invalid(|s| s.models[0].upstream = "nope".into());
        invalid(|s| s.routers[0].members = vec![member("nope", 1)]);
        invalid(|s| s.routers[0].members.clear());
        invalid(|s| s.api_keys[0].team_id = "nope".into());
        invalid(|s| s.routers[0].name = "unknown".into());
        invalid(|s| s.models[0].upstream_model.clear());
    }

    #[test]
    fn invalid_keys() {
        invalid(|s| s.api_keys[0].key_hash.truncate(31));
        invalid(|s| s.api_keys[0].key_hash.push(0));
        invalid(|s| s.api_keys[0].key_hash.clear());
        invalid(|s| s.api_keys[1].key_id = "k1".into());
        invalid(|s| s.api_keys[1].key_id.clear());
        invalid(|s| s.api_keys[1].key_hash = s.api_keys[0].key_hash.clone());
        invalid(|s| s.api_keys[0].policy = None);
        invalid(|s| s.api_keys[0].expires_at_unix_ms = -1);
        // Rotation grace needs its capability, a 32-byte hash and its own expiry.
        invalid(|s| {
            s.api_keys[0].previous_key_hash = vec![7; 32];
            s.api_keys[0].previous_key_expires_at_unix_ms = 1;
        });
        invalid(|s| {
            s.required_capabilities.push("rotation_grace".into());
            s.api_keys[0].previous_key_hash = vec![7; 31];
            s.api_keys[0].previous_key_expires_at_unix_ms = 1;
        });
        invalid(|s| {
            s.required_capabilities.push("rotation_grace".into());
            s.api_keys[0].previous_key_hash = vec![7; 32];
        });
        invalid(|s| {
            s.required_capabilities.push("rotation_grace".into());
            s.api_keys[0].previous_key_hash = s.api_keys[0].key_hash.clone();
            s.api_keys[0].previous_key_expires_at_unix_ms = 1;
        });
        invalid(|s| {
            s.required_capabilities.push("rotation_grace".into());
            s.api_keys[0].previous_key_expires_at_unix_ms = 1;
        });
        invalid(|s| {
            s.api_keys[0]
                .policy
                .as_mut()
                .unwrap()
                .allowed_models
                .push(String::new())
        });
        let scope = |id: &str, rpm| pb::RateLimitScope {
            id: id.into(),
            limit: Some(pb::RateLimit {
                requests_per_minute: rpm,
                ..Default::default()
            }),
        };
        invalid(|s| {
            s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes =
                vec![scope("team:t1", 1), scope("team:t1", 1)]
        });
        invalid(|s| {
            s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes = vec![scope("team:t1", 1)];
            s.api_keys[1].policy.as_mut().unwrap().rate_limit_scopes = vec![scope("team:t1", 2)];
        });
        invalid(|s| {
            s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes = vec![pb::RateLimitScope {
                id: "x".into(),
                limit: None,
            }]
        });
        // Shared scopes with equal limits are fine.
        let mut s = base();
        s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes = vec![scope("team:t1", 1)];
        s.api_keys[1].policy.as_mut().unwrap().rate_limit_scopes = vec![scope("team:t1", 1)];
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        // Exactly four levels (org, team, owner, key) are accepted...
        let four = ["org:o1", "team:t1", "member:m1", "key:k1"];
        let mut s = base();
        s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes =
            four.iter().map(|id| scope(id, 100)).collect();
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        // ...a fifth is rejected before any request-path work is possible.
        let err = invalid(|s| {
            s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes = four
                .iter()
                .map(|id| scope(id, 100))
                .chain([scope("sa:s1", 100)])
                .collect()
        });
        assert!(err.reason.contains("over the limit of 4"), "{err}");
    }

    #[test]
    fn duplicates_and_unspecified_enums() {
        invalid(|s| s.upstreams[1].name = "u".into());
        invalid(|s| s.models[1].name = "a".into());
        invalid(|s| s.routers[1].name = "m1".into());
        invalid(|s| s.teams[1].id = "t1".into());
        invalid(|s| s.teams[1].id.clear());
        invalid(|s| s.upstreams[0].kind = 0);
        invalid(|s| s.upstreams[0].kind = 99);
        invalid(|s| s.upstreams[0].stream_usage_mode = 99);
        invalid(|s| s.api_keys[0].status = 0);
        invalid(|s| s.api_keys[0].status = 99);
        let mut s = base();
        s.upstreams[0].stream_usage_mode = 0;
        validate(&s, SUPPORTED_CAPABILITIES).unwrap();
    }

    #[test]
    fn too_many_keys() {
        let mut s = base();
        s.api_keys = (0..=MAX_KEYS)
            .map(|i| {
                let mut hash = vec![0; 32];
                hash[..8].copy_from_slice(&(i as u64).to_le_bytes());
                pb::ApiKey {
                    key_id: format!("k{i}"),
                    key_hash: hash,
                    ..key("x", "x", "", &[])
                }
            })
            .collect();
        assert!(invalid(|x| *x = s).reason.contains("keys"));
    }

    fn temp_dir() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    #[test]
    fn store_and_load_round_trip() {
        let root = temp_dir();
        let dir = root.path().join("state");
        assert!(load(&dir, "scope-a").unwrap().is_none());
        store(&dir, &base()).unwrap();
        assert_eq!(load(&dir, "scope-a").unwrap(), Some(base()));
        store_with_digest(&dir, &base(), b"opaque").unwrap();
        assert_eq!(
            load_with_digest(&dir, "scope-a").unwrap(),
            Some((base(), b"opaque".to_vec()))
        );
        let mut next = base();
        next.version = 2;
        store(&dir, &next).unwrap();
        assert_eq!(load(&dir, "scope-a").unwrap().unwrap().version, 2);
        assert!(!dir.join(TMP_NAME).exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&dir), 0o700);
            assert_eq!(mode(&dir.join(FILE_NAME)), 0o600);
        }
    }

    #[test]
    fn load_rejects_wrong_scope_corruption_and_truncation() {
        let root = temp_dir();
        let dir = root.path();
        store(dir, &base()).unwrap();
        assert!(matches!(
            load(dir, "scope-b"),
            Err(LoadError::WrongScope { found }) if found == "scope-a"
        ));
        let path = dir.join(FILE_NAME);
        let good = fs::read(&path).unwrap();
        // Flip one payload bit.
        let mut corrupt = good.clone();
        corrupt[3] ^= 1;
        fs::write(&path, &corrupt).unwrap();
        assert!(matches!(load(dir, "scope-a"), Err(LoadError::Corrupt)));
        // Truncated anywhere, including inside the digest.
        for len in [0, 10, good.len() - 32, good.len() - 1] {
            fs::write(&path, &good[..len]).unwrap();
            assert!(
                matches!(load(dir, "scope-a"), Err(LoadError::Corrupt)),
                "{len}"
            );
        }
        // Valid digest over bytes that are not a snapshot.
        let mut garbage = FILE_MAGIC.to_vec();
        garbage.extend_from_slice(&[0xff; 40]);
        garbage.extend_from_slice(&0u32.to_be_bytes());
        let digest = Sha256::digest(&garbage);
        garbage.extend_from_slice(&digest);
        fs::write(&path, &garbage).unwrap();
        assert!(matches!(load(dir, "scope-a"), Err(LoadError::Corrupt)));
    }

    /// Protobuf skips unknown fields, so a file of the previous format decodes
    /// into a snapshot with empty routers. It must not be trusted as a base.
    #[test]
    fn load_refuses_a_file_of_the_previous_format() {
        let root = temp_dir();
        let path = root.path().join(FILE_NAME);
        // Snapshot{ routers: [Router{ name: "r", models: ["d"], strategy:
        // WEIGHTED }], version: 7 } with the old field numbers, then the old
        // file trailer: digest, its length, checksum.
        let mut old = vec![
            0x08, 0x07, 0x22, 0x08, 0x0a, 0x01, b'r', 0x12, 0x01, b'd', 0x18, 0x01,
        ];
        assert_eq!(Snapshot::decode(old.as_slice()).unwrap().version, 7);
        assert!(Snapshot::decode(old.as_slice()).unwrap().routers[0]
            .members
            .is_empty());
        old.extend_from_slice(b"digest");
        old.extend_from_slice(&6u32.to_be_bytes());
        let checksum = Sha256::digest(&old);
        old.extend_from_slice(&checksum);
        fs::write(&path, &old).unwrap();
        assert!(matches!(load(root.path(), ""), Err(LoadError::OldFormat)));
        // The next store replaces it.
        store(root.path(), &base()).unwrap();
        assert_eq!(load(root.path(), "scope-a").unwrap(), Some(base()));
    }

    #[test]
    fn load_rejects_oversized_file() {
        let root = temp_dir();
        let path = root.path().join(FILE_NAME);
        let file = File::create(&path).unwrap();
        file.set_len(
            (FILE_MAGIC.len()
                + MAX_SNAPSHOT_BYTES
                + MAX_DIGEST_BYTES as usize
                + DIGEST_LEN
                + LEN_FIELD
                + 1) as u64,
        )
        .unwrap();
        assert!(matches!(load(root.path(), ""), Err(LoadError::TooLarge)));
    }

    #[test]
    fn store_replaces_leftover_temp_file() {
        let root = temp_dir();
        fs::write(root.path().join(TMP_NAME), b"partial write from a crash").unwrap();
        // The leftover never affects load, and the next store replaces it.
        assert!(load(root.path(), "scope-a").unwrap().is_none());
        store(root.path(), &base()).unwrap();
        assert_eq!(load(root.path(), "scope-a").unwrap(), Some(base()));
    }
}

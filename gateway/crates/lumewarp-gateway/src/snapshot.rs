//! Snapshot validation, delta application and on-disk persistence, per
//! `docs/config-protocol.md`. Everything here is off the request path: the
//! apply task calls `apply` -> `validate` -> `Router::compile` -> `store` ->
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
    "lowest_latency",
    "vllm_load",
];

pub fn supported_capabilities() -> Vec<String> {
    let mut caps: Vec<_> = SUPPORTED_CAPABILITIES
        .iter()
        .map(|c| c.to_string())
        .collect();
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

pub fn strategy_params(r: &pb::ModelRoute) -> Result<serde_json::Value, ApplyError> {
    if r.strategy_params_json.len() > 16 * 1024 {
        return Err(ApplyError::invalid("strategy params exceed 16 KiB"));
    }
    if r.strategy_params_json.is_empty() {
        return Ok(serde_json::json!({}));
    }
    let params: serde_json::Value = serde_json::from_str(&r.strategy_params_json)
        .map_err(|_| ApplyError::invalid("strategy params must be a JSON object"))?;
    if !params.is_object() {
        return Err(ApplyError::invalid("strategy params must be a JSON object"));
    }
    Ok(params)
}

pub fn strategy_headers(r: &pb::ModelRoute) -> Result<Vec<String>, ApplyError> {
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
            hyper::header::HeaderName::from_bytes(name.as_bytes())
                .map(|name| name.as_str().to_owned())
                .map_err(|_| ApplyError::invalid("params.headers contains an invalid header name"))
        })
        .collect()
}
/// Upper bounds that keep split labels and per-request work bounded.
pub const MAX_SPLITS: usize = 16;
pub const MAX_SPLIT_NAME: usize = 32;
pub const MAX_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_KEYS: usize = 200_000;
pub const FILE_NAME: &str = "snapshot.pb";
const TMP_NAME: &str = "snapshot.pb.tmp";
const DIGEST_LEN: usize = 32;
const LEN_FIELD: usize = 4;
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
        if let Some(h) = &u.health_check {
            if h.interval_ms < 1000 || h.timeout_ms == 0 || h.timeout_ms >= h.interval_ms {
                return Err(ApplyError::invalid(
                    "health interval must be >= 1000 ms and 0 < timeout < interval",
                ));
            }
            if !h.path.is_empty() && (!h.path.starts_with('/') || h.path.contains(['?', '#'])) {
                return Err(ApplyError::invalid(
                    "health path must be an absolute path without query or fragment",
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
    let deployments = unique(
        snapshot.deployments.iter().map(|d| d.name.as_str()),
        "deployment",
    )?;
    for d in &snapshot.deployments {
        if !upstreams.contains(d.upstream.as_str()) {
            return Err(ApplyError::invalid(format!(
                "deployment {:?} references unknown upstream {:?}",
                d.name, d.upstream
            )));
        }
        if d.upstream_model.is_empty() {
            return Err(ApplyError::invalid(format!(
                "deployment {:?} has an empty upstream_model",
                d.name
            )));
        }
    }
    let routes = unique(snapshot.routes.iter().map(|r| r.model.as_str()), "route")?;
    for r in &snapshot.routes {
        require(r.retry.is_some(), "retry")?;
        require(!r.fallbacks.is_empty(), "fallbacks")?;
        require(
            !r.traffic_split.is_empty() || r.sticky != 0 || !r.sticky_header.is_empty(),
            "traffic_split",
        )?;
        let sticky = known::<pb::StickyBy>(r.sticky, true, "sticky")?;
        validate_split(r, sticky, &deployments)?;
        if let Some(p) = &r.retry {
            if p.max_attempts > 5 || p.retry_on_status.iter().any(|s| !(400..=599).contains(s)) {
                return Err(ApplyError::invalid(
                    "retry max_attempts must be <= 5 and statuses in 400..=599",
                ));
            }
        }
        for fallback in &r.fallbacks {
            if fallback == &r.model || !routes.contains(fallback.as_str()) {
                return Err(ApplyError::invalid(format!(
                    "route {:?} has a self or unknown fallback {:?}",
                    r.model, fallback
                )));
            }
        }
        if r.model == "unknown" {
            return Err(ApplyError::invalid(
                "route alias 'unknown' is reserved for metrics",
            ));
        }
        let strategy = known::<pb::RoutingStrategy>(
            r.strategy,
            false,
            &format!("route {:?} strategy", r.model),
        )?;
        require(
            strategy == pb::RoutingStrategy::LowestLatency,
            "lowest_latency",
        )?;
        require(strategy == pb::RoutingStrategy::VllmLoad, "vllm_load")?;
        if strategy == pb::RoutingStrategy::Custom {
            if !valid_strategy_name(&r.custom_strategy) {
                return Err(ApplyError::invalid("invalid custom strategy name"));
            }
            require(true, &format!("strategy:{}", r.custom_strategy))?;
            if lumewarp_router_api::lookup(&r.custom_strategy)
                .map_err(|_| ApplyError::invalid("duplicate strategy registration"))?
                .is_none()
            {
                return Err(ApplyError::unsupported("unregistered custom strategy"));
            }
            strategy_headers(r)?;
        } else if !r.custom_strategy.is_empty() || !r.strategy_params_json.is_empty() {
            return Err(ApplyError::invalid("custom strategy fields require CUSTOM"));
        }
        // With traffic_split the route's own deployment list is ignored.
        if !r.traffic_split.is_empty() {
            continue;
        }
        if r.deployments.is_empty() {
            return Err(ApplyError::invalid(format!(
                "route {:?} has no deployments",
                r.model
            )));
        }
        if let Some(d) = r
            .deployments
            .iter()
            .find(|d| !deployments.contains(d.as_str()))
        {
            return Err(ApplyError::invalid(format!(
                "route {:?} references unknown deployment {:?}",
                r.model, d
            )));
        }
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

/// Split names are bounded metric labels; every split names known deployments
/// and the weights must leave something to route to.
fn validate_split(
    r: &pb::ModelRoute,
    sticky: pb::StickyBy,
    deployments: &HashSet<&str>,
) -> Result<(), ApplyError> {
    let model = &r.model;
    let fail = |what: String| Err(ApplyError::invalid(format!("route {model:?}: {what}")));
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
        if split.deployments.is_empty() {
            return fail(format!("split {name:?} has no deployments"));
        }
        let mut seen = HashSet::new();
        for d in &split.deployments {
            if !deployments.contains(d.as_str()) {
                return fail(format!(
                    "split {name:?} references unknown deployment {d:?}"
                ));
            }
            if !seen.insert(d.as_str()) {
                return fail(format!("split {name:?} repeats deployment {d:?}"));
            }
        }
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
                &mut next.deployments,
                delta.upsert_deployments,
                &delta.remove_deployments,
                |d| &d.name,
                "deployment",
            )?;
            merge(
                &mut next.routes,
                delta.upsert_routes,
                &delta.remove_routes,
                |r| &r.model,
                "route",
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

/// Atomically replaces `<dir>/snapshot.pb`: `payload || digest || len(digest)
/// as u32 BE || sha256(everything before)`, written to a 0600 temp file,
/// fsynced, renamed, then the directory fsynced. `digest` is the control
/// plane's opaque `target_digest`, kept in the same file so the two can never
/// diverge. Creates `dir` (0700) if missing. Blocking: call from the apply
/// task only.
pub fn store_with_digest(dir: &Path, snapshot: &Snapshot, digest: &[u8]) -> io::Result<()> {
    let mut data = snapshot.encode_to_vec();
    if data.len() > MAX_SNAPSHOT_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "snapshot exceeds the size cap",
        ));
    }
    let digest_len = u32::try_from(digest.len())
        .ok()
        .filter(|&n| n <= MAX_DIGEST_BYTES)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "digest too large"))?;
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

/// Reads `<dir>/snapshot.pb`. `Ok(None)` when absent; corrupt, oversized or
/// wrong-scope files are errors, and the caller must not serve from them.
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
    let cap = MAX_SNAPSHOT_BYTES + MAX_DIGEST_BYTES as usize + DIGEST_LEN + LEN_FIELD;
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

    pub fn upstream(name: &str) -> pb::Upstream {
        pb::Upstream {
            name: name.into(),
            kind: pb::UpstreamKind::Vllm as i32,
            base_url: "http://localhost:8000/v1".into(),
            timeout_ms: 1000,
            ..Default::default()
        }
    }

    pub fn deployment(name: &str, upstream: &str) -> pb::Deployment {
        pb::Deployment {
            name: name.into(),
            upstream: upstream.into(),
            upstream_model: "actual".into(),
            weight: 1,
            price: None,
        }
    }

    pub fn route(model: &str, deployments: &[&str]) -> pb::ModelRoute {
        pb::ModelRoute {
            model: model.into(),
            deployments: deployments.iter().map(|d| d.to_string()).collect(),
            strategy: pb::RoutingStrategy::Weighted as i32,
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
        }
    }

    pub fn base() -> Snapshot {
        Snapshot {
            version: 1,
            upstreams: vec![upstream("u"), upstream("v")],
            deployments: vec![deployment("a", "u"), deployment("b", "v")],
            routes: vec![route("m1", &["a"]), route("m2", &["a", "b"])],
            api_keys: vec![key("k1", "s1", "t1", &["m1"]), key("k2", "s2", "", &[])],
            teams: vec![team("t1"), team("t2")],
            created_at_unix_ms: 10,
            required_capabilities: vec![],
            scope: "scope-a".into(),
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
            match *cap {
                "retry" => s.routes[0].retry = Some(pb::RetryPolicy::default()),
                "fallbacks" => s.routes[0].fallbacks = vec!["m2".into()],
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
                "traffic_split" => s.routes[0].traffic_split = vec![split("stable", &["a"], 1)],
                "lowest_latency" => {
                    s.routes[0].strategy = pb::RoutingStrategy::LowestLatency as i32
                }
                "vllm_load" => s.routes[0].strategy = pb::RoutingStrategy::VllmLoad as i32,
                _ => unreachable!(),
            }
            assert_code(
                validate(&s, SUPPORTED_CAPABILITIES),
                NackCode::InvalidConfig,
            );
            s.required_capabilities.push((*cap).into());
            validate(&s, SUPPORTED_CAPABILITIES).unwrap();
        }
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
            s.routes[0].retry = Some(pb::RetryPolicy {
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
        for fallback in ["m1", "missing"] {
            let mut s = base();
            s.required_capabilities.push("fallbacks".into());
            s.routes[0].fallbacks.push(fallback.into());
            assert_code(
                validate(&s, SUPPORTED_CAPABILITIES),
                NackCode::InvalidConfig,
            );
        }
    }

    pub fn split(name: &str, deployments: &[&str], weight: u32) -> pb::TrafficSplit {
        pb::TrafficSplit {
            name: name.into(),
            deployments: deployments.iter().map(|d| d.to_string()).collect(),
            weight,
        }
    }

    #[test]
    fn traffic_split_and_sticky_validation() {
        let check = |mutate: &dyn Fn(&mut pb::ModelRoute)| {
            let mut s = base();
            s.required_capabilities.push("traffic_split".into());
            s.routes[1].traffic_split =
                vec![split("stable", &["a"], 9), split("canary", &["b"], 1)];
            mutate(&mut s.routes[1]);
            validate(&s, SUPPORTED_CAPABILITIES)
        };
        check(&|_| {}).unwrap();
        // The route's own list is ignored (may be empty) with splits.
        check(&|r| r.deployments.clear()).unwrap();
        check(&|r| r.traffic_split[1].weight = 0).unwrap();
        check(&|r| {
            r.sticky = pb::StickyBy::Header as i32;
            r.sticky_header = "X-Session".into();
        })
        .unwrap();
        check(&|r| r.sticky = pb::StickyBy::Team as i32).unwrap();
        check(&|r| r.sticky = pb::StickyBy::Key as i32).unwrap();
        type Mutation = Box<dyn Fn(&mut pb::ModelRoute)>;
        let bad: Vec<Mutation> = vec![
            Box::new(|r| {
                r.traffic_split[0].weight = 0;
                r.traffic_split[1].weight = 0;
            }),
            Box::new(|r| r.traffic_split[1].name = "stable".into()),
            Box::new(|r| r.traffic_split[1].name.clear()),
            Box::new(|r| r.traffic_split[1].name = "x".repeat(MAX_SPLIT_NAME + 1)),
            Box::new(|r| r.traffic_split[1].name = "can ary".into()),
            Box::new(|r| r.traffic_split[1].deployments.clear()),
            Box::new(|r| r.traffic_split[1].deployments = vec!["nope".into()]),
            Box::new(|r| r.traffic_split[1].deployments = vec!["b".into(), "b".into()]),
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
        s.routes[0].traffic_split = vec![split("stable", &["a"], 1)];
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
        let mut a = deployment("a", "w");
        a.weight = 7;
        d.upsert_deployments = vec![a, deployment("c", "u")];
        d.remove_deployments = vec!["b".into()];
        d.upsert_routes = vec![route("m2", &["c"]), route("m3", &["a"])];
        d.remove_routes = vec!["m1".into()];
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
            next.deployments
                .iter()
                .map(|d| d.name.clone())
                .collect::<Vec<_>>(),
            names(vec!["a", "c"])
        );
        assert_eq!(next.deployments[0].weight, 7);
        assert_eq!(
            next.routes
                .iter()
                .map(|r| r.model.clone())
                .collect::<Vec<_>>(),
            names(vec!["m2", "m3"])
        );
        assert_eq!(next.routes[0].deployments, ["c"]);
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
        validate(&next, &["x"]).unwrap();
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
        // Removing an upstream still used by a deployment, likewise.
        let mut d = delta(2);
        d.remove_upstreams = vec!["u".into()];
        let next = apply_delta(d).unwrap();
        assert_code(
            validate(&next, SUPPORTED_CAPABILITIES),
            NackCode::InvalidConfig,
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
        s.required_capabilities = vec!["jwt".into()];
        assert_code(validate(&s, &[]), NackCode::UnsupportedFeature);
        validate(&s, &["jwt"]).unwrap();
        let mut s = base();
        s.routes[0].strategy = pb::RoutingStrategy::LowestLatency as i32;
        assert_code(validate(&s, &[]), NackCode::InvalidConfig);
        s.required_capabilities = vec!["lowest_latency".into()];
        validate(&s, &["lowest_latency"]).unwrap();
        assert_code(validate(&s, &[]), NackCode::UnsupportedFeature);
    }

    #[test]
    fn invalid_references() {
        invalid(|s| s.deployments[0].upstream = "nope".into());
        invalid(|s| s.routes[0].deployments = vec!["nope".into()]);
        invalid(|s| s.routes[0].deployments.clear());
        invalid(|s| s.api_keys[0].team_id = "nope".into());
        invalid(|s| s.routes[0].model = "unknown".into());
        invalid(|s| s.deployments[0].upstream_model.clear());
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
        validate(&s, &[]).unwrap();
    }

    #[test]
    fn duplicates_and_unspecified_enums() {
        invalid(|s| s.upstreams[1].name = "u".into());
        invalid(|s| s.deployments[1].name = "a".into());
        invalid(|s| s.routes[1].model = "m1".into());
        invalid(|s| s.teams[1].id = "t1".into());
        invalid(|s| s.teams[1].id.clear());
        invalid(|s| s.upstreams[0].kind = 0);
        invalid(|s| s.upstreams[0].kind = 99);
        invalid(|s| s.upstreams[0].stream_usage_mode = 99);
        invalid(|s| s.routes[0].strategy = 0);
        invalid(|s| s.routes[0].strategy = 99);
        invalid(|s| s.api_keys[0].status = 0);
        invalid(|s| s.api_keys[0].status = 99);
        let mut s = base();
        s.upstreams[0].stream_usage_mode = 0;
        validate(&s, &[]).unwrap();
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
        let mut garbage = vec![0xff; 40];
        let digest = Sha256::digest(&garbage);
        garbage.extend_from_slice(&digest);
        fs::write(&path, &garbage).unwrap();
        assert!(matches!(load(dir, "scope-a"), Err(LoadError::Corrupt)));
    }

    #[test]
    fn load_rejects_oversized_file() {
        let root = temp_dir();
        let path = root.path().join(FILE_NAME);
        let file = File::create(&path).unwrap();
        file.set_len(
            (MAX_SNAPSHOT_BYTES + MAX_DIGEST_BYTES as usize + DIGEST_LEN + LEN_FIELD + 1) as u64,
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

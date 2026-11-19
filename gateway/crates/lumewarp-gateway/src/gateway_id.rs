//! Identity of this gateway process.
//!
//! A gateway does not name itself: the control plane assigns an id in
//! `Welcome`, the first message of every config stream, and every stream (a
//! reconnect included) gets a new one. [`Identity`] is shared by the stream,
//! which stores the id, and the usage reporter, which reads it when it sends a
//! batch. Usage sent before the first Welcome carries an empty id and is
//! attributed to no gateway.
//!
//! Redis shared limits cannot wait for a handshake, so they count under a
//! separate process-local instance key: random, fixed at startup and never
//! shown.

use std::sync::{Arc, OnceLock};

use arc_swap::ArcSwapOption;
use uuid::Uuid;

/// The longest host part of the instance key; the same bound the control
/// plane applies to the hostname prefix of an assigned id.
const MAX_HOST_CHARS: usize = 60;
const SUFFIX_HEX_CHARS: usize = 8;

/// The id the control plane assigned to the current config stream.
#[derive(Clone)]
pub struct Identity {
    assigned: Arc<ArcSwapOption<String>>,
}

impl Identity {
    pub fn new() -> Self {
        Self {
            assigned: Arc::new(ArcSwapOption::empty()),
        }
    }

    /// The assigned id, or `None` before the first Welcome.
    pub fn assigned(&self) -> Option<Arc<String>> {
        self.assigned.load_full()
    }

    /// The id to put on a usage report: the current one, or empty.
    pub fn assigned_or_empty(&self) -> String {
        self.assigned()
            .map(|id| id.as_str().to_owned())
            .unwrap_or_default()
    }

    /// Stores the id from `Welcome`. Cheap: one atomic swap.
    pub fn assign(&self, id: impl Into<String>) {
        self.assigned.store(Some(Arc::new(id.into())));
    }
}

impl Default for Identity {
    fn default() -> Self {
        Self::new()
    }
}

/// `HOSTNAME` exactly as the environment has it; the control plane sanitizes
/// it into the prefix of the id it assigns (empty becomes `local`).
pub fn hostname() -> String {
    std::env::var("HOSTNAME").unwrap_or_default()
}

static INSTANCE_KEY: OnceLock<String> = OnceLock::new();

/// The process-local random key shared limits count under. It is never sent to
/// the control plane nor logged; it only has to differ between processes that
/// share one Redis.
pub fn instance_key() -> &'static str {
    INSTANCE_KEY.get_or_init(|| format!("{}-{}", host_part(&hostname()), suffix()))
}

/// The hostname reduced to `[A-Za-z0-9._-]`, at most 60 characters, or
/// `local` when nothing usable is left. Keeps the Redis key free of the
/// separators (`:`, `{}`) the key format uses.
fn host_part(host: &str) -> String {
    let cleaned: String = host
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        .take(MAX_HOST_CHARS)
        .collect();
    if cleaned.is_empty() {
        "local".to_owned()
    } else {
        cleaned
    }
}

/// 8 lowercase hex characters of random bits: the *last* characters of a UUIDv7, which are
/// `rand_b` (the first ones are its millisecond timestamp). Two processes on one host, or
/// two processes that start within the same millisecond, therefore never share an
/// instance key.
fn suffix() -> String {
    let uuid = Uuid::now_v7().simple().to_string();
    uuid[uuid.len() - SUFFIX_HEX_CHARS..].to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unassigned_identity_reports_an_empty_id() {
        let identity = Identity::new();
        assert!(identity.assigned().is_none());
        assert_eq!(identity.assigned_or_empty(), "");
    }

    #[test]
    fn the_assigned_id_is_shared_and_replaceable() {
        let identity = Identity::new();
        let observer = identity.clone();
        identity.assign("gw-1-0123abcd");
        assert_eq!(observer.assigned_or_empty(), "gw-1-0123abcd");
        identity.assign("gw-2-89abcdef");
        assert_eq!(observer.assigned_or_empty(), "gw-2-89abcdef");
    }

    #[test]
    fn a_plain_hostname_is_kept() {
        assert_eq!(host_part("gw-1"), "gw-1");
        assert_eq!(host_part("gw_1.example.com"), "gw_1.example.com");
    }

    #[test]
    fn disallowed_characters_are_dropped() {
        assert_eq!(host_part("gw/1 pod:2"), "gw1pod2");
        assert_eq!(host_part("gw\u{00e9}1"), "gw1");
        assert_eq!(host_part("gw-1\n"), "gw-1");
        assert_eq!(host_part("gw{1}"), "gw1");
    }

    #[test]
    fn an_unusable_hostname_becomes_local() {
        assert_eq!(host_part(""), "local");
        assert_eq!(host_part("///"), "local");
    }

    #[test]
    fn the_host_part_is_cut_to_sixty_characters() {
        let long = "h".repeat(100);
        assert_eq!(host_part(&long).len(), MAX_HOST_CHARS);
        assert_eq!(host_part(&long), "h".repeat(MAX_HOST_CHARS));
    }

    #[test]
    fn the_suffix_is_eight_lowercase_hex_characters() {
        let suffix = suffix();
        assert_eq!(suffix.len(), SUFFIX_HEX_CHARS);
        assert!(suffix
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
    }

    #[test]
    fn two_suffixes_differ() {
        // Random bits, not a timestamp: two processes that start together must not collide.
        let first = suffix();
        assert_ne!(first, suffix());
    }

    #[test]
    fn the_instance_key_is_fixed_per_process_and_never_a_hostname_only() {
        let key = instance_key();
        assert_eq!(key, instance_key(), "the key must be fixed at startup");
        assert!(key.len() <= MAX_HOST_CHARS + 1 + SUFFIX_HEX_CHARS);
        assert!(
            key.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')),
            "{key}"
        );
        assert_eq!(key.rfind('-'), Some(key.len() - SUFFIX_HEX_CHARS - 1));
    }
}

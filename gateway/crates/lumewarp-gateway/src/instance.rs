//! Identity of this gateway process.
//!
//! One gateway id may run as several processes behind a Service, all sharing
//! its token; each is an *instance* and keeps its own config stream, version
//! and status (docs/config-protocol.md, "Gateway instances"). The id is fixed
//! at startup: a restarted process is a new instance, so whatever it left in
//! the control plane (rows, counters) is never mistaken for this one.
//!
//! Format `<host>-<8 lowercase hex>`: the host part makes replicas tellable
//! apart in a pod list, the random suffix keeps two processes on one host (or
//! with no hostname) distinct.

use std::sync::OnceLock;

use uuid::Uuid;

/// The control plane's bound on `Hello.instance_id`, and the longest host part.
const MAX_HOST_CHARS: usize = 60;
const SUFFIX_HEX_CHARS: usize = 8;

static INSTANCE_ID: OnceLock<String> = OnceLock::new();

/// This process's instance id; computed once, on first use.
pub fn instance_id() -> &'static str {
    INSTANCE_ID.get_or_init(|| {
        let host = std::env::var("HOSTNAME").unwrap_or_default();
        format!("{}-{}", host_part(&host), suffix())
    })
}

/// The hostname reduced to `[A-Za-z0-9._-]`, at most 60 characters, or
/// `local` when nothing usable is left.
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
/// two processes that start within the same millisecond, therefore never share an instance
/// id; with the leading hex they would, and two instances with the same id replace each
/// other's stream forever.
fn suffix() -> String {
    let uuid = Uuid::now_v7().simple().to_string();
    uuid[uuid.len() - SUFFIX_HEX_CHARS..].to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn the_instance_id_fits_the_control_plane_rule_and_never_changes() {
        let id = instance_id();
        assert_eq!(id, instance_id());
        assert_eq!(id, instance_id(), "the id must be fixed at startup");
        assert!(id.len() <= MAX_HOST_CHARS + 1 + SUFFIX_HEX_CHARS);
        assert!(
            id.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')),
            "{id}"
        );
        assert_eq!(id.rfind('-'), Some(id.len() - SUFFIX_HEX_CHARS - 1));
    }
}

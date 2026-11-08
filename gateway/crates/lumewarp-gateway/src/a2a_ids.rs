//! Opaque gateway ids for A2A tasks and contexts: `lwt.<owner>.<orig>.<mac>`.
//!
//! A2A clients send task and context ids back to the gateway on `tasks/get`,
//! `tasks/cancel`, `message/send` (continuing a task) and inside every SSE
//! event. The gateway has no state of its own, so an id it hands out must carry
//! its provenance: which API key it was minted for and which upstream id it
//! stands for, authenticated so a client cannot read or reuse another key's ids.
//!
//! - `owner` is the key id the id was minted for.
//! - `orig` is the upstream id, base64url without padding.
//! - `mac` is the first 16 bytes of
//!   `HMAC-SHA256(key, DOMAIN_A2A_ID || 0x00 || agent || 0x00 || owner || 0x00 || orig)`,
//!   base64url without padding, over the encoded `orig` text.
//!
//! Domain separation: the MAC message starts with [`DOMAIN_A2A_ID`], and
//! [`DOMAIN_A2A_ID`] is also excluded as an HKDF info string, so a tag made by
//! this module can never be accepted by another consumer of the same key (for
//! example a future MCP session id sealer). The key comes from
//! [`ENV`] (`LUMEWARP_GATEWAY_ID_KEY`, the same value on every gateway, never in
//! a snapshot) and must decode to at least 32 bytes. Code that needs its own
//! key material derives a subkey with [`IdKey::derive_hmac_key`] instead of
//! using [`IdKey::secret`] directly.

// The MCP/A2A request path (W2) wires these modules; until then most of the
// API has no caller in the binary, which `-D warnings` would reject.
#![allow(dead_code)]
use base64::engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE_NO_PAD};
use base64::Engine as _;
use ring::{hkdf, hmac};

/// Environment variable holding the base64-encoded gateway id key.
pub const ENV: &str = "LUMEWARP_GATEWAY_ID_KEY";

/// Prefix of every wrapped id.
pub const PREFIX: &str = "lwt.";

/// Domain separation label for A2A task and context ids.
pub const DOMAIN_A2A_ID: &[u8] = b"lumewarp/a2a-id/v1";

/// Shortest accepted key after base64 decoding.
pub const MIN_KEY_BYTES: usize = 32;

/// Truncated tag length: 16 bytes (128 bits) is plenty for an id that is also
/// bound to agent and owner.
pub const MAC_BYTES: usize = 16;

/// Longest upstream id the gateway wraps.
pub const MAX_ID_BYTES: usize = 4096;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum KeyError {
    #[error("{ENV} is not valid base64")]
    Base64,
    #[error("{ENV} decodes to fewer than {MIN_KEY_BYTES} bytes")]
    TooShort,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum UnwrapError {
    /// Missing prefix, undecodable fields or a bad MAC: not minted by the
    /// gateway with this key, or tampered with.
    #[error("not a gateway id")]
    NotOurs,
    /// A genuine gateway id, minted for a different API key.
    #[error("id belongs to another key")]
    OtherOwner,
}

/// The process-wide gateway id key.
///
/// `Debug` is derived on purpose: the type only holds key material, and it must
/// never be logged, so there is nothing useful (or safe) to print.
pub struct IdKey {
    secret: Box<[u8]>,
    /// Derived once: HMAC-SHA256 keyed with the raw secret.
    a2a: hmac::Key,
}

impl std::fmt::Debug for IdKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("IdKey(<redacted>)")
    }
}

impl IdKey {
    /// A key from raw bytes (at least [`MIN_KEY_BYTES`]).
    pub fn from_secret(secret: impl Into<Vec<u8>>) -> Result<Self, KeyError> {
        let secret = secret.into();
        if secret.len() < MIN_KEY_BYTES {
            return Err(KeyError::TooShort);
        }
        let a2a = hmac::Key::new(hmac::HMAC_SHA256, &secret);
        Ok(Self {
            secret: secret.into_boxed_slice(),
            a2a,
        })
    }

    /// A key from the base64 text of [`ENV`]: standard alphabet, padding
    /// optional.
    pub fn from_base64(text: &str) -> Result<Self, KeyError> {
        let text = text.trim();
        let decoded = STANDARD
            .decode(text)
            .or_else(|_| STANDARD_NO_PAD.decode(text.trim_end_matches('=')))
            .map_err(|_| KeyError::Base64)?;
        Self::from_secret(decoded)
    }

    /// The key from the environment: `Ok(None)` when [`ENV`] is unset or empty.
    /// `Err` means it is set but unusable, which the caller must treat as a
    /// misconfiguration (A2A answers 503; LLM and MCP traffic are unaffected).
    pub fn from_env() -> Result<Option<Self>, KeyError> {
        match std::env::var(ENV) {
            Ok(text) if !text.trim().is_empty() => Self::from_base64(&text).map(Some),
            Ok(_) | Err(std::env::VarError::NotPresent) => Ok(None),
            // Do not retain VarError::NotUnicode: its Display quotes the value.
            Err(std::env::VarError::NotUnicode(_)) => Err(KeyError::Base64),
        }
    }

    /// The raw key bytes. Exposed so another module (an MCP session id sealer,
    /// for example) can derive its own subkey; prefer [`Self::derive_hmac_key`],
    /// which keeps the domains apart.
    pub fn secret(&self) -> &[u8] {
        &self.secret
    }

    /// An HMAC-SHA256 key for another purpose, derived with HKDF-SHA256
    /// (salt = none, `info` = the caller's domain label). The `info` string must
    /// not be [`DOMAIN_A2A_ID`]; that domain belongs to this module.
    pub fn derive_hmac_key(&self, info: &[u8]) -> hmac::Key {
        assert_ne!(info, DOMAIN_A2A_ID, "the A2A id domain belongs to a2a_ids");
        let mut subkey = [0u8; 32];
        hkdf::Salt::new(hkdf::HKDF_SHA256, &[])
            .extract(&self.secret)
            .expand(&[info], hmac::HMAC_SHA256)
            .and_then(|okm| okm.fill(&mut subkey))
            .expect("32-byte HKDF output");
        hmac::Key::new(hmac::HMAC_SHA256, &subkey)
    }

    /// `lwt.<owner>.<orig>.<mac>` for the upstream id `id`.
    ///
    /// `None` when the id cannot be represented: an empty or oversized id, or
    /// an owner that is empty or contains `.` or NUL (key ids are UUIDs, so this
    /// only fires on a programming error).
    pub fn wrap(&self, agent: &str, owner: &str, id: &str) -> Option<String> {
        if id.is_empty() || id.len() > MAX_ID_BYTES || !valid_owner(owner) {
            return None;
        }
        let orig = URL_SAFE_NO_PAD.encode(id.as_bytes());
        let mac = self.mac(agent, owner, &orig);
        let mut wrapped = String::with_capacity(PREFIX.len() + owner.len() + orig.len() + 32);
        wrapped.push_str(PREFIX);
        wrapped.push_str(owner);
        wrapped.push('.');
        wrapped.push_str(&orig);
        wrapped.push('.');
        wrapped.push_str(&URL_SAFE_NO_PAD.encode(mac));
        Some(wrapped)
    }

    /// The upstream id inside `wrapped`, when it was minted for `agent` and
    /// `owner`.
    ///
    /// A failed MAC means the id was not minted with this key (or for this
    /// agent): [`UnwrapError::NotOurs`]. A valid MAC under a different owner is
    /// another key's id: [`UnwrapError::OtherOwner`]. The comparison is
    /// constant time.
    pub fn unwrap(&self, agent: &str, owner: &str, wrapped: &str) -> Result<String, UnwrapError> {
        let rest = wrapped.strip_prefix(PREFIX).ok_or(UnwrapError::NotOurs)?;
        let mut parts = rest.split('.');
        let (Some(claimed), Some(orig), Some(tag), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(UnwrapError::NotOurs);
        };
        if !valid_owner(claimed) || orig.is_empty() {
            return Err(UnwrapError::NotOurs);
        }
        let tag = URL_SAFE_NO_PAD
            .decode(tag)
            .map_err(|_| UnwrapError::NotOurs)?;
        if tag.len() != MAC_BYTES {
            return Err(UnwrapError::NotOurs);
        }
        let id = URL_SAFE_NO_PAD
            .decode(orig)
            .map_err(|_| UnwrapError::NotOurs)?;
        if id.is_empty() || id.len() > MAX_ID_BYTES {
            return Err(UnwrapError::NotOurs);
        }
        let id = String::from_utf8(id).map_err(|_| UnwrapError::NotOurs)?;
        // `hmac::verify` rejects a truncated tag (`verify_slices_are_equal`
        // needs equal lengths), so compare the prefix here, without branching
        // on the content.
        let expected = hmac::sign(&self.a2a, &mac_message(agent, claimed, orig));
        if !constant_time_eq(&expected.as_ref()[..MAC_BYTES], &tag) {
            return Err(UnwrapError::NotOurs);
        }
        if claimed != owner {
            return Err(UnwrapError::OtherOwner);
        }
        Ok(id)
    }

    fn mac(&self, agent: &str, owner: &str, orig: &str) -> [u8; MAC_BYTES] {
        let tag = hmac::sign(&self.a2a, &mac_message(agent, owner, orig));
        let mut mac = [0u8; MAC_BYTES];
        mac.copy_from_slice(&tag.as_ref()[..MAC_BYTES]);
        mac
    }
}

/// Whether `id` looks like an id minted by [`IdKey::wrap`]. Cheap check for
/// paths that must not parse untrusted ids.
pub fn is_wrapped(id: &str) -> bool {
    id.starts_with(PREFIX)
}

fn valid_owner(owner: &str) -> bool {
    !owner.is_empty() && !owner.contains('.') && !owner.contains('\0')
}

/// Constant-time equality for equal-length slices: no early exit, no
/// data-dependent branch, so a caller cannot learn how many bytes matched.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut difference = 0u8;
    for (x, y) in a.iter().zip(b) {
        difference |= x ^ y;
    }
    difference == 0
}

fn mac_message(agent: &str, owner: &str, orig: &str) -> Vec<u8> {
    let mut message =
        Vec::with_capacity(DOMAIN_A2A_ID.len() + agent.len() + owner.len() + orig.len() + 3);
    message.extend_from_slice(DOMAIN_A2A_ID);
    for field in [agent, owner, orig] {
        message.push(0);
        message.extend_from_slice(field.as_bytes());
    }
    message
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> IdKey {
        // 32 bytes, base64: "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
        IdKey::from_base64("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=").unwrap()
    }

    const AGENT: &str = "summarizer";
    const OWNER: &str = "6f1c0b3e-2a58-4f77-9a4a-1f2b8c9d0e11";

    #[test]
    fn round_trip() {
        let key = key();
        let wrapped = key.wrap(AGENT, OWNER, "task-123").unwrap();
        assert!(wrapped.starts_with("lwt."));
        assert_eq!(wrapped.split('.').count(), 4);
        assert!(is_wrapped(&wrapped));
        assert_eq!(key.unwrap(AGENT, OWNER, &wrapped).unwrap(), "task-123");
    }

    #[test]
    fn format_is_exactly_owner_orig_mac() {
        let key = key();
        let wrapped = key.wrap(AGENT, OWNER, "abc").unwrap();
        let mut parts = wrapped.split('.');
        assert_eq!(parts.next(), Some("lwt"));
        assert_eq!(parts.next(), Some(OWNER));
        // base64url no padding of "abc".
        assert_eq!(parts.next(), Some("YWJj"));
        let mac = parts.next().unwrap();
        assert_eq!(mac.len(), 22, "16 bytes base64url without padding");
        assert!(mac
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
        assert_eq!(parts.next(), None);
    }

    #[test]
    fn unicode_and_punctuated_upstream_ids_survive() {
        let key = key();
        for id in [
            "",
            " ",
            "任務-1/2",
            "a.b.c",
            "%%%",
            &"x".repeat(MAX_ID_BYTES),
        ] {
            let wrapped = key.wrap(AGENT, OWNER, id);
            if id.is_empty() || id.len() > MAX_ID_BYTES {
                assert!(wrapped.is_none());
                continue;
            }
            assert_eq!(key.unwrap(AGENT, OWNER, &wrapped.unwrap()).unwrap(), id);
        }
    }

    #[test]
    fn tampered_mac_and_malformed_ids_are_not_ours() {
        let key = key();
        let wrapped = key.wrap(AGENT, OWNER, "task-1").unwrap();
        // Flip one character of the tag.
        let mut tampered = wrapped.clone();
        let last = tampered.pop().unwrap();
        tampered.push(if last == 'A' { 'B' } else { 'A' });
        assert_eq!(
            key.unwrap(AGENT, OWNER, &tampered),
            Err(UnwrapError::NotOurs)
        );
        // Flip one character of the encoded upstream id.
        let mut tampered = wrapped.clone();
        let at = wrapped.rfind('.').unwrap() + 2;
        let current = tampered.as_bytes()[at];
        tampered.replace_range(at..at + 1, if current == b'X' { "Y" } else { "X" });
        assert_eq!(
            key.unwrap(AGENT, OWNER, &tampered),
            Err(UnwrapError::NotOurs)
        );
        for bad in [
            "",
            "task-1",
            "lwt",
            "lwt.",
            "lwt.owner.only",
            "lwt.owner.a.b.c",
            "lwt..YWJj.AAAAAAAAAAAAAAAAAAAAAA",
            "lwt.ow.ner.YWJj.AAAAAAAAAAAAAAAAAAAAAA",
            "lwt.owner..AAAAAAAAAAAAAAAAAAAAAA",
            "lwt.owner.YWJj.",
            "lwt.owner.YWJj.not_base64!!",
            "lwt.owner.YWJj.AAAAAAAAAAAAAAAAAAAAAA",
        ] {
            assert_eq!(
                key.unwrap(AGENT, OWNER, bad),
                Err(UnwrapError::NotOurs),
                "{bad:?}"
            );
        }
        // Non-UTF-8 upstream bytes.
        let orig = URL_SAFE_NO_PAD.encode([0xff, 0xfe]);
        assert_eq!(
            key.unwrap(
                AGENT,
                OWNER,
                &format!("lwt.{OWNER}.{orig}.AAAAAAAAAAAAAAAAAAAAAA")
            ),
            Err(UnwrapError::NotOurs)
        );
    }

    #[test]
    fn another_agents_id_is_not_ours() {
        let key = key();
        let wrapped = key.wrap(AGENT, OWNER, "task-1").unwrap();
        assert_eq!(
            key.unwrap("other-agent", OWNER, &wrapped),
            Err(UnwrapError::NotOurs)
        );
    }

    #[test]
    fn another_owners_id_is_reported_separately() {
        let key = key();
        let other = "0b7d2e4f-1111-4222-8333-444455556666";
        let wrapped = key.wrap(AGENT, other, "task-1").unwrap();
        assert_eq!(
            key.unwrap(AGENT, OWNER, &wrapped),
            Err(UnwrapError::OtherOwner)
        );
        // Same owner still works.
        assert_eq!(key.unwrap(AGENT, other, &wrapped).unwrap(), "task-1");
    }

    #[test]
    fn a_different_key_cannot_unwrap() {
        let wrapped = key().wrap(AGENT, OWNER, "task-1").unwrap();
        let other = IdKey::from_secret(vec![7u8; 32]).unwrap();
        assert_eq!(
            other.unwrap(AGENT, OWNER, &wrapped),
            Err(UnwrapError::NotOurs)
        );
    }

    #[test]
    fn wrap_rejects_ids_it_cannot_represent() {
        let key = key();
        assert!(key.wrap(AGENT, "", "task-1").is_none());
        assert!(key.wrap(AGENT, "own.er", "task-1").is_none());
        assert!(key.wrap(AGENT, "own\0er", "task-1").is_none());
        assert!(key.wrap(AGENT, OWNER, "").is_none());
        assert!(key
            .wrap(AGENT, OWNER, &"x".repeat(MAX_ID_BYTES + 1))
            .is_none());
    }

    #[test]
    fn key_loading_rules() {
        assert_eq!(
            IdKey::from_secret(vec![0u8; 31]).unwrap_err(),
            KeyError::TooShort
        );
        assert!(IdKey::from_secret(vec![0u8; 32]).is_ok());
        assert_eq!(
            IdKey::from_base64("not base64!!").unwrap_err(),
            KeyError::Base64
        );
        // Padding is optional; standard alphabet both ways.
        let padded = IdKey::from_base64("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=").unwrap();
        let bare = IdKey::from_base64("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY").unwrap();
        assert_eq!(padded.secret(), bare.secret());
        assert_eq!(padded.secret().len(), 32);
    }

    #[test]
    fn env_loader_returns_none_when_unset() {
        // Only this test touches the variable; restore it afterwards.
        let previous = std::env::var(ENV).ok();
        std::env::remove_var(ENV);
        assert!(IdKey::from_env().unwrap().is_none());
        std::env::set_var(ENV, "");
        assert!(IdKey::from_env().unwrap().is_none());
        std::env::set_var(ENV, "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=");
        assert!(IdKey::from_env().unwrap().is_some());
        std::env::set_var(ENV, "MDEyMzQ1Njc4OWFiY2RlZg=="); // 16 bytes
        assert_eq!(IdKey::from_env().unwrap_err(), KeyError::TooShort);
        std::env::set_var(ENV, "short");
        assert_eq!(IdKey::from_env().unwrap_err(), KeyError::Base64);
        match previous {
            Some(value) => std::env::set_var(ENV, value),
            None => std::env::remove_var(ENV),
        }
    }

    #[test]
    fn derived_subkeys_are_domain_separated() {
        let key = key();
        let a2a = key.wrap(AGENT, OWNER, "task-1").unwrap();
        let mcp = key.derive_hmac_key(b"lumewarp/mcp-session-id/v1");
        // The same id under an MCP-derived key never verifies here.
        let tag = hmac::sign(&mcp, &mac_message(AGENT, OWNER, "YWJj"));
        let forged = format!(
            "lwt.{OWNER}.YWJj.{}",
            URL_SAFE_NO_PAD.encode(&tag.as_ref()[..MAC_BYTES])
        );
        assert_eq!(key.unwrap(AGENT, OWNER, &forged), Err(UnwrapError::NotOurs));
        assert_eq!(
            key.unwrap(AGENT, OWNER, &a2a).unwrap(),
            "task-1",
            "the real id is unaffected"
        );
        assert_ne!(
            hmac::sign(&key.derive_hmac_key(b"lumewarp/mcp-session-id/v1"), &[]).as_ref(),
            hmac::sign(&key.derive_hmac_key(b"lumewarp/other/v1"), &[]).as_ref()
        );
    }

    #[test]
    fn constant_time_equality_rejects_any_difference_or_length_change() {
        assert!(constant_time_eq(b"0123456789abcdef", b"0123456789abcdef"));
        assert!(!constant_time_eq(b"0123456789abcdef", b"0123456789abcdeF"));
        assert!(!constant_time_eq(b"0123456789abcdef", b"0123456789abcde"));
        assert!(!constant_time_eq(b"", b"x"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn secret_is_exposed_for_subkey_derivation_only() {
        let key = key();
        assert_eq!(key.secret().len(), 32);
        assert_eq!(format!("{key:?}"), "IdKey(<redacted>)");
    }
}

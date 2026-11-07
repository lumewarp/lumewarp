//! Opens upstream API keys sealed by the control plane
//! (`control-plane/src/lumewarp_control_plane/sealing.py`):
//!
//! ```text
//! 0x01 || 12-byte nonce || AES-256-GCM ciphertext || 16-byte tag
//! key = HKDF-SHA256(ikm = LUMEWARP_ENCRYPTION_KEY, salt = none, info = KEY_INFO)
//! ```
//!
//! Called while a snapshot is applied, never per request. Errors never carry key
//! material.

use ring::{aead, hkdf};

pub const ENV: &str = "LUMEWARP_ENCRYPTION_KEY";
const VERSION: u8 = 1;
const KEY_INFO: &[u8] = b"lumewarp/upstream-api-key/v1";

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum OpenError {
    #[error("LUMEWARP_ENCRYPTION_KEY is not set")]
    NoKey,
    #[error("unknown sealed format")]
    Format,
    #[error("does not open with LUMEWARP_ENCRYPTION_KEY")]
    Mismatch,
}

/// The key from the environment, when set.
pub fn env_secret() -> Option<String> {
    std::env::var(ENV).ok().filter(|s| !s.is_empty())
}

pub fn open(secret: Option<&str>, sealed: &[u8]) -> Result<String, OpenError> {
    let secret = secret.ok_or(OpenError::NoKey)?;
    if sealed.len() < 1 + aead::NONCE_LEN + aead::AES_256_GCM.tag_len() || sealed[0] != VERSION {
        return Err(OpenError::Format);
    }
    let mut key = [0u8; 32];
    // An empty salt equals RFC 5869's default (HashLen zero bytes) under HMAC.
    hkdf::Salt::new(hkdf::HKDF_SHA256, &[])
        .extract(secret.as_bytes())
        .expand(&[KEY_INFO], &aead::AES_256_GCM)
        .and_then(|okm| okm.fill(&mut key))
        .map_err(|_| OpenError::Mismatch)?;
    let key = aead::LessSafeKey::new(
        aead::UnboundKey::new(&aead::AES_256_GCM, &key).map_err(|_| OpenError::Mismatch)?,
    );
    let (nonce, body) = sealed[1..].split_at(aead::NONCE_LEN);
    let nonce = aead::Nonce::try_assume_unique_for_key(nonce).map_err(|_| OpenError::Format)?;
    let mut buf = body.to_vec();
    let plain = key
        .open_in_place(nonce, aead::Aad::empty(), &mut buf)
        .map_err(|_| OpenError::Mismatch)?;
    String::from_utf8(plain.to_vec()).map_err(|_| OpenError::Mismatch)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-encryption-key-0123456789abcdef";
    // Sealed by sealing.py with SECRET.
    const VECTOR: &str =
        "01cf29abe2c0cabded1dee59a04699ce44e9a26482cb8e102d94504cd57206704404b13d0b01faf8";

    #[test]
    fn opens_control_plane_ciphertext() {
        let sealed = hex::decode(VECTOR).unwrap();
        assert_eq!(open(Some(SECRET), &sealed).unwrap(), "sk-test-123");
    }

    #[test]
    fn rejects_wrong_key_tamper_and_format() {
        let sealed = hex::decode(VECTOR).unwrap();
        assert_eq!(
            open(Some("another-key-0123456789abcdef012345"), &sealed),
            Err(OpenError::Mismatch)
        );
        let mut tampered = sealed.clone();
        *tampered.last_mut().unwrap() ^= 1;
        assert_eq!(open(Some(SECRET), &tampered), Err(OpenError::Mismatch));
        let mut versioned = sealed.clone();
        versioned[0] = 2;
        assert_eq!(open(Some(SECRET), &versioned), Err(OpenError::Format));
        assert_eq!(open(Some(SECRET), &sealed[..10]), Err(OpenError::Format));
        assert_eq!(open(None, &sealed), Err(OpenError::NoKey));
    }
}

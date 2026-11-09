//! Domain-separated, authenticated and encrypted MCP routing handles.
use crate::a2a_ids::IdKey;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use ring::{
    aead, hkdf,
    rand::{SecureRandom, SystemRandom},
};
use serde::{de::DeserializeOwned, Deserialize, Serialize};

pub const MAX_TOKEN: usize = 8192;
pub const TTL: u64 = 86400;
/// Different gateways and small wall-clock corrections need not agree exactly.
/// This bounds future issuance only; the 24-hour expiry is never extended.
pub const ISSUED_CLOCK_SKEW: u64 = 60;
pub fn valid_at(issued: u64, now: u64) -> bool {
    issued <= now.saturating_add(ISSUED_CLOCK_SKEW) && now.saturating_sub(issued) < TTL
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Leg {
    pub name: String,
    pub target: String,
    pub url: String,
    pub auth: String,
    pub session: Option<String>,
    pub version: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Session {
    pub owner: String,
    pub credential: String,
    pub route: String,
    pub issued: u64,
    pub nonce: String,
    pub version: String,
    pub legs: Vec<Leg>,
    /// The `clientInfo` the session was opened with, bounded, for usage only.
    #[serde(default)]
    pub client_name: String,
    #[serde(default)]
    pub client_version: String,
    /// The `protocolVersion` the client's initialize asked for, as sent (bounded);
    /// `version` is the one the session runs.
    #[serde(default)]
    pub client_protocol: String,
}
#[derive(Serialize, Deserialize)]
pub struct Cursor {
    pub session: String,
    pub method: String,
    pub index: usize,
    pub upstream: Option<String>,
}
pub fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn key(id: &IdKey, purpose: &[u8]) -> Result<aead::LessSafeKey, ()> {
    let mut raw = [0; 32];
    hkdf::Salt::new(hkdf::HKDF_SHA256, &[])
        .extract(id.secret())
        .expand(&[purpose], &aead::AES_256_GCM)
        .map_err(|_| ())?
        .fill(&mut raw)
        .map_err(|_| ())?;
    Ok(aead::LessSafeKey::new(
        aead::UnboundKey::new(&aead::AES_256_GCM, &raw).map_err(|_| ())?,
    ))
}
pub fn seal<T: Serialize>(id: &IdKey, purpose: &[u8], value: &T) -> Result<String, ()> {
    let mut nonce = [0; 12];
    SystemRandom::new().fill(&mut nonce).map_err(|_| ())?;
    let mut data = serde_json::to_vec(value).map_err(|_| ())?;
    key(id, purpose)?
        .seal_in_place_append_tag(
            aead::Nonce::assume_unique_for_key(nonce),
            aead::Aad::empty(),
            &mut data,
        )
        .map_err(|_| ())?;
    let mut wire = nonce.to_vec();
    wire.extend(data);
    let token = URL_SAFE_NO_PAD.encode(wire);
    if token.len() > MAX_TOKEN {
        return Err(());
    }
    Ok(token)
}
pub fn open<T: DeserializeOwned>(id: &IdKey, purpose: &[u8], token: &str) -> Result<T, ()> {
    if token.len() > MAX_TOKEN {
        return Err(());
    }
    let wire = URL_SAFE_NO_PAD.decode(token).map_err(|_| ())?;
    if wire.len() < 28 {
        return Err(());
    }
    let nonce = aead::Nonce::try_assume_unique_for_key(&wire[..12]).map_err(|_| ())?;
    let mut data = wire[12..].to_vec();
    let plain = key(id, purpose)?
        .open_in_place(nonce, aead::Aad::empty(), &mut data)
        .map_err(|_| ())?;
    serde_json::from_slice(plain).map_err(|_| ())
}
pub const SESSION: &[u8] = b"lumewarp/mcp/session/v1";
pub const CURSOR: &[u8] = b"lumewarp/mcp/cursor/v1";

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn issuance_tolerates_clock_corrections_but_expiry_remains_exact() {
        let issued = 1_000;
        assert!(valid_at(issued, issued - 1));
        assert!(valid_at(issued, issued - ISSUED_CLOCK_SKEW));
        assert!(!valid_at(issued, issued - ISSUED_CLOCK_SKEW - 1));
        assert!(valid_at(issued, issued + TTL - 1));
        assert!(!valid_at(issued, issued + TTL));
        assert!(!valid_at(issued, issued + TTL + 1));
    }
    #[test]
    fn cross_process_domains_and_tampering() {
        let a = IdKey::from_secret(vec![42; 32]).unwrap();
        let b = IdKey::from_secret(vec![42; 32]).unwrap();
        let token = seal(&a, SESSION, &vec!["private-session"]).unwrap();
        assert_eq!(
            open::<Vec<String>>(&b, SESSION, &token).unwrap(),
            vec!["private-session"]
        );
        assert!(open::<Vec<String>>(&b, CURSOR, &token).is_err());
        assert!(
            open::<Vec<String>>(&IdKey::from_secret(vec![43; 32]).unwrap(), SESSION, &token)
                .is_err()
        );
        assert!(seal(&a, SESSION, &"x".repeat(MAX_TOKEN)).is_err());
    }
}

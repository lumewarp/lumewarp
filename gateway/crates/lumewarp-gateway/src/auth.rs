use std::collections::HashSet;

use hyper::{header, HeaderMap};
use sha2::{Digest, Sha256};

pub struct Auth {
    hashes: HashSet<[u8; 32]>,
}

impl Auth {
    pub fn compile(keys: &[String]) -> anyhow::Result<Self> {
        let hashes = keys
            .iter()
            .map(|key| {
                let mut hash = [0; 32];
                hex::decode_to_slice(key, &mut hash)?;
                Ok(hash)
            })
            .collect::<Result<_, hex::FromHexError>>()?;
        Ok(Self { hashes })
    }

    pub fn allows(&self, headers: &HeaderMap) -> bool {
        if self.hashes.is_empty() {
            return true;
        }
        let mut values = headers.get_all(header::AUTHORIZATION).iter();
        let Some(value) = values.next() else {
            return false;
        };
        if values.next().is_some() {
            return false;
        }
        let Ok(value) = value.to_str() else {
            return false;
        };
        let Some((scheme, key)) = value.split_once(' ') else {
            return false;
        };
        let key = key.trim_start();
        if !scheme.eq_ignore_ascii_case("Bearer") || key.is_empty() {
            return false;
        }
        let hash: [u8; 32] = Sha256::digest(key.as_bytes()).into();
        self.hashes.contains(&hash)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checks_hash_and_open_mode() {
        let auth = Auth::compile(&[hex::encode(Sha256::digest(b"secret"))]).unwrap();
        let mut headers = HeaderMap::new();
        assert!(!auth.allows(&headers));
        headers.insert(header::AUTHORIZATION, "Bearer wrong".parse().unwrap());
        assert!(!auth.allows(&headers));
        headers.insert(header::AUTHORIZATION, "Bearer secret".parse().unwrap());
        assert!(auth.allows(&headers));
        headers.insert(header::AUTHORIZATION, "Bearer  secret".parse().unwrap());
        assert!(auth.allows(&headers));
        assert!(Auth::compile(&[]).unwrap().allows(&HeaderMap::new()));
    }
}

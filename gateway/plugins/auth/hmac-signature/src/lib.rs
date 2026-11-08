//! `hmac-signature`: signs every upstream request with a shared secret.
//!
//! The scheme is `bench/mock-upstream/HMAC.md`; the mock MCP server and A2A
//! agent verify it in their `--auth hmac` mode. The gateway sends
//!
//! ```text
//! x-hmac-signature: t=<unix-ms>,v1=<hex hmac-sha256(secret, t.method.path.sha256hex(body))>
//! ```
//!
//! so a server can prove the gateway authenticated the call itself instead of
//! forwarding a client token. Only the method, the path and the exact body bytes
//! are covered: the signature proves possession of the secret, not integrity of
//! the headers.
//!
//! The secret never appears in settings. It is read through `secret()` from
//! `LUMEWARP_PLUGIN_SECRET_<secret_name>` when the plugin is configured, so the
//! gateway may persist the settings to disk untouched.
//!
//! `cache-ms` is always `0`: the signature covers this request's body and time,
//! and a reused one would be wrong (and soon stale).

use hmac::{Hmac, Mac};
use lumewarp_plugin_sdk::{
    auth::lumewarp::plugin::host, auth_types, register, Authenticator, ConfigError, ConfigureAuth,
};
use schemars::JsonSchema;
use serde::Deserialize;
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

/// The header the mock verifies; lowercase, as `HeaderName` compares.
pub const HEADER: &str = "x-hmac-signature";

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Environment variable holding the shared secret, without the
    /// `LUMEWARP_PLUGIN_SECRET_` prefix.
    #[schemars(title = "Secret name")]
    pub secret_name: String,
}

pub struct HmacSignature {
    secret: Vec<u8>,
}

impl ConfigureAuth<Params> for HmacSignature {
    fn configure(_: &auth_types::TargetInfo, params: Params) -> Result<Self, ConfigError> {
        if params.secret_name.is_empty() {
            return Err(ConfigError("secret_name is required".into()));
        }
        host::secret(&params.secret_name)
            .map(|secret| Self {
                secret: secret.into_bytes(),
            })
            .ok_or_else(|| {
                ConfigError(format!(
                    "LUMEWARP_PLUGIN_SECRET_{} is not set",
                    params.secret_name
                ))
            })
    }
}

impl Authenticator for HmacSignature {
    fn authorize(
        &self,
        request: auth_types::OutboundRequest,
    ) -> Result<auth_types::AuthResult, String> {
        let header = signature_header(
            &self.secret,
            host::now_unix_ms(),
            &request.method,
            &request.url,
            &request.body,
        )?;
        Ok(auth_types::AuthResult {
            headers: vec![(HEADER.to_owned(), header.into_bytes())],
            // Body-dependent: never cache (HMAC.md, "Gateway plugin notes").
            cache_ms: 0,
        })
    }
}

/// The `t=...,v1=...` value for one request at `now_ms`.
fn signature_header(
    secret: &[u8],
    now_ms: u64,
    method: &str,
    url: &str,
    body: &[u8],
) -> Result<String, String> {
    let path = request_path(url).ok_or_else(|| "request url has no path".to_string())?;
    let method = method.to_ascii_uppercase();
    let mac = sign(secret, now_ms, &method, path, body);
    Ok(format!("t={now_ms},v1={mac}"))
}

/// The request target without the query string, byte for byte (HMAC.md,
/// "Canonical string"). No percent-decoding and no trailing-slash change; a URL
/// with no path signs `/`, which is what an HTTP client puts on the request line.
fn request_path(url: &str) -> Option<&str> {
    let start = url.find("://").map_or(0, |scheme| scheme + 3);
    let rest = &url[start..];
    let Some(slash) = rest.find('/') else {
        return Some("/");
    };
    let path = &rest[slash..];
    let path = path.split(['?', '#']).next().unwrap_or(path);
    Some(if path.is_empty() { "/" } else { path })
}

/// `hex(hmac-sha256(secret, "<unix-ms>.<METHOD>.<path>.<sha256hex(body)>"))`.
fn sign(secret: &[u8], now_ms: u64, method: &str, path: &str, body: &[u8]) -> String {
    let body_hash = hex::encode(Sha256::digest(body));
    let canonical = format!("{now_ms}.{method}.{path}.{body_hash}");
    let mut mac = HmacSha256::new_from_slice(secret).expect("HMAC accepts keys of any length");
    mac.update(canonical.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

register! {
    kind: "upstream-auth",
    name: "hmac_signature",
    title: "HMAC signature",
    description: "Sign each upstream request with a shared secret (x-hmac-signature).",
    params: Params,
    authenticator: HmacSignature,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The worked example in `bench/mock-upstream/HMAC.md`.
    #[test]
    fn matches_the_documented_worked_example() {
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"ping"}"#;
        assert_eq!(
            hex::encode(Sha256::digest(body)),
            "98e0961a7c1232f08d2f2187d13c4a1a22a0641e00e5dec0eca645d646077fab"
        );
        assert_eq!(
            sign(
                b"lumewarp-dev-secret",
                1_762_500_000_000,
                "POST",
                "/mcp",
                body
            ),
            "b2f1c5fd7baf3e518a7ddcfaecda5e8fc402fe70c5e144464c6803bd8ce6d47b"
        );
        assert_eq!(
            signature_header(
                b"lumewarp-dev-secret",
                1_762_500_000_000,
                "post",
                "https://mcp.example.test/mcp?x=1",
                body
            )
            .unwrap(),
            "t=1762500000000,v1=b2f1c5fd7baf3e518a7ddcfaecda5e8fc402fe70c5e144464c6803bd8ce6d47b"
        );
    }

    #[test]
    fn paths_are_the_request_target_without_the_query() {
        assert_eq!(request_path("https://h.test/mcp"), Some("/mcp"));
        assert_eq!(
            request_path("https://h.test:8443/a2a/v1?x=1"),
            Some("/a2a/v1")
        );
        assert_eq!(request_path("http://h.test/a2a?x=/mcp"), Some("/a2a"));
        assert_eq!(request_path("https://h.test"), Some("/"));
        assert_eq!(request_path("https://h.test?x=1"), Some("/"));
        // No normalisation: a trailing slash is signed as sent.
        assert_eq!(request_path("https://h.test/mcp/"), Some("/mcp/"));
        assert_eq!(request_path("https://h.test/%6Dcp"), Some("/%6Dcp"));
    }

    #[test]
    fn the_body_and_the_time_change_the_signature() {
        let secret = b"secret";
        let one = sign(secret, 1, "POST", "/mcp", b"a");
        assert_ne!(one, sign(secret, 1, "POST", "/mcp", b"b"));
        assert_ne!(one, sign(secret, 2, "POST", "/mcp", b"a"));
        assert_ne!(one, sign(secret, 1, "PUT", "/mcp", b"a"));
        assert_ne!(one, sign(secret, 1, "POST", "/a", b"a"));
        assert_ne!(one, sign(b"other", 1, "POST", "/mcp", b"a"));
        // A request without a body signs the digest of the empty string.
        assert_eq!(
            sign(secret, 1, "GET", "/mcp", b""),
            sign(secret, 1, "GET", "/mcp", &[])
        );
    }

    #[test]
    fn the_signature_is_lowercase_hex_and_signed_for_now() {
        let header = signature_header(b"secret", 42, "POST", "https://h.test/mcp", b"x").unwrap();
        let (time, signature) = header.split_once(",v1=").unwrap();
        assert_eq!(time, "t=42");
        assert_eq!(signature.len(), 64);
        assert!(signature
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
    }
}

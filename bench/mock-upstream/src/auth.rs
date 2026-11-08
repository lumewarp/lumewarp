//! Authentication modes for the example MCP server and A2A agent.
//!
//! The HMAC mode verifies the scheme defined in `HMAC.md`, which the gateway's
//! future `hmac-signature` upstream-auth plugin will produce.

use crate::util::{hex_decode, now_ms, sha256_hex};
use clap::{Args, ValueEnum};
use hmac::{Hmac, Mac};
use hyper::header::HeaderMap;
use sha2::Sha256;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, ValueEnum)]
pub enum AuthMode {
    /// No credentials are checked.
    #[default]
    None,
    /// Exactly `Authorization: Bearer <--bearer>`.
    Bearer,
    /// Any non-empty `Authorization: Bearer <token>` (a passthrough user token).
    #[value(name = "any-bearer")]
    AnyBearer,
    /// Verify `x-hmac-signature` as described in `HMAC.md`.
    Hmac,
}

#[derive(Clone, Debug, Args)]
pub struct AuthOptions {
    /// none, bearer, any-bearer or hmac.
    #[arg(long, value_enum, default_value = "none")]
    pub auth: AuthMode,
    /// Bearer token required by `--auth bearer`.
    #[arg(long, value_name = "TOKEN")]
    pub bearer: Option<String>,
    /// Shared secret required by `--auth hmac`.
    #[arg(long, value_name = "SECRET")]
    pub hmac_secret: Option<String>,
    /// Reject HMAC timestamps further than this from the server clock.
    #[arg(long, default_value_t = 300_000)]
    pub hmac_skew_ms: u64,
}

impl Default for AuthOptions {
    fn default() -> Self {
        Self {
            auth: AuthMode::None,
            bearer: None,
            hmac_secret: None,
            hmac_skew_ms: 300_000,
        }
    }
}

impl AuthOptions {
    /// Check the options used for one server; the message names the flags so
    /// a configuration mistake is obvious at startup.
    pub fn build(&self) -> Result<Auth, String> {
        match self.auth {
            AuthMode::Bearer if self.bearer.is_none() => {
                Err("--auth bearer needs --bearer TOKEN".to_owned())
            }
            AuthMode::Hmac if self.hmac_secret.is_none() => {
                Err("--auth hmac needs --hmac-secret SECRET".to_owned())
            }
            _ => Ok(Auth {
                mode: self.auth,
                bearer: self.bearer.clone(),
                hmac_secret: self.hmac_secret.clone(),
                hmac_skew_ms: self.hmac_skew_ms,
            }),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Auth {
    mode: AuthMode,
    bearer: Option<String>,
    hmac_secret: Option<String>,
    hmac_skew_ms: u64,
}

#[derive(Clone, Debug)]
pub struct AuthFailure {
    /// Stable machine-readable reason, e.g. `missing_credentials`.
    pub reason: &'static str,
    /// Human-readable explanation.
    pub detail: String,
    /// `WWW-Authenticate` header value.
    pub challenge: &'static str,
}

impl AuthFailure {
    fn new(reason: &'static str, detail: String, challenge: &'static str) -> Self {
        Self {
            reason,
            detail,
            challenge,
        }
    }
}

impl Auth {
    /// `method` must be the HTTP method as sent and `path` the request target
    /// without a query string. `body` is the exact bytes received.
    pub fn check(
        &self,
        method: &str,
        path: &str,
        headers: &HeaderMap,
        body: &[u8],
    ) -> Result<(), AuthFailure> {
        match self.mode {
            AuthMode::None => Ok(()),
            AuthMode::Bearer => {
                let expected = self.bearer.as_deref().unwrap_or_default();
                match header(headers, "authorization") {
                    Some(value) if bearer_token(value) == Some(expected) => Ok(()),
                    Some(_) => Err(AuthFailure::new(
                        "invalid_token",
                        "Authorization header does not carry the configured bearer token"
                            .to_owned(),
                        "Bearer realm=\"mock\"",
                    )),
                    None => Err(AuthFailure::new(
                        "missing_credentials",
                        "Authorization header is required".to_owned(),
                        "Bearer realm=\"mock\"",
                    )),
                }
            }
            AuthMode::AnyBearer => match header(headers, "authorization") {
                Some(value) if bearer_token(value).is_some_and(|token| !token.is_empty()) => Ok(()),
                Some(_) => Err(AuthFailure::new(
                    "invalid_token",
                    "expected Authorization: Bearer <token>".to_owned(),
                    "Bearer realm=\"mock\"",
                )),
                None => Err(AuthFailure::new(
                    "missing_credentials",
                    "Authorization header is required".to_owned(),
                    "Bearer realm=\"mock\"",
                )),
            },
            AuthMode::Hmac => self.verify_hmac(method, path, headers, body),
        }
    }

    fn verify_hmac(
        &self,
        method: &str,
        path: &str,
        headers: &HeaderMap,
        body: &[u8],
    ) -> Result<(), AuthFailure> {
        let challenge = "HmacSignature realm=\"mock\"";
        let malformed = |detail: String| AuthFailure::new("malformed_signature", detail, challenge);
        let Some(raw) = header(headers, "x-hmac-signature") else {
            return Err(AuthFailure::new(
                "missing_signature",
                "x-hmac-signature header is required".to_owned(),
                challenge,
            ));
        };
        let mut timestamp = None;
        let mut signatures = Vec::new();
        for part in raw.split(',') {
            let part = part.trim();
            let Some((key, value)) = part.split_once('=') else {
                return Err(malformed(format!(
                    "signature part {part:?} is not key=value"
                )));
            };
            match key.trim() {
                "t" => {
                    timestamp = Some(value.trim().parse::<u64>().map_err(|_| {
                        malformed(format!(
                            "timestamp {:?} is not a unix-ms integer",
                            value.trim()
                        ))
                    })?);
                }
                "v1" => signatures.push(value.trim()),
                _ => {}
            }
        }
        let Some(timestamp) = timestamp else {
            return Err(malformed("missing t= timestamp".to_owned()));
        };
        if signatures.is_empty() {
            return Err(malformed("missing v1= signature".to_owned()));
        }
        let now = now_ms();
        let skew = now.abs_diff(timestamp);
        if skew > self.hmac_skew_ms {
            return Err(AuthFailure::new(
                "signature_stale",
                format!("timestamp is {skew} ms from the server clock"),
                challenge,
            ));
        }
        let secret = self.hmac_secret.as_deref().unwrap_or_default();
        let canonical = format!("{timestamp}.{method}.{path}.{}", sha256_hex(body));
        let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
            .expect("HMAC accepts keys of any length");
        mac.update(canonical.as_bytes());
        for signature in signatures {
            let Some(bytes) = hex_decode(signature) else {
                return Err(malformed(format!(
                    "signature {signature:?} is not lowercase hex"
                )));
            };
            if mac.clone().verify_slice(&bytes).is_ok() {
                return Ok(());
            }
        }
        Err(AuthFailure::new(
            "signature_mismatch",
            "the HMAC does not match the request".to_owned(),
            challenge,
        ))
    }
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|value| value.to_str().ok())
}

fn bearer_token(value: &str) -> Option<&str> {
    let (scheme, token) = value.split_once(' ')?;
    scheme.eq_ignore_ascii_case("bearer").then(|| token.trim())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::hex_encode;
    use hyper::header::HeaderValue;

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.insert(
                hyper::header::HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        headers
    }

    #[test]
    fn bearer_modes() {
        let auth = AuthOptions {
            auth: AuthMode::Bearer,
            bearer: Some("token".to_owned()),
            ..AuthOptions::default()
        }
        .build()
        .unwrap();
        assert!(auth
            .check(
                "POST",
                "/mcp",
                &headers(&[("authorization", "Bearer token")]),
                b""
            )
            .is_ok());
        assert_eq!(
            auth.check(
                "POST",
                "/mcp",
                &headers(&[("authorization", "Bearer other")]),
                b""
            )
            .unwrap_err()
            .reason,
            "invalid_token"
        );
        assert_eq!(
            auth.check("POST", "/mcp", &headers(&[]), b"")
                .unwrap_err()
                .reason,
            "missing_credentials"
        );
    }

    #[test]
    fn hmac_good_bad_and_stale() {
        let auth = AuthOptions {
            auth: AuthMode::Hmac,
            hmac_secret: Some("secret".to_owned()),
            hmac_skew_ms: 5_000,
            ..AuthOptions::default()
        }
        .build()
        .unwrap();
        let body = br#"{"jsonrpc":"2.0"}"#;
        let signed = |timestamp: u64| {
            let canonical = format!("{timestamp}.POST./mcp.{}", sha256_hex(body));
            let mut mac = Hmac::<Sha256>::new_from_slice(b"secret").unwrap();
            mac.update(canonical.as_bytes());
            hex_encode(&mac.finalize().into_bytes())
        };
        let now = now_ms();
        let good = format!("t={now},v1={}", signed(now));
        assert!(auth
            .check(
                "POST",
                "/mcp",
                &headers(&[("x-hmac-signature", &good)]),
                body
            )
            .is_ok());
        let bad = format!("t={now},v1={}", signed(now - 1));
        assert_eq!(
            auth.check(
                "POST",
                "/mcp",
                &headers(&[("x-hmac-signature", &bad)]),
                body
            )
            .unwrap_err()
            .reason,
            "signature_mismatch"
        );
        let stale = format!("t={},v1={}", now - 60_000, signed(now - 60_000));
        assert_eq!(
            auth.check(
                "POST",
                "/mcp",
                &headers(&[("x-hmac-signature", &stale)]),
                body
            )
            .unwrap_err()
            .reason,
            "signature_stale"
        );
        let malformed = format!("t={now},v1=not-hex");
        assert_eq!(
            auth.check(
                "POST",
                "/mcp",
                &headers(&[("x-hmac-signature", &malformed)]),
                body
            )
            .unwrap_err()
            .reason,
            "malformed_signature"
        );
    }
}

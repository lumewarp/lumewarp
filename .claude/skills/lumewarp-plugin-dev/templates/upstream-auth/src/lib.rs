//! `my_auth`: OAuth 2.0 client credentials. Exchanges a client id and secret
//! for an access token at `token_url` and sends it as `Authorization: Bearer`.
//!
//! The client secret never appears in settings: it is read through `secret()`
//! from `LUMEWARP_PLUGIN_SECRET_<client_secret_name>`. The router settings also
//! need the reserved `allowed_origins` listing the token endpoint's origin, or
//! `http-fetch` refuses the call; the gateway removes that field before
//! `configure`, so `Params` does not declare it.
//!
//! The result does not depend on the request, so it is cached for the token's
//! lifetime minus a margin (the gateway caps any value at one hour).

use lumewarp_plugin_sdk::{
    auth::lumewarp::plugin::host, auth_types, register, serde_json, Authenticator, ConfigError,
    ConfigureAuth,
};
use schemars::JsonSchema;
use serde::Deserialize;

/// Seconds before expiry when a cached token is no longer reused.
const EXPIRY_MARGIN_S: u64 = 30;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Token endpoint; its origin must also be listed in `allowed_origins`.
    #[schemars(title = "Token URL")]
    pub token_url: String,
    #[schemars(title = "Client id")]
    pub client_id: String,
    /// Environment variable holding the client secret, without the
    /// `LUMEWARP_PLUGIN_SECRET_` prefix.
    #[schemars(title = "Client secret name")]
    pub client_secret_name: String,
    /// Space-separated scopes; empty sends none.
    #[serde(default)]
    #[schemars(title = "Scope")]
    pub scope: String,
}

pub struct MyAuth {
    token_url: String,
    form: String,
}

impl ConfigureAuth<Params> for MyAuth {
    fn configure(_: &auth_types::TargetInfo, params: Params) -> Result<Self, ConfigError> {
        if !(params.token_url.starts_with("https://") || params.token_url.starts_with("http://")) {
            return Err(ConfigError("token_url must be an http(s) URL".into()));
        }
        if params.client_id.is_empty() || params.client_secret_name.is_empty() {
            return Err(ConfigError(
                "client_id and client_secret_name are required".into(),
            ));
        }
        // A missing secret refuses the configuration version instead of
        // failing every request later.
        let secret = host::secret(&params.client_secret_name).ok_or_else(|| {
            ConfigError(format!(
                "LUMEWARP_PLUGIN_SECRET_{} is not set",
                params.client_secret_name
            ))
        })?;
        Ok(Self {
            form: token_form(&params.client_id, &secret, &params.scope),
            token_url: params.token_url,
        })
    }
}

impl Authenticator for MyAuth {
    fn authorize(&self, _: auth_types::OutboundRequest) -> Result<auth_types::AuthResult, String> {
        let response = host::http_fetch(&host::HttpRequest {
            method: "POST".to_owned(),
            url: self.token_url.clone(),
            headers: vec![(
                "content-type".to_owned(),
                b"application/x-www-form-urlencoded".to_vec(),
            )],
            body: self.form.clone().into_bytes(),
        })?;
        if !(200..300).contains(&response.status) {
            // Never echo the body: it may contain the request's credentials.
            return Err(format!("token endpoint answered {}", response.status));
        }
        let (token, cache_ms) = parse_token(&response.body)?;
        Ok(auth_types::AuthResult {
            headers: vec![(
                "authorization".to_owned(),
                format!("Bearer {token}").into_bytes(),
            )],
            cache_ms,
        })
    }
}

/// The `application/x-www-form-urlencoded` body of the token request.
fn token_form(client_id: &str, client_secret: &str, scope: &str) -> String {
    let mut form = format!(
        "grant_type=client_credentials&client_id={}&client_secret={}",
        encode(client_id),
        encode(client_secret)
    );
    if !scope.is_empty() {
        form.push_str("&scope=");
        form.push_str(&encode(scope));
    }
    form
}

/// Percent-encodes everything but the RFC 3986 unreserved characters.
fn encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// The access token and how long to cache it, from a token response body.
fn parse_token(body: &[u8]) -> Result<(String, u32), String> {
    #[derive(Deserialize)]
    struct TokenResponse {
        access_token: String,
        expires_in: Option<u64>,
    }
    let response: TokenResponse =
        serde_json::from_slice(body).map_err(|_| "token response is not valid JSON".to_string())?;
    if response.access_token.is_empty() {
        return Err("token response has an empty access_token".into());
    }
    let cache_ms = response
        .expires_in
        .map_or(0, |seconds| seconds.saturating_sub(EXPIRY_MARGIN_S) * 1000)
        .min(u64::from(u32::MAX)) as u32;
    Ok((response.access_token, cache_ms))
}

register! {
    kind: "upstream-auth",
    name: "my_auth",
    title: "OAuth client credentials",
    description: "Exchange a client id and secret for a bearer token.",
    params: Params,
    authenticator: MyAuth,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_form_is_percent_encoded() {
        assert_eq!(
            token_form("id", "s3cr&t=", "read write"),
            "grant_type=client_credentials&client_id=id&client_secret=s3cr%26t%3D&scope=read%20write"
        );
        assert_eq!(
            token_form("id", "s", ""),
            "grant_type=client_credentials&client_id=id&client_secret=s"
        );
    }

    #[test]
    fn the_token_is_cached_for_its_lifetime_minus_the_margin() {
        let body = br#"{"access_token":"t","expires_in":3600,"token_type":"Bearer"}"#;
        assert_eq!(parse_token(body).unwrap(), ("t".to_owned(), 3_570_000));
        assert_eq!(parse_token(br#"{"access_token":"t"}"#).unwrap().1, 0);
        assert_eq!(
            parse_token(br#"{"access_token":"t","expires_in":10}"#)
                .unwrap()
                .1,
            0
        );
    }

    #[test]
    fn a_bad_response_is_an_error() {
        assert!(parse_token(b"not json").is_err());
        assert!(parse_token(br#"{"access_token":""}"#).is_err());
        assert!(parse_token(br#"{"error":"invalid_client"}"#).is_err());
    }
}

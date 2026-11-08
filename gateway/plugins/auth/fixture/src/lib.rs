//! Test fixture for the `upstream-auth` adapter. Not shipped: `build.sh` builds
//! it only for `--tests`, into `target/plugins-test/auth/`.
//!
//! One plugin covers what the adapter tests need, selected by `mode`:
//!
//! | `mode` | Behaviour |
//! | --- | --- |
//! | `fixed` | Waits `delay_ms`, then returns `headers` (each `"name: value"`) plus an `x-calls` counter for this instance, and `cache_ms`. |
//! | `fetch` | `http-fetch`es `url` and returns its status and body as headers, `cache_ms` from settings. |
//! | `configure_fetch` | `http-fetch`es `url` once per instance **at configure time**, then behaves like `fixed`: a server that counts its requests can prove a target was configured (N1). |
//! | `echo` | Returns `x-echo: <value of echo_header>`, or `absent`. |
//! | `secret` | Returns what `secret(secret_name)` gave, or `none`. |
//! | `error` | Returns `Err("fixture error")`. |
//! | `stuck_configure` | Never returns from `configure` (the apply-time budget must cut it). |
//! | `loop` | Spins forever, so the epoch deadline trips. |
//!
//! `x-calls` is what proves a cache hit (`1` on both answers) and a configure
//! (a fresh instance starts at `1` again).

use std::sync::atomic::{AtomicU32, Ordering};

use lumewarp_plugin_sdk::{
    auth::lumewarp::plugin::host, auth_types, register, Authenticator, ConfigError, ConfigureAuth,
};
use schemars::JsonSchema;
use serde::Deserialize;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// `fixed`, `fetch`, `configure_fetch`, `echo`, `secret`, `error` or `loop`.
    pub mode: String,
    /// Headers to return in `fixed` mode, each written `"name: value"`.
    #[serde(default)]
    pub headers: Vec<String>,
    /// How long the result may be reused.
    #[serde(default)]
    pub cache_ms: u32,
    /// `fixed` mode: busy-wait this long before answering, so concurrent calls
    /// really overlap.
    #[serde(default)]
    pub delay_ms: u64,
    /// `fetch` and `configure_fetch` mode: the URL to fetch.
    #[serde(default)]
    pub url: String,
    /// `secret` mode: the name passed to `secret()`.
    #[serde(default)]
    pub secret_name: String,
    /// `echo` mode: the request header to echo back.
    #[serde(default)]
    pub echo_header: String,
}

pub struct Fixture {
    params: Params,
    calls: AtomicU32,
}

impl ConfigureAuth<Params> for Fixture {
    fn configure(_: &auth_types::TargetInfo, params: Params) -> Result<Self, ConfigError> {
        match params.mode.as_str() {
            "fixed" | "fetch" | "configure_fetch" | "echo" | "secret" | "error" | "loop" => {}
            "stuck_configure" => loop {
                std::hint::spin_loop();
            },
            other => return Err(ConfigError(format!("unknown mode {other:?}"))),
        }
        if matches!(params.mode.as_str(), "fetch" | "configure_fetch") && params.url.is_empty() {
            return Err(ConfigError(format!("{} mode needs url", params.mode)));
        }
        for header in &params.headers {
            parse_header(header).map_err(|error| ConfigError(error.to_string()))?;
        }
        if params.mode == "configure_fetch" {
            // One fetch per configured instance: a test counts them to prove a
            // target was configured once, at apply time (N1).
            host::http_fetch(&host::HttpRequest {
                method: "POST".to_owned(),
                url: params.url.clone(),
                headers: vec![("content-type".to_owned(), b"application/json".to_vec())],
                body: b"{}".to_vec(),
            })
            .map_err(ConfigError)?;
        }
        Ok(Self {
            params,
            calls: AtomicU32::new(0),
        })
    }
}

fn parse_header(text: &str) -> Result<(String, Vec<u8>), String> {
    match text.split_once(':') {
        Some((name, value)) if !name.is_empty() => Ok((
            name.trim().to_owned(),
            value.trim_start().as_bytes().to_vec(),
        )),
        _ => Err(format!("header {text:?} is not \"name: value\"")),
    }
}

impl Authenticator for Fixture {
    fn authorize(
        &self,
        request: auth_types::OutboundRequest,
    ) -> Result<auth_types::AuthResult, String> {
        let call = self.calls.fetch_add(1, Ordering::Relaxed) + 1;
        let mut result = auth_types::AuthResult {
            headers: Vec::new(),
            cache_ms: self.params.cache_ms,
        };
        match self.params.mode.as_str() {
            "fixed" | "configure_fetch" => {
                let until = host::now_unix_ms() + self.params.delay_ms;
                while host::now_unix_ms() < until {
                    std::hint::spin_loop();
                }
                for header in &self.params.headers {
                    result.headers.push(parse_header(header)?);
                }
                result
                    .headers
                    .push(("x-calls".to_owned(), call.to_string().into_bytes()));
            }
            "fetch" => {
                let response = host::http_fetch(&host::HttpRequest {
                    method: "POST".to_owned(),
                    url: self.params.url.clone(),
                    headers: vec![("content-type".to_owned(), b"application/json".to_vec())],
                    body: b"{\"exchange\":true}".to_vec(),
                })?;
                result.headers.push((
                    "x-fetch-status".to_owned(),
                    response.status.to_string().into_bytes(),
                ));
                result
                    .headers
                    .push(("x-fetch-body".to_owned(), response.body));
            }
            "echo" => {
                let value = request
                    .headers
                    .iter()
                    .find(|(name, _)| name.eq_ignore_ascii_case(&self.params.echo_header))
                    .map(|(_, value)| value.clone())
                    .unwrap_or_else(|| b"absent".to_vec());
                result.headers.push(("x-echo".to_owned(), value));
            }
            "secret" => {
                let secret = host::secret(&self.params.secret_name);
                result.headers.push((
                    "x-secret".to_owned(),
                    secret.unwrap_or_else(|| "none".to_owned()).into_bytes(),
                ));
            }
            "error" => return Err("fixture error".to_owned()),
            "loop" => loop {
                std::hint::spin_loop();
            },
            _ => unreachable!("configure checked the mode"),
        }
        Ok(result)
    }
}

register! {
    kind: "upstream-auth",
    name: "auth_fixture",
    title: "Auth fixture",
    description: "Test fixture for the upstream-auth adapter; not for deployment.",
    params: Params,
    authenticator: Fixture,
}

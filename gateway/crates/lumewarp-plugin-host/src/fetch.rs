//! The transport behind `host.http-fetch` (`docs/plugins.md`, "Upstream auth").
//!
//! An auth plugin may reach out to its own identity provider, but only under an
//! origin its settings list in `allowed_origins`. This module owns that check
//! and the HTTP client: exact origin match, no redirects followed (the client
//! never follows them), bounded request and response sizes, and a per-call
//! timeout that the caller sets.
//!
//! Blocking is the caller's business: [`send`] is a future, and the adapter in
//! [`crate::authenticator`] runs it on the tokio runtime from a blocking task,
//! so a slow identity provider never parks a runtime worker.

use std::{collections::HashSet, sync::OnceLock};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{
    header::{HeaderName, HeaderValue},
    Request, Uri,
};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::TokioExecutor,
};

/// Largest response body handed to a plugin; the rest is dropped.
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
/// Largest request body a plugin may send.
pub const MAX_REQUEST_BYTES: usize = 1024 * 1024;
/// Largest number of request or response headers accepted.
pub const MAX_HEADERS: usize = 128;
/// Largest total size of request or response headers.
pub const MAX_HEADER_BYTES: usize = 32 * 1024;

type Body = Full<Bytes>;
type HttpClient = Client<HttpsConnector<HttpConnector>, Body>;

fn client() -> &'static HttpClient {
    static CLIENT: OnceLock<HttpClient> = OnceLock::new();
    CLIENT.get_or_init(|| {
        let mut http = HttpConnector::new();
        http.set_nodelay(true);
        // `enforce_http(false)` lets the TLS connector see https URLs.
        http.enforce_http(false);
        let connector = HttpsConnectorBuilder::new()
            .with_webpki_roots()
            .https_or_http()
            .enable_http1()
            .wrap_connector(http);
        Client::builder(TokioExecutor::new()).build(connector)
    })
}

/// `(scheme, host, effective port)`: the identity of an origin.
pub type Parts = (String, String, u16);

/// Splits an absolute http(s) URI into the parts an origin comparison needs.
fn parts(uri: &Uri) -> Option<Parts> {
    let scheme = uri.scheme_str()?.to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return None;
    }
    let authority = uri.authority()?;
    // Userinfo never belongs in an upstream URL or an allowed origin.
    if authority.as_str().contains('@') {
        return None;
    }
    let host = authority.host().to_ascii_lowercase();
    if host.is_empty() || host.bytes().any(|b| b.is_ascii_whitespace()) {
        return None;
    }
    let port = authority
        .port_u16()
        .unwrap_or(if scheme == "https" { 443 } else { 80 });
    Some((scheme, host, port))
}

/// One origin an auth plugin's settings allow `http-fetch` to reach.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Origin {
    scheme: String,
    host: String,
    port: u16,
}

impl Origin {
    /// Parses an allowed origin: absolute `http(s)://host[:port]`, no path (a
    /// bare `/` is accepted), no query, no fragment, no userinfo.
    ///
    /// The error text names the rule, never echoes a credential.
    pub fn parse(text: &str) -> Result<Self, String> {
        let uri: Uri = text
            .parse()
            .map_err(|_| "allowed_origins entries must be absolute origins".to_string())?;
        let (scheme, host, port) = parts(&uri)
            .ok_or_else(|| "allowed_origins entries must be http(s) origins".to_string())?;
        if !matches!(uri.path(), "" | "/") {
            return Err("allowed_origins entries must not have a path".to_string());
        }
        if uri.query().is_some() {
            return Err("allowed_origins entries must not have a query".to_string());
        }
        if text.contains('#') {
            return Err("allowed_origins entries must not have a fragment".to_string());
        }
        Ok(Self { scheme, host, port })
    }

    fn allows(&self, parts: &Parts) -> bool {
        self.scheme == parts.0 && self.host == parts.1 && self.port == parts.2
    }
}

/// Reads the reserved `allowed_origins` setting. Absent means "no fetch at
/// all"; anything malformed refuses the configuration.
pub fn parse_allowed(value: Option<&serde_json::Value>) -> Result<Vec<Origin>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let list = value
        .as_array()
        .ok_or_else(|| "allowed_origins must be an array of origins".to_string())?;
    if list.len() > 32 {
        return Err("allowed_origins has more than 32 entries".to_string());
    }
    let mut origins = Vec::with_capacity(list.len());
    for entry in list {
        let text = entry
            .as_str()
            .ok_or_else(|| "allowed_origins entries must be strings".to_string())?;
        origins.push(Origin::parse(text)?);
    }
    Ok(origins)
}

/// One `http-fetch` request, already handed over by the guest.
#[derive(Debug)]
pub struct RequestSpec {
    pub method: String,
    pub url: String,
    pub headers: Vec<(String, Vec<u8>)>,
    pub body: Vec<u8>,
}

/// One `http-fetch` response, truncated at [`MAX_RESPONSE_BYTES`].
#[derive(Debug)]
pub struct ResponseSpec {
    pub status: u16,
    pub headers: Vec<(String, Vec<u8>)>,
    pub body: Vec<u8>,
}

/// Whether a plugin may set this header on its own HTTP request. Framing and
/// routing headers belong to the client, and request bodies are re-framed.
fn request_header(name: &HeaderName) -> bool {
    !HOP_BY_HOP.iter().any(|hop| *hop == name.as_str())
        && name.as_str() != "host"
        && name.as_str() != "content-length"
}

/// RFC 9110 §7.6.1 plus `proxy-authorization`, which a plugin must not send on
/// its own (this is not the request being authenticated).
pub const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

fn header_pair(name: &str, value: &[u8]) -> Result<(HeaderName, HeaderValue), String> {
    let name = HeaderName::from_bytes(name.as_bytes())
        .map_err(|_| "http-fetch header name is invalid".to_string())?;
    if !request_header(&name) {
        return Err(format!("http-fetch may not set {}", name.as_str()));
    }
    let value = HeaderValue::from_bytes(value)
        .map_err(|_| "http-fetch header value is invalid".to_string())?;
    Ok((name, value))
}

async fn send_inner(request: RequestSpec) -> Result<ResponseSpec, String> {
    if request.body.len() > MAX_REQUEST_BYTES {
        return Err("http-fetch request body exceeds 1 MiB".to_string());
    }
    if request.headers.len() > MAX_HEADERS {
        return Err("http-fetch request has too many headers".to_string());
    }
    let mut header_bytes = 0usize;
    let mut headers = Vec::with_capacity(request.headers.len());
    for (name, value) in &request.headers {
        header_bytes += name.len() + value.len();
        headers.push(header_pair(name, value)?);
    }
    if header_bytes > MAX_HEADER_BYTES {
        return Err("http-fetch request headers exceed 32 KiB".to_string());
    }
    let uri: Uri = request
        .url
        .parse()
        .map_err(|_| "http-fetch url is not absolute".to_string())?;
    let method: hyper::Method = request
        .method
        .parse()
        .map_err(|_| "http-fetch method is invalid".to_string())?;
    let mut builder = Request::builder().method(method).uri(uri);
    for (name, value) in headers {
        builder = builder.header(name, value);
    }
    let request = builder
        .body(Full::new(Bytes::from(request.body)))
        .map_err(|_| "http-fetch request could not be built".to_string())?;
    let response = client()
        .request(request)
        .await
        .map_err(|error| format!("http-fetch failed: {error}"))?;
    let (parts, body) = response.into_parts();
    let mut collected = Vec::new();
    let mut body = body;
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| "http-fetch response body could not be read".to_string())?;
        let Some(data) = frame.data_ref() else {
            continue;
        };
        let room = MAX_RESPONSE_BYTES - collected.len();
        if data.len() > room {
            collected.extend_from_slice(&data[..room]);
            break;
        }
        collected.extend_from_slice(data);
    }
    if parts.headers.len() > MAX_HEADERS {
        return Err("http-fetch response has too many headers".to_string());
    }
    let mut response_headers = Vec::with_capacity(parts.headers.len());
    let mut header_bytes = 0usize;
    for (name, value) in &parts.headers {
        header_bytes += name.as_str().len() + value.as_bytes().len();
        response_headers.push((name.as_str().to_owned(), value.as_bytes().to_vec()));
    }
    if header_bytes > MAX_HEADER_BYTES {
        return Err("http-fetch response headers exceed 32 KiB".to_string());
    }
    Ok(ResponseSpec {
        status: parts.status.as_u16(),
        headers: response_headers,
        body: collected,
    })
}

/// Performs one `http-fetch`.
///
/// There is no time limit here: the fetch is part of the plugin call and ends
/// when that call does (its timeout, or the caller's future being dropped,
/// which cancels the request).
pub async fn send(request: RequestSpec) -> Result<ResponseSpec, String> {
    send_inner(request).await
}

/// A `http-fetch` request for one allowlisted origin: the check that stands
/// between a plugin and the network.
pub fn check_origin(origins: &[Origin], url: &str) -> Result<Uri, String> {
    let uri: Uri = url
        .parse()
        .map_err(|_| "http-fetch url is not absolute".to_string())?;
    let parts = parts(&uri).ok_or_else(|| "http-fetch url is not an http(s) url".to_string())?;
    if !origins.iter().any(|origin| origin.allows(&parts)) {
        return Err("http-fetch origin is not in allowed_origins".to_string());
    }
    Ok(uri)
}

/// Header names a plugin cannot smuggle past the gateway: the RFC hop-by-hop
/// headers and anything nominated by its own `Connection` header. Used for the
/// headers an auth plugin returns for the upstream request.
pub fn hop_by_hop(headers: &[(String, Vec<u8>)]) -> HashSet<String> {
    let mut set: HashSet<String> = HOP_BY_HOP.iter().map(|name| (*name).to_owned()).collect();
    for (name, value) in headers {
        if !name.eq_ignore_ascii_case("connection") {
            continue;
        }
        // `Connection: x-foo, close` nominates `x-foo`.
        for nominated in String::from_utf8_lossy(value).split(',') {
            let nominated = nominated.trim().to_ascii_lowercase();
            if !nominated.is_empty() && nominated != "close" {
                set.insert(nominated);
            }
        }
    }
    set
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins_are_exact_scheme_host_and_port() {
        let origins = vec![Origin::parse("https://auth.example.test").unwrap()];
        assert!(check_origin(&origins, "https://auth.example.test/token").is_ok());
        // Scheme and host compare case-insensitively; ports do not.
        assert!(check_origin(&origins, "https://AUTH.example.test/token").is_ok());
        assert!(check_origin(&origins, "http://auth.example.test/token").is_err());
        assert!(check_origin(&origins, "https://auth.example.test:8443/token").is_err());
        assert!(check_origin(&origins, "https://auth.example.test.evil/token").is_err());
        assert!(check_origin(&origins, "https://evil.example/auth.example.test").is_err());
        let explicit = vec![Origin::parse("http://127.0.0.1:8080").unwrap()];
        assert!(check_origin(&explicit, "http://127.0.0.1:8080/token").is_ok());
        assert!(Origin::parse("http://127.0.0.1:8080/").is_ok());
    }

    #[test]
    fn bad_origins_are_refused() {
        for bad in [
            "",
            "auth.example.test",
            "ftp://auth.example.test",
            "https://user:pw@auth.example.test",
            "https://auth.example.test/path",
            "https://auth.example.test?x=1",
            "https://auth.example.test#frag",
            "https://",
        ] {
            assert!(Origin::parse(bad).is_err(), "{bad:?}");
        }
        assert!(parse_allowed(None).unwrap().is_empty());
        assert!(parse_allowed(Some(&serde_json::json!({}))).is_err());
        assert!(parse_allowed(Some(&serde_json::json!([1]))).is_err());
        assert!(
            parse_allowed(Some(&serde_json::json!(["https://a.test"])))
                .unwrap()
                .len()
                == 1
        );
    }

    #[test]
    fn non_allowlisted_origins_fail_before_any_connection() {
        let origins = vec![Origin::parse("https://auth.example.test").unwrap()];
        let error = check_origin(&origins, "http://127.0.0.1:9/token").unwrap_err();
        assert!(error.contains("not in allowed_origins"), "{error}");
        assert!(check_origin(&origins, "/token").is_err());
        assert!(check_origin(&origins, "ftp://auth.example.test/token").is_err());
        assert!(check_origin(&origins, "https://auth.example.test/token").is_ok());
    }

    #[test]
    fn framing_headers_are_refused_on_fetch_requests() {
        let hop = hop_by_hop(&[("connection".into(), b"x-nominated".to_vec())]);
        for bad in ["host", "content-length", "connection", "transfer-encoding"] {
            let name = bad.parse::<HeaderName>().unwrap();
            assert!(!request_header(&name), "{bad}");
        }
        assert!(request_header(&HeaderName::from_static("authorization")));
        assert!(hop.contains("x-nominated"));
        assert!(header_pair("authorization", b"Bearer x").is_ok());
        assert!(header_pair("x-bad\nname", b"v").is_err());
        assert!(header_pair("x", b"a\nb").is_err());
    }
}

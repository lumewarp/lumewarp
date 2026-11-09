//! Bounded, strict multipart scan. File contents are never decoded or copied.
use bytes::Bytes;
use memchr::memmem;
use std::ops::Range;

const INVALID: &str = "Invalid multipart request";
const MAX_PARTS: usize = 256;
const MAX_FAILED_DELIMITERS: usize = 256;

// Split only outside quotes; reject escapes rather than risk divergent decoding.
fn parameters(value: &str) -> Result<Vec<&str>, &'static str> {
    let mut quoted = false;
    let mut start = 0;
    let mut result = Vec::new();
    for (i, byte) in value.bytes().enumerate() {
        match byte {
            b'\\' | b'\r' | b'\n' => return Err(INVALID),
            b'"' => quoted = !quoted,
            b';' if !quoted => {
                result.push(value[start..i].trim());
                start = i + 1;
            }
            _ => {}
        }
    }
    if quoted {
        return Err(INVALID);
    }
    result.push(value[start..].trim());
    Ok(result)
}

fn parameter(value: &str) -> Result<(&str, &str), &'static str> {
    let (key, value) = value.split_once('=').ok_or(INVALID)?;
    let value = value.trim();
    let value = if value.starts_with('"') {
        value
            .strip_prefix('"')
            .and_then(|s| s.strip_suffix('"'))
            .ok_or(INVALID)?
    } else {
        if value.contains('"') {
            return Err(INVALID);
        }
        value
    };
    if value.contains('"') || value.bytes().any(|b| b.is_ascii_control()) {
        return Err(INVALID);
    }
    let key = key.trim();
    if key.is_empty()
        || !key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-*".contains(&b))
    {
        return Err(INVALID);
    }
    Ok((key, value))
}

// RFC 2046 transport padding belongs to the delimiter, not the preceding value.
fn delimiter_tail(body: &[u8], mut at: usize) -> Option<(usize, bool)> {
    let closing = body.get(at..at + 2) == Some(b"--");
    if closing {
        at += 2;
    }
    while matches!(body.get(at), Some(b' ' | b'\t')) {
        at += 1;
    }
    if closing && at == body.len() {
        return Some((at, true));
    }
    (body.get(at..at + 2) == Some(b"\r\n")).then_some((at + 2, closing))
}

pub struct Fields<'a> {
    pub model: &'a str,
    pub stream: bool,
    pub content_type: String,
    span: Range<usize>,
}

impl<'a> Fields<'a> {
    pub fn from_headers(body: &'a [u8], headers: &hyper::HeaderMap) -> Result<Self, &'static str> {
        let mut values = headers.get_all(hyper::header::CONTENT_TYPE).iter();
        let value = values.next().and_then(|v| v.to_str().ok()).ok_or(INVALID)?;
        if values.next().is_some() {
            return Err("Duplicate Content-Type header");
        }
        Self::parse(body, value)
    }

    pub fn parse(body: &'a [u8], content_type: &str) -> Result<Self, &'static str> {
        let params = parameters(content_type)?;
        if !params[0].eq_ignore_ascii_case("multipart/form-data") {
            return Err(INVALID);
        }
        let mut boundary = None;
        for p in &params[1..] {
            let (key, value) = parameter(p)?;
            if key.eq_ignore_ascii_case("boundary") && boundary.replace(value).is_some() {
                return Err("Duplicate multipart boundary");
            }
        }
        let boundary = boundary
            .filter(|b| {
                !b.is_empty()
                    && b.len() <= 70
                    && !b.ends_with(' ')
                    && b.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"'()+_,-./:=? ".contains(&c))
            })
            .ok_or("Missing or invalid multipart boundary")?;
        let marker = format!("--{boundary}");
        if !body.starts_with(marker.as_bytes()) {
            return Err(INVALID);
        }
        let delimiter = format!("\r\n{marker}");
        let finder = memmem::Finder::new(delimiter.as_bytes());
        let mut failed_delimiters = 0;
        let mut at = marker.len();
        let mut model = None;
        let mut stream = None;
        let mut count = 0;
        loop {
            let (header_start, closing) = delimiter_tail(body, at).ok_or(INVALID)?;
            if closing {
                if header_start != body.len() {
                    return Err("Multipart epilogue is not allowed");
                }
                break;
            }
            count += 1;
            if count > MAX_PARTS {
                return Err("Multipart exceeds 256 parts");
            }
            // Find the next delimiter before parsing headers: malformed headers must
            // never consume a subsequent part's disposition.
            let mut search = header_start;
            let end = loop {
                let end = finder
                    .find(&body[search..])
                    .map(|n| n + search)
                    .ok_or(INVALID)?;
                let tail = end + delimiter.len();
                if delimiter_tail(body, tail).is_some() {
                    break end;
                }
                failed_delimiters += 1;
                if failed_delimiters > MAX_FAILED_DELIMITERS {
                    return Err("Multipart exceeds 256 invalid delimiter candidates");
                }
                search = tail;
            };
            let header_end = memmem::find(&body[header_start..end], b"\r\n\r\n")
                .map(|n| n + header_start)
                .ok_or(INVALID)?;
            if header_end == header_start {
                return Err(INVALID);
            }
            if header_end - header_start > 16 * 1024 {
                return Err("Multipart headers exceed 16 KiB");
            }
            let headers =
                std::str::from_utf8(&body[header_start..header_end]).map_err(|_| INVALID)?;
            let mut disposition = None;
            for line in headers.split("\r\n") {
                let (key, value) = line.split_once(':').ok_or(INVALID)?;
                if key.is_empty() || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
                    return Err(INVALID);
                }
                if key.eq_ignore_ascii_case("content-disposition")
                    && disposition.replace(value).is_some()
                {
                    return Err("Duplicate Content-Disposition header");
                }
            }
            let params = parameters(disposition.ok_or(INVALID)?)?;
            if !params[0].eq_ignore_ascii_case("form-data") {
                return Err(INVALID);
            }
            let mut name = None;
            let mut filename = false;
            for p in &params[1..] {
                let (key, value) = parameter(p)?;
                if key.eq_ignore_ascii_case("name*") {
                    return Err(INVALID);
                }
                if key.eq_ignore_ascii_case("name") && name.replace(value).is_some() {
                    return Err(INVALID);
                }
                filename |=
                    key.eq_ignore_ascii_case("filename") || key.eq_ignore_ascii_case("filename*");
            }
            let name = name.ok_or(INVALID)?;
            let start = header_end + 4;
            if name.eq_ignore_ascii_case("model") {
                if filename || model.is_some() {
                    return Err("Invalid or duplicate model field");
                }
                let text = std::str::from_utf8(&body[start..end]).map_err(|_| INVALID)?;
                if text.is_empty() {
                    return Err("Missing model field");
                }
                model = Some((text, start..end));
            } else if name.eq_ignore_ascii_case("stream") {
                if filename || stream.is_some() {
                    return Err("Invalid or duplicate stream field");
                }
                stream = Some(body[start..end].eq_ignore_ascii_case(b"true"));
            }
            at = end + delimiter.len();
        }
        let (model, span) = model.ok_or("Missing model field")?;
        Ok(Self {
            model,
            stream: stream.unwrap_or(false),
            content_type: format!("multipart/form-data; boundary=\"{boundary}\""),
            span,
        })
    }

    pub fn rewrite(&self, body: &Bytes, model: &str) -> crate::request_body::RequestBody {
        crate::request_body::RequestBody::spliced(
            body.slice(..self.span.start),
            Bytes::copy_from_slice(model.as_bytes()),
            body.slice(self.span.end..),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const CT: &str = "multipart/form-data; boundary=b";
    fn part(params: &str, value: &str) -> String {
        format!("--b\r\nContent-Disposition: form-data; {params}\r\n\r\n{value}\r\n")
    }
    fn rejects_extra(extra: &str) {
        let body = format!("{}{extra}--b--\r\n", part("name=model", "granted"));
        assert!(Fields::parse(body.as_bytes(), CT).is_err());
    }
    #[test]
    fn rejects_case_insensitive_duplicate_model_and_stream() {
        rejects_extra(&part("NAME=\"model\"", "FORBIDDEN"));
        rejects_extra(&format!(
            "{}{}",
            part("name=stream", "false"),
            part("NAME=STREAM", "true")
        ));
    }
    #[test]
    fn rejects_duplicate_boundary_and_preamble() {
        let body = format!("{}--b--\r\n", part("name=model", "granted"));
        assert!(Fields::parse(
            body.as_bytes(),
            "multipart/form-data; boundary=a; BOUNDARY=b"
        )
        .is_err());
        assert!(Fields::parse(format!("preamble\r\n{body}").as_bytes(), CT).is_err());
    }
    #[test]
    fn quoted_semicolon_cannot_hide_model_name() {
        rejects_extra(&part("filename=\"; name=x\"; name=\"model\"", "FORBIDDEN"));
    }
    #[test]
    fn rejects_empty_headers() {
        rejects_extra("--b\r\n\r\n\r\n");
    }
    #[test]
    fn rejects_headers_spanning_delimiter() {
        rejects_extra("--b\r\nX: broken\r\n");
    }
    #[test]
    fn rejects_extended_name() {
        rejects_extra(&part("name*=utf-8''model", "FORBIDDEN"));
    }
    #[test]
    fn transport_padding_is_a_delimiter() {
        rejects_extra(&part("name=model", "FORBIDDEN").replacen("--b\r\n", "--b \t\r\n", 1));
        let body = format!(
            "{}--b-- \t\r\n",
            part("name=model", "granted").replacen("--b\r\n", "--b \t\r\n", 1)
        );
        assert_eq!(Fields::parse(body.as_bytes(), CT).unwrap().model, "granted");
    }
    #[test]
    fn rejects_duplicate_disposition() {
        rejects_extra("--b\r\nContent-Disposition: form-data; name=x\r\nContent-Disposition: form-data; name=model\r\n\r\nFORBIDDEN\r\n");
    }
    #[test]
    fn rejects_duplicate_content_type() {
        let mut headers = hyper::HeaderMap::new();
        headers.append(hyper::header::CONTENT_TYPE, CT.parse().unwrap());
        headers.append(hyper::header::CONTENT_TYPE, CT.parse().unwrap());
        assert!(Fields::from_headers(b"", &headers).is_err());
    }
    #[test]
    fn caps_parts_at_256() {
        let body = format!(
            "{}{}--b--\r\n",
            part("name=model", "granted"),
            part("name=x", "x").repeat(255)
        );
        assert!(Fields::parse(body.as_bytes(), CT).is_ok());
        let body = format!(
            "{}{}--b--\r\n",
            part("name=model", "granted"),
            part("name=x", "x").repeat(256)
        );
        assert!(Fields::parse(body.as_bytes(), CT).is_err());
    }
    #[test]
    fn caps_failed_delimiter_candidates_across_parts() {
        // Count candidates rather than wall time: the shared request-wide budget
        // permits exactly 256 near-delimiters, even across different file parts.
        let upload = |first, second| {
            format!(
                "{}{}{}--b--\r\n",
                part("name=file; filename=a.wav", &"\r\n--bX".repeat(first)),
                part("name=file2; filename=b.wav", &"\r\n--bX".repeat(second)),
                part("name=model", "granted")
            )
        };
        let body = upload(MAX_FAILED_DELIMITERS / 2, MAX_FAILED_DELIMITERS / 2);
        assert!(Fields::parse(body.as_bytes(), CT).is_ok());
        for count in [MAX_FAILED_DELIMITERS / 2 + 1, 1_000_000] {
            let body = upload(MAX_FAILED_DELIMITERS / 2, count);
            assert_eq!(
                Fields::parse(body.as_bytes(), CT).err(),
                Some("Multipart exceeds 256 invalid delimiter candidates")
            );
        }
    }

    #[test]
    fn rejects_nonempty_epilogue() {
        let bare = format!("{}--b--", part("name=model", "granted"));
        assert!(Fields::parse(bare.as_bytes(), CT).is_ok());
        for closing in ["--b--\r\n", "--b-- \t\r\n"] {
            let body = format!("{}{closing}", part("name=model", "granted"));
            assert!(Fields::parse(body.as_bytes(), CT).is_ok());
            for epilogue in [
                "noise",
                "\r\n",
                "--b\r\nContent-Disposition: form-data; name=model\r\n\r\nFORBIDDEN\r\n--b--\r\n",
            ] {
                let body = format!("{body}{epilogue}");
                assert!(Fields::parse(body.as_bytes(), CT).is_err());
            }
        }
    }

    #[tokio::test]
    async fn rewrites_only_model_and_canonicalizes_content_type() {
        use http_body_util::BodyExt;
        let body = Bytes::from(format!(
            "{}{}--b--\r\n",
            part("name=file; filename=\"a.wav\"", "abc\r\n--bX\r\nxyz"),
            part("name=model", "public")
        ));
        let fields =
            Fields::parse(&body, "multipart/form-data; boundary=\"b\"; charset=utf-8").unwrap();
        assert_eq!(fields.content_type, "multipart/form-data; boundary=\"b\"");
        let rewritten = fields
            .rewrite(&body, "upstream")
            .collect()
            .await
            .unwrap()
            .to_bytes();
        assert_eq!(
            rewritten,
            String::from_utf8_lossy(&body).replace("public", "upstream")
        );
    }
}

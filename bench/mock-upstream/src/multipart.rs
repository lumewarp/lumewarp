//! Minimal `multipart/form-data` reader for the transcription endpoints.
//!
//! Only the text fields (`model`, `stream`, `response_format`) and the presence
//! of a `file` part matter, so the reader scans the buffered body for part
//! headers and borrows field values instead of copying file bytes.

/// The boundary from a `multipart/form-data` content type, unquoted.
pub fn boundary(content_type: &str) -> Option<String> {
    let lower = content_type.to_ascii_lowercase();
    if !lower.starts_with("multipart/form-data") {
        return None;
    }
    let start = lower.find("boundary=")? + "boundary=".len();
    let rest = content_type[start..].trim_start();
    let value = match rest.strip_prefix('"') {
        Some(quoted) => &quoted[..quoted.find('"')?],
        None => {
            let end = rest.find(';').unwrap_or(rest.len());
            rest[..end].trim_end()
        }
    };
    if value.is_empty() || value.len() > 200 || value.contains('\r') || value.contains('\n') {
        return None;
    }
    Some(value.to_owned())
}

/// One part of a multipart body; `body` is the raw, untrimmed content.
#[derive(Debug, PartialEq, Eq)]
pub struct Part<'a> {
    pub name: &'a str,
    pub filename: Option<&'a str>,
    pub body: &'a [u8],
}

impl Part<'_> {
    /// UTF-8 text of a value part, trimmed; empty for binary or invalid UTF-8.
    pub fn text(&self) -> &str {
        std::str::from_utf8(self.body)
            .map(str::trim)
            .unwrap_or_default()
    }
}

/// Every well-formed part between the boundary delimiters, in body order.
pub fn parts<'a>(body: &'a [u8], boundary: &str) -> Vec<Part<'a>> {
    let delimiter = format!("--{boundary}");
    let separator = format!("\r\n--{boundary}");
    let mut parts = Vec::new();
    let Some(mut position) = find(body, delimiter.as_bytes(), 0) else {
        return parts;
    };
    loop {
        let mut cursor = position + delimiter.len();
        if body[cursor..].starts_with(b"--") {
            break;
        }
        while matches!(body.get(cursor), Some(b' ' | b'\t')) {
            cursor += 1;
        }
        if !body[cursor..].starts_with(b"\r\n") {
            break;
        }
        cursor += 2;
        let Some(headers_end) = find(body, b"\r\n\r\n", cursor) else {
            break;
        };
        let Some(next) = find(body, separator.as_bytes(), headers_end + 4) else {
            break;
        };
        if let Some(part) = parse_part(&body[cursor..headers_end], &body[headers_end + 4..next]) {
            parts.push(part);
        }
        position = next + 2;
    }
    parts
}

fn parse_part<'a>(headers: &'a [u8], body: &'a [u8]) -> Option<Part<'a>> {
    let mut name = None;
    let mut filename = None;
    for line in headers.split(|byte| *byte == b'\n') {
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        let Ok(line) = std::str::from_utf8(line) else {
            continue;
        };
        let Some((header, value)) = line.split_once(':') else {
            continue;
        };
        if !header.trim().eq_ignore_ascii_case("content-disposition") {
            continue;
        }
        name = param(value, "name");
        filename = param(value, "filename");
    }
    Some(Part {
        name: name?,
        filename,
        body,
    })
}

/// `name="model"` or `name=model` from a `Content-Disposition` value. The
/// parameter must start after `;`/whitespace so `filename` never matches `name`.
fn param<'a>(value: &'a str, key: &str) -> Option<&'a str> {
    let lower = value.to_ascii_lowercase();
    let mut search = 0;
    while let Some(index) = lower[search..].find(key) {
        let start = search + index;
        let end = start + key.len();
        let before = lower[..start].chars().next_back();
        if before.is_some_and(|c| c != ';' && !c.is_whitespace()) || !lower[end..].starts_with('=')
        {
            search = end;
            continue;
        }
        let rest = value[end + 1..].trim_start();
        return Some(match rest.strip_prefix('"') {
            Some(quoted) => &quoted[..quoted.find('"').unwrap_or(quoted.len())],
            None => rest[..rest.find(';').unwrap_or(rest.len())].trim_end(),
        });
    }
    None
}

fn find(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from > haystack.len() {
        return None;
    }
    haystack[from..]
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|index| index + from)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(boundary: &str, parts: &[(&str, &[u8])]) -> Vec<u8> {
        let mut body = Vec::new();
        for (headers, content) in parts {
            body.extend_from_slice(format!("--{boundary}\r\n{headers}\r\n\r\n").as_bytes());
            body.extend_from_slice(content);
            body.extend_from_slice(b"\r\n");
        }
        body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
        body
    }

    #[test]
    fn boundary_forms() {
        assert_eq!(
            boundary("multipart/form-data; boundary=abc").as_deref(),
            Some("abc")
        );
        assert_eq!(
            boundary("multipart/form-data; boundary=\"quoted-boundary\"").as_deref(),
            Some("quoted-boundary")
        );
        assert_eq!(
            boundary("Multipart/Form-Data; charset=utf-8; boundary=x; y=1").as_deref(),
            Some("x")
        );
        assert_eq!(boundary("multipart/form-data"), None);
        assert_eq!(boundary("application/json; boundary=x"), None);
        assert_eq!(boundary("multipart/form-data; boundary="), None);
        assert_eq!(
            boundary("multipart/form-data; boundary=\"unterminated"),
            None
        );
    }

    #[test]
    fn parts_keep_order_and_parse_disposition() {
        let raw = body(
            "XyZ",
            &[
                (
                    "Content-Disposition: form-data; name=\"model\"",
                    b"whisper-1",
                ),
                ("Content-Disposition: form-data; name=stream", b" true "),
                (
                    "Content-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav",
                    b"\x00\x01binary",
                ),
            ],
        );
        let parts = parts(&raw, "XyZ");
        assert_eq!(parts.len(), 3);
        assert_eq!(parts[0].name, "model");
        assert_eq!(parts[0].text(), "whisper-1");
        assert_eq!(parts[0].filename, None);
        assert_eq!(parts[1].name, "stream");
        assert_eq!(parts[1].text(), "true");
        assert_eq!(parts[2].name, "file");
        assert_eq!(parts[2].filename, Some("a.wav"));
        assert_eq!(parts[2].body, b"\x00\x01binary");
    }

    #[test]
    fn model_after_file_preamble_and_epilogue() {
        let mut raw = b"preamble\r\n".to_vec();
        raw.extend(body(
            "b",
            &[
                (
                    "Content-Disposition: form-data; name=\"file\"; filename=\"a.wav\"",
                    b"\xff",
                ),
                (
                    "content-disposition: form-data; name=\"model\"",
                    b"audio-tokens",
                ),
            ],
        ));
        raw.extend_from_slice(b"epilogue");
        let parts = parts(&raw, "b");
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[1].name, "model");
        assert_eq!(parts[1].text(), "audio-tokens");
    }

    #[test]
    fn missing_boundary_delimiter_yields_no_parts() {
        assert!(parts(b"no parts here", "b").is_empty());
        assert!(parts(b"--b\r\n", "b").is_empty());
    }

    #[test]
    fn filename_does_not_match_name_parameter() {
        let raw = body(
            "b",
            &[(
                "Content-Disposition: form-data; filename=\"name=x\"; name=\"model\"",
                b"mock",
            )],
        );
        let parts = parts(&raw, "b");
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0].name, "model");
        assert_eq!(parts[0].filename, Some("name=x"));
    }

    #[test]
    fn invalid_utf8_value_part_is_empty() {
        let raw = body(
            "b",
            &[("Content-Disposition: form-data; name=\"model\"", b"\xff")],
        );
        assert_eq!(parts(&raw, "b")[0].text(), "");
    }
}

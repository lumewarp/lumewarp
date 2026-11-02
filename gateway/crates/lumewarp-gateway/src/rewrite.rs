//! Rewrites the top-level `model` of a response to the name the client called,
//! without buffering: every byte is forwarded as soon as its frame is scanned,
//! and frames are cut (reference counted, no copy) around the replaced value.
//!
//! The scanner follows the structure of the top-level object byte by byte and
//! keeps only a fixed-size state, so any split between frames gives the same
//! output. It is not a JSON validator. What it decides:
//!
//! - Valid JSON gets what a parser would see: every top-level key equal to
//!   `model` after unescaping (`"m\u006fdel"` counts) with a string value has
//!   that value replaced; nothing else changes, whitespace and order included.
//! - A duplicate top-level `model`: all of them are replaced, so a parser that
//!   keeps the first and one that keeps the last both see the called name.
//! - A non-string `model` value is left alone, and an absent `model` is not
//!   added: neither is a name to replace.
//! - Invalid JSON: structure that breaks the top-level object (a body that does
//!   not start with `{`, a bad separator, mismatched brackets, nesting deeper
//!   than 128) forwards everything from that byte on unchanged. Earlier `model`
//!   values were already sent as replaced. Scalars, commas inside nested values
//!   and string contents are not checked, so an error there may leave a later
//!   `model` replaced in a body that was invalid anyway. A `model` string cut
//!   by EOF is forwarded as the complete called name; a control byte inside it
//!   stops the replacement and forwards from that byte. The connection is never
//!   failed because of the content.
//!
//! SSE: each event's `data` lines are one JSON document, joined by a newline as
//! the SSE format says, and the scanner restarts at each blank line. Comments
//! and other fields pass through, and may sit between `data` lines. CR, LF and
//! CRLF all end a line. An event whose data is not an object (`[DONE]`) and an
//! event with no data pass through unchanged. One byte order mark at the very
//! start of the body is skipped when reading fields and is forwarded; anywhere
//! later it is part of an unknown field, as the format says.
//!
//! A JSON body starting with a byte order mark is not an object to the scanner
//! and is forwarded unchanged: JSON producers must not send one.

use std::{
    pin::Pin,
    task::{ready, Context, Poll},
};

use bytes::{Buf, Bytes};
use hyper::{
    body::{Body, Frame},
    HeaderMap,
};

const MODEL: &[u8; 5] = b"model";
/// `key_at` once the current key can no longer be `model`.
const NOT_MODEL: u8 = u8::MAX;
/// Levels of nesting below the top-level object tracked in `containers`.
const MAX_NESTING: u8 = 128;
const BOM: &[u8; 3] = b"\xef\xbb\xbf";

/// Fields that describe the exact bytes of the upstream's body. A rewrite makes
/// them false, and the new value is not known until the body has ended.
const REPRESENTATION_FIELDS: [&str; 5] = [
    "content-md5",
    "digest",
    "content-digest",
    "repr-digest",
    "etag",
];

/// Removes `REPRESENTATION_FIELDS`, from headers or trailers of a rewritten body.
pub fn strip_representation_fields(fields: &mut HeaderMap) {
    for name in REPRESENTATION_FIELDS {
        fields.remove(name);
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    /// Before the top-level `{`.
    Start,
    /// After `{`: a key or `}`.
    FirstKey,
    /// After `,`: a key.
    NextKey,
    Key,
    /// After a key: `:`.
    AfterKey,
    /// After `:`: a value.
    Value,
    /// After a value: `,` or `}`.
    AfterValue,
    /// Inside a top-level string value that stays.
    String,
    /// Inside a top-level number or literal.
    Scalar,
    /// Inside a top-level object or array value.
    Nested,
    NestedString,
    /// Dropping the replaced value, up to its closing quote.
    Replacing,
    /// Forward everything unchanged: invalid, or the object has ended.
    Pass,
}

/// Fixed-size scanner of one JSON document.
struct JsonScanner {
    state: State,
    /// The previous string byte was a backslash.
    escaped: bool,
    /// One bit per nesting level below the top: set for an array.
    containers: u128,
    nesting: u8,
    /// Bytes of `model` matched by the current key, or `NOT_MODEL`.
    key_at: u8,
    /// Inside a key escape: 0 none, 1 after the backslash, 2..=5 before the
    /// 1st..=4th hex digit of `\uXXXX`.
    key_escape: u8,
    key_code: u16,
    /// The last complete key was `model`.
    is_model: bool,
}

const fn repeat(byte: u8) -> u64 {
    u64::from_ne_bytes([byte; 8])
}

/// Whether any byte of `word` equals `byte` (the classic zero-byte test).
const fn has_byte(word: u64, byte: u8) -> bool {
    let x = word ^ repeat(byte);
    x.wrapping_sub(repeat(1)) & !x & repeat(0x80) != 0
}

/// Whether `word` may hold a quote or bracket. `| 0x20` folds `[` onto `{` and
/// `]` onto `}`. Number arrays, the bulk of an embeddings body, have none.
const fn has_structural(word: u64) -> bool {
    let folded = word | repeat(0x20);
    has_byte(word, b'"') || has_byte(folded, b'{') || has_byte(folded, b'}')
}

const fn whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

const fn hex(byte: u8) -> Option<u16> {
    match byte {
        b'0'..=b'9' => Some((byte - b'0') as u16),
        b'a'..=b'f' => Some((byte - b'a') as u16 + 10),
        b'A'..=b'F' => Some((byte - b'A') as u16 + 10),
        _ => None,
    }
}

impl JsonScanner {
    const fn new() -> Self {
        Self {
            state: State::Start,
            escaped: false,
            containers: 0,
            nesting: 0,
            key_at: 0,
            key_escape: 0,
            key_code: 0,
            is_model: false,
        }
    }

    fn begin_key(&mut self) {
        self.state = State::Key;
        self.key_at = 0;
        self.key_escape = 0;
        self.escaped = false;
        self.is_model = false;
    }

    /// Scans until the opening quote of a `model` value and returns its offset,
    /// with the state set to `Replacing`. `None`: all of `bytes` was scanned.
    fn scan(&mut self, bytes: &[u8]) -> Option<usize> {
        let mut at = 0;
        while let Some(&byte) = bytes.get(at) {
            match self.state {
                State::Pass | State::Replacing => return None,
                State::Key => {
                    at += self.key(&bytes[at..]);
                    continue;
                }
                State::String | State::NestedString => {
                    at += self.skip_string(&bytes[at..]);
                    continue;
                }
                State::Nested => {
                    at += self.skip_nested(&bytes[at..]);
                    continue;
                }
                State::Scalar => {
                    match byte {
                        b',' | b'}' | b' ' | b'\t' | b'\n' | b'\r' => {
                            self.state = State::AfterValue;
                            continue;
                        }
                        b'"' | b'{' | b'[' | b']' | b':' => self.state = State::Pass,
                        _ => {}
                    }
                    at += 1;
                    continue;
                }
                _ if whitespace(byte) => {
                    at += 1;
                    continue;
                }
                State::Start => {
                    self.state = if byte == b'{' {
                        State::FirstKey
                    } else {
                        State::Pass
                    };
                }
                // After `{` a `}` also ends here: an empty object has no `model`.
                State::FirstKey | State::NextKey => {
                    if byte == b'"' {
                        self.begin_key();
                    } else {
                        self.state = State::Pass;
                    }
                }
                State::AfterKey => {
                    self.state = if byte == b':' {
                        State::Value
                    } else {
                        State::Pass
                    };
                }
                State::Value => match byte {
                    b'"' if self.is_model => {
                        self.state = State::Replacing;
                        self.escaped = false;
                        return Some(at);
                    }
                    b'"' => {
                        self.state = State::String;
                        self.escaped = false;
                    }
                    b'{' | b'[' => {
                        self.state = State::Nested;
                        self.nesting = 1;
                        self.containers = u128::from(byte == b'[');
                    }
                    b'-' | b'0'..=b'9' | b't' | b'f' | b'n' => self.state = State::Scalar,
                    _ => self.state = State::Pass,
                },
                State::AfterValue => {
                    self.state = if byte == b',' {
                        State::NextKey
                    } else {
                        // `}` ends the object: what follows is not rewritten.
                        State::Pass
                    };
                }
            }
            at += 1;
        }
        None
    }

    /// Consumes key bytes, comparing them with `model` after unescaping.
    fn key(&mut self, bytes: &[u8]) -> usize {
        let mut at = 0;
        while let Some(&byte) = bytes.get(at) {
            if self.key_at == NOT_MODEL {
                return at + self.skip_string(&bytes[at..]);
            }
            at += 1;
            match self.key_escape {
                0 => match byte {
                    b'"' => {
                        self.is_model = usize::from(self.key_at) == MODEL.len();
                        self.state = State::AfterKey;
                        return at;
                    }
                    b'\\' => self.key_escape = 1,
                    _ => self.key_char(u16::from(byte)),
                },
                1 if byte == b'u' => {
                    self.key_escape = 2;
                    self.key_code = 0;
                }
                1 => {
                    // No short escape spells a letter of `model`. The escaped
                    // byte is consumed here, so `\"` does not close the key.
                    self.key_at = NOT_MODEL;
                    self.key_escape = 0;
                }
                digit => match hex(byte) {
                    Some(value) => {
                        self.key_code = self.key_code << 4 | value;
                        if digit == 5 {
                            self.key_escape = 0;
                            self.key_char(self.key_code);
                        } else {
                            self.key_escape = digit + 1;
                        }
                    }
                    None => {
                        // Not an escape after all: the byte is ordinary again.
                        self.key_at = NOT_MODEL;
                        self.key_escape = 0;
                        at -= 1;
                    }
                },
            }
        }
        at
    }

    fn key_char(&mut self, unit: u16) {
        match MODEL.get(usize::from(self.key_at)) {
            Some(&expected) if unit == u16::from(expected) => self.key_at += 1,
            _ => self.key_at = NOT_MODEL,
        }
    }

    /// Consumes string bytes up to and including the closing quote.
    fn skip_string(&mut self, bytes: &[u8]) -> usize {
        let mut at = 0;
        if self.escaped && !bytes.is_empty() {
            self.escaped = false;
            at = 1;
        }
        while let Some(found) = memchr::memchr2(b'"', b'\\', &bytes[at..]) {
            let index = at + found;
            if bytes[index] == b'"' {
                self.state = match self.state {
                    State::Key => State::AfterKey,
                    State::NestedString => State::Nested,
                    _ => State::AfterValue,
                };
                return index + 1;
            }
            if index + 1 == bytes.len() {
                self.escaped = true;
                return bytes.len();
            }
            at = index + 2;
        }
        bytes.len()
    }

    /// Consumes a nested value, stopping after its end or at a string.
    fn skip_nested(&mut self, bytes: &[u8]) -> usize {
        let mut at = 0;
        loop {
            while let Some(word) = bytes.get(at..at + 8) {
                let word = u64::from_ne_bytes(word.try_into().expect("eight bytes"));
                if has_structural(word) {
                    break;
                }
                at += 8;
            }
            let Some(&byte) = bytes.get(at) else {
                return bytes.len();
            };
            at += 1;
            match byte {
                b'"' => {
                    self.state = State::NestedString;
                    self.escaped = false;
                    return at;
                }
                b'{' | b'[' => {
                    if self.nesting == MAX_NESTING {
                        self.state = State::Pass;
                        return at;
                    }
                    if byte == b'[' {
                        self.containers |= 1 << self.nesting;
                    } else {
                        self.containers &= !(1 << self.nesting);
                    }
                    self.nesting += 1;
                }
                b'}' | b']' => {
                    self.nesting -= 1;
                    let array = self.containers & (1 << self.nesting) != 0;
                    if array != (byte == b']') {
                        self.state = State::Pass;
                        return at;
                    }
                    if self.nesting == 0 {
                        self.state = State::AfterValue;
                        return at;
                    }
                }
                _ => {}
            }
        }
    }

    /// In `Replacing`: returns how many bytes of the old value to drop.
    fn drop_value(&mut self, bytes: &[u8]) -> usize {
        for (at, &byte) in bytes.iter().enumerate() {
            if byte < 0x20 {
                // Raw control bytes end a broken value, and an SSE line.
                self.state = State::Pass;
                return at;
            }
            if self.escaped {
                self.escaped = false;
            } else if byte == b'\\' {
                self.escaped = true;
            } else if byte == b'"' {
                self.state = State::AfterValue;
                return at + 1;
            }
        }
        bytes.len()
    }
}

/// Where the SSE scanner is within a line.
#[derive(Clone, Copy)]
enum Line {
    /// At the very start of the body: how many bytes of a BOM it has matched.
    Bom(u8),
    /// In the field name: how many bytes of `data` it has matched; 0 at the
    /// start of a line.
    Name(u8),
    /// A comment or another field.
    Other,
    /// Right after `data:`, where one space is not part of the value.
    ValueStart,
    Value,
}

#[derive(Clone, Copy)]
struct SseLines {
    line: Line,
    /// The last byte ended a line with CR: an LF next belongs to it.
    after_cr: bool,
}

/// The scanner over a response body. `push` a frame, then call `next_piece`
/// until it returns `None`. Allocates nothing per frame; cutting a frame only
/// moves reference counts.
pub struct ModelRewriter {
    /// The called name as a JSON string, quotes and escapes included.
    name: Bytes,
    json: JsonScanner,
    sse: Option<SseLines>,
    /// The part of the current frame not yet scanned.
    rest: Bytes,
    emit_name: bool,
}

impl ModelRewriter {
    pub fn new(name: Bytes, sse: bool) -> Self {
        Self {
            name,
            json: JsonScanner::new(),
            sse: sse.then_some(SseLines {
                line: Line::Bom(0),
                after_cr: false,
            }),
            rest: Bytes::new(),
            emit_name: false,
        }
    }

    /// Takes the next frame; the previous one must have been drained.
    pub fn push(&mut self, frame: Bytes) {
        debug_assert!(!self.has_pending());
        self.rest = frame;
    }

    pub fn has_pending(&self) -> bool {
        self.emit_name || !self.rest.is_empty()
    }

    pub fn next_piece(&mut self) -> Option<Bytes> {
        loop {
            if self.emit_name {
                self.emit_name = false;
                return Some(self.name.clone());
            }
            if self.rest.is_empty() {
                return None;
            }
            if self.json.state == State::Replacing {
                let dropped = self.json.drop_value(&self.rest);
                self.rest.advance(dropped);
                continue;
            }
            let found = match self.sse {
                Some(_) => self.scan_sse(),
                None => self.json.scan(&self.rest),
            };
            let Some(at) = found else {
                return Some(std::mem::take(&mut self.rest));
            };
            let piece = self.rest.split_to(at);
            self.rest.advance(1);
            self.emit_name = true;
            if !piece.is_empty() {
                return Some(piece);
            }
        }
    }

    /// Scans SSE lines; returns what `JsonScanner::scan` would, as an offset
    /// into `rest`.
    fn scan_sse(&mut self) -> Option<usize> {
        let bytes = &self.rest[..];
        let lines = self.sse.as_mut().expect("sse mode");
        let mut at = 0;
        while let Some(&byte) = bytes.get(at) {
            if std::mem::take(&mut lines.after_cr) && byte == b'\n' {
                at += 1;
                continue;
            }
            match lines.line {
                Line::Bom(matched) => {
                    if BOM.get(usize::from(matched)) == Some(&byte) {
                        at += 1;
                        lines.line = if usize::from(matched) + 1 == BOM.len() {
                            Line::Name(0)
                        } else {
                            Line::Bom(matched + 1)
                        };
                    } else if matched == 0 {
                        lines.line = Line::Name(0);
                    } else {
                        // Part of a BOM is the start of an unknown field.
                        lines.line = Line::Other;
                    }
                }
                Line::Name(matched) => {
                    at += 1;
                    match byte {
                        b'\r' | b'\n' => {
                            lines.after_cr = byte == b'\r';
                            lines.line = Line::Name(0);
                            match matched {
                                0 => self.json = JsonScanner::new(),
                                // `data` without a colon is an empty value.
                                4 => {
                                    self.json.scan(b"\n");
                                }
                                _ => {}
                            }
                        }
                        b':' if matched == 4 => lines.line = Line::ValueStart,
                        _ if b"data".get(usize::from(matched)) == Some(&byte) => {
                            lines.line = Line::Name(matched + 1);
                        }
                        _ => lines.line = Line::Other,
                    }
                }
                Line::Other => {
                    at += memchr::memchr2(b'\r', b'\n', &bytes[at..])? + 1;
                    lines.after_cr = bytes[at - 1] == b'\r';
                    lines.line = Line::Name(0);
                }
                Line::ValueStart => {
                    lines.line = Line::Value;
                    at += usize::from(byte == b' ');
                }
                Line::Value => {
                    let found = memchr::memchr2(b'\r', b'\n', &bytes[at..]);
                    let end = found.map_or(bytes.len(), |found| at + found);
                    if let Some(value) = self.json.scan(&bytes[at..end]) {
                        return Some(at + value);
                    }
                    // The line goes on in the next frame.
                    found?;
                    // The newline that joins this line to the next `data` line.
                    self.json.scan(b"\n");
                    lines.after_cr = bytes[end] == b'\r';
                    lines.line = Line::Name(0);
                    at = end + 1;
                }
            }
        }
        None
    }
}

/// `body` with its top-level `model` rewritten. The length changes and is not
/// known up front, so the size hint is left open and the response is chunked.
pub struct RewriteBody<B> {
    body: B,
    rewriter: ModelRewriter,
}

impl<B> RewriteBody<B> {
    pub fn new(body: B, rewriter: ModelRewriter) -> Self {
        Self { body, rewriter }
    }
}

impl<B: Body<Data = Bytes> + Unpin> Body for RewriteBody<B> {
    type Data = Bytes;
    type Error = B::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, B::Error>>> {
        let this = &mut *self;
        loop {
            if let Some(piece) = this.rewriter.next_piece() {
                return Poll::Ready(Some(Ok(Frame::data(piece))));
            }
            match ready!(Pin::new(&mut this.body).poll_frame(cx)) {
                Some(Ok(frame)) => match frame.into_data() {
                    Ok(data) => this.rewriter.push(data),
                    Err(frame) => match frame.into_trailers() {
                        Ok(mut trailers) => {
                            strip_representation_fields(&mut trailers);
                            if !trailers.is_empty() {
                                return Poll::Ready(Some(Ok(Frame::trailers(trailers))));
                            }
                        }
                        Err(frame) => return Poll::Ready(Some(Ok(frame))),
                    },
                },
                end => return Poll::Ready(end),
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        !self.rewriter.has_pending() && self.body.is_end_stream()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::BytesMut;
    use http_body_util::{BodyExt, StreamBody};
    use serde::de::{Deserializer, MapAccess, Visitor};
    use serde_json::value::RawValue;
    use std::{borrow::Cow, fmt};

    const NAME: &str = "called";

    fn name() -> Bytes {
        Bytes::from(serde_json::to_vec(NAME).unwrap())
    }

    fn drain(rewriter: &mut ModelRewriter, frame: &[u8], out: &mut Vec<u8>) {
        rewriter.push(Bytes::copy_from_slice(frame));
        while let Some(piece) = rewriter.next_piece() {
            assert!(!piece.is_empty(), "no empty frames");
            out.extend_from_slice(&piece);
        }
    }

    fn run(input: &[u8], sse: bool, splits: &[usize]) -> Vec<u8> {
        let mut rewriter = ModelRewriter::new(name(), sse);
        let mut out = Vec::new();
        let mut start = 0;
        for &split in splits {
            drain(&mut rewriter, &input[start..split], &mut out);
            start = split;
        }
        drain(&mut rewriter, &input[start..], &mut out);
        out
    }

    fn json(input: &str) -> String {
        String::from_utf8(run(input.as_bytes(), false, &[])).unwrap()
    }

    fn sse(input: &str) -> String {
        String::from_utf8(run(input.as_bytes(), true, &[])).unwrap()
    }

    /// Key spans as a parser sees them: the raw value of every top-level entry
    /// with the key decoded.
    fn top_level_values(input: &str) -> Vec<(String, &str)> {
        struct Entries;
        impl<'de> Visitor<'de> for Entries {
            type Value = Vec<(String, &'de str)>;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("an object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut entries = Vec::new();
                while let Some(key) = map.next_key::<Cow<'de, str>>()? {
                    let value: &'de RawValue = map.next_value()?;
                    entries.push((key.into_owned(), value.get()));
                }
                Ok(entries)
            }
        }
        let mut deserializer = serde_json::Deserializer::from_str(input);
        let entries = (&mut deserializer).deserialize_map(Entries).unwrap();
        deserializer.end().unwrap();
        entries
    }

    /// What the scanner must produce for valid JSON, from serde_json's view of
    /// the same input: string values of `model` keys replaced, nothing else.
    fn reference(input: &str) -> String {
        let quoted = serde_json::to_string(NAME).unwrap();
        if !input.trim_start().starts_with('{') {
            return input.to_owned();
        }
        let mut out = String::with_capacity(input.len());
        let mut at = 0;
        for (key, value) in top_level_values(input) {
            if key == "model" && value.starts_with('"') {
                let start = value.as_ptr() as usize - input.as_ptr() as usize;
                out.push_str(&input[at..start]);
                out.push_str(&quoted);
                at = start + value.len();
            }
        }
        out.push_str(&input[at..]);
        out
    }

    /// A random valid JSON document, biased to the cases the scanner cares about.
    struct Generator {
        rng: fastrand::Rng,
        /// Whitespace may hold newlines: split into `data` lines for SSE.
        newlines: bool,
    }

    impl Generator {
        fn space(&mut self, out: &mut String) {
            if self.rng.u8(0..4) == 0 {
                let choices: &[&str] = if self.newlines {
                    &[" ", "  ", "\t", "\n", " \n "]
                } else {
                    &[" ", "  ", "\t", "\n", "\r\n", " \n "]
                };
                out.push_str(choices[self.rng.usize(0..choices.len())]);
            }
        }

        fn string(&mut self, out: &mut String) {
            const PARTS: &[&str] = &[
                "a",
                "model",
                "\\\"",
                "\\\\",
                "\\n",
                "\\u0041",
                "\\ud83d\\ude00",
                "模型",
                "{",
                "}",
                "[",
                "]",
                ":",
                ",",
                "\\\\\\\"model\\\":",
            ];
            out.push('"');
            for _ in 0..self.rng.usize(0..5) {
                out.push_str(PARTS[self.rng.usize(0..PARTS.len())]);
            }
            out.push('"');
        }

        fn key(&mut self, out: &mut String) {
            const KEYS: &[&str] = &[
                "\"model\"",
                "\"model\"",
                "\"m\\u006fdel\"",
                "\"\\u006Dodel\"",
                "\"\\u006d\\u006F\\u0064\\u0065\\u006c\"",
                "\"Model\"",
                "\"models\"",
                "\"mode\"",
                "\"\"",
                "\"model\\n\"",
                "\"mod\\\"el\"",
                "\"data\"",
                "\"usage\"",
                "\"模型\"",
            ];
            if self.rng.u8(0..3) == 0 {
                self.string(out);
            } else {
                out.push_str(KEYS[self.rng.usize(0..KEYS.len())]);
            }
        }

        fn value(&mut self, depth: u32, out: &mut String) {
            let kind = if depth == 0 {
                self.rng.u8(0..5)
            } else {
                self.rng.u8(0..7)
            };
            match kind {
                0 | 1 => self.string(out),
                2 => out.push_str(["0", "-1.5e+3", "12", "0.125"][self.rng.usize(0..4)]),
                3 => out.push_str(["true", "false"][self.rng.usize(0..2)]),
                4 => out.push_str("null"),
                5 => {
                    out.push('[');
                    let count = self.rng.usize(0..4);
                    for index in 0..count {
                        if index > 0 {
                            out.push(',');
                        }
                        self.space(out);
                        self.value(depth - 1, out);
                        self.space(out);
                    }
                    out.push(']');
                }
                _ => self.object(depth - 1, out),
            }
        }

        fn object(&mut self, depth: u32, out: &mut String) {
            out.push('{');
            self.space(out);
            let count = self.rng.usize(0..5);
            for index in 0..count {
                if index > 0 {
                    out.push(',');
                    self.space(out);
                }
                // Half of the entries: a model key with a string value.
                let replaced = self.rng.bool();
                if replaced {
                    out.push_str("\"model\"");
                } else {
                    self.key(out);
                }
                self.space(out);
                out.push(':');
                self.space(out);
                if replaced {
                    self.string(out);
                } else {
                    self.value(depth, out);
                }
                self.space(out);
            }
            out.push('}');
        }

        fn document(&mut self) -> String {
            let mut out = String::new();
            self.space(&mut out);
            match self.rng.u8(0..12) {
                0 => self.value(2, &mut out),
                _ => self.object(3, &mut out),
            }
            self.space(&mut out);
            out
        }
    }

    fn random_splits(rng: &mut fastrand::Rng, len: usize) -> Vec<usize> {
        let mut splits: Vec<usize> = (0..rng.usize(1..12)).map(|_| rng.usize(0..=len)).collect();
        splits.sort_unstable();
        splits
    }

    #[test]
    fn matches_the_reference_at_every_split_point() {
        let mut generator = Generator {
            rng: fastrand::Rng::with_seed(0x6d6f64656c),
            newlines: false,
        };
        let mut rewritten = 0;
        for _ in 0..3000 {
            let doc = generator.document();
            let expected = reference(&doc);
            rewritten += usize::from(expected != doc);
            assert_eq!(json(&doc), expected, "{doc}");
            for split in 0..=doc.len() {
                assert_eq!(
                    run(doc.as_bytes(), false, &[split]),
                    expected.as_bytes(),
                    "{doc} split at {split}"
                );
            }
            let splits = random_splits(&mut generator.rng, doc.len());
            assert_eq!(
                run(doc.as_bytes(), false, &splits),
                expected.as_bytes(),
                "{doc} split at {splits:?}"
            );
        }
        assert!(rewritten > 500, "generator too weak: {rewritten}");
    }

    #[test]
    fn invalid_input_is_split_invariant_and_never_loses_the_tail() {
        let mut rng = fastrand::Rng::with_seed(7);
        let mut generator = Generator {
            rng: rng.clone(),
            newlines: false,
        };
        for _ in 0..3000 {
            let doc = generator.document();
            // Damage one byte; the document may stay valid, which is fine.
            let mut bytes = doc.into_bytes();
            if bytes.is_empty() {
                continue;
            }
            const JUNK: &[u8] = b"\"\\{}[]:,x\n\0\xff";
            let at = rng.usize(0..bytes.len());
            bytes[at] = JUNK[rng.usize(0..JUNK.len())];
            let whole = run(&bytes, false, &[]);
            for split in 0..=bytes.len() {
                assert_eq!(run(&bytes, false, &[split]), whole);
            }
            let splits = random_splits(&mut rng, bytes.len());
            assert_eq!(run(&bytes, false, &splits), whole);
            assert_eq!(run(&bytes, true, &splits), run(&bytes, true, &[]));
        }
    }

    #[test]
    fn model_positions() {
        for (input, expected) in [
            // At the start, the end, and in the middle.
            (r#"{"model":"up","a":1}"#, r#"{"model":"called","a":1}"#),
            (r#"{"a":1,"model":"up"}"#, r#"{"a":1,"model":"called"}"#),
            (
                r#"{"a":[{"model":"x"}],"model":"up","b":{"model":"y"}}"#,
                r#"{"a":[{"model":"x"}],"model":"called","b":{"model":"y"}}"#,
            ),
            // Whitespace and order kept.
            (
                " {\n \"model\" :\t\"up\" ,\"z\":0.10 }\n",
                " {\n \"model\" :\t\"called\" ,\"z\":0.10 }\n",
            ),
            // Nested only, and inside strings.
            (r#"{"a":{"model":"x"}}"#, r#"{"a":{"model":"x"}}"#),
            (r#"{"a":["model","x"]}"#, r#"{"a":["model","x"]}"#),
            (
                r#"{"a":"\"model\":\"x\"","b":"model"}"#,
                r#"{"a":"\"model\":\"x\"","b":"model"}"#,
            ),
            // A value equal to the key's text is not a key.
            (r#"{"a":"model","b":"up"}"#, r#"{"a":"model","b":"up"}"#),
            // Escapes in the value are dropped with it.
            (r#"{"model":"u\"p\\","a":1}"#, r#"{"model":"called","a":1}"#),
            (r#"{"model":"\u6a21","a":1}"#, r#"{"model":"called","a":1}"#),
            // Escapes in the key.
            (r#"{"m\u006fdel":"up"}"#, r#"{"m\u006fdel":"called"}"#),
            (
                r#"{"\u006D\u006f\u0064\u0065\u006C":"up"}"#,
                r#"{"\u006D\u006f\u0064\u0065\u006C":"called"}"#,
            ),
            // Almost the key.
            (r#"{"Model":"up"}"#, r#"{"Model":"up"}"#),
            (r#"{"models":"up"}"#, r#"{"models":"up"}"#),
            (r#"{"mode":"up"}"#, r#"{"mode":"up"}"#),
            (r#"{"m\nodel":"up"}"#, r#"{"m\nodel":"up"}"#),
            (
                r#"{"\u006dodel\u0000":"up"}"#,
                r#"{"\u006dodel\u0000":"up"}"#,
            ),
            // Duplicates: all replaced.
            (
                r#"{"model":"a","x":1,"model":"b"}"#,
                r#"{"model":"called","x":1,"model":"called"}"#,
            ),
            // Non-string values stay; absent is not added.
            (r#"{"model":null,"a":1}"#, r#"{"model":null,"a":1}"#),
            (r#"{"model":12,"a":1}"#, r#"{"model":12,"a":1}"#),
            (r#"{"model":{"model":"x"}}"#, r#"{"model":{"model":"x"}}"#),
            (r#"{"model":["x"],"a":1}"#, r#"{"model":["x"],"a":1}"#),
            (r#"{"a":1}"#, r#"{"a":1}"#),
            ("{}", "{}"),
            // Not an object.
            ("[]", "[]"),
            (r#"[{"model":"x"}]"#, r#"[{"model":"x"}]"#),
            (r#""model""#, r#""model""#),
            ("null", "null"),
            ("", ""),
        ] {
            assert_eq!(json(input), expected, "{input}");
        }
    }

    #[test]
    fn invalid_json_is_forwarded_from_the_point_it_breaks() {
        for (input, expected) in [
            // Garbage before any object.
            (r#"nope{"model":"x"}"#, r#"nope{"model":"x"}"#),
            // Broken before the key: nothing later is touched.
            (r#"{x,"model":"up"}"#, r#"{x,"model":"up"}"#),
            (r#"{,"model":"up"}"#, r#"{,"model":"up"}"#),
            (r#"{"a" 1,"model":"up"}"#, r#"{"a" 1,"model":"up"}"#),
            (r#"{"a":,"model":"up"}"#, r#"{"a":,"model":"up"}"#),
            (r#"{"a":1 "model":"up"}"#, r#"{"a":1 "model":"up"}"#),
            (r#"{"a":1,,"model":"up"}"#, r#"{"a":1,,"model":"up"}"#),
            (r#"{"a":12"model":"up"}"#, r#"{"a":12"model":"up"}"#),
            // Mismatched brackets are noticed.
            (r#"{"a":[},"model":"up"}"#, r#"{"a":[},"model":"up"}"#),
            (r#"{"a":{],"model":"up"}"#, r#"{"a":{],"model":"up"}"#),
            // A replaced value stays replaced; the tail is untouched.
            (
                r#"{"model":"up"!!!,"model":"b"}"#,
                r#"{"model":"called"!!!,"model":"b"}"#,
            ),
            // Cut mid-value: the name was already sent whole.
            (r#"{"model":"up"#, r#"{"model":"called""#),
            (r#"{"model":"up\"#, r#"{"model":"called""#),
            // A control byte stops the replacement and is forwarded.
            (
                "{\"model\":\"u\np\",\"a\":1}",
                "{\"model\":\"called\"\np\",\"a\":1}",
            ),
            // After the object nothing changes, not even another object.
            (r#"{"a":1}{"model":"x"}"#, r#"{"a":1}{"model":"x"}"#),
            (r#"{}{"model":"x"}"#, r#"{}{"model":"x"}"#),
            (
                r#"{"model":"up"} {"model":"x"}"#,
                r#"{"model":"called"} {"model":"x"}"#,
            ),
            // Truncated elsewhere: forwarded as is.
            (r#"{"a":"x"#, r#"{"a":"x"#),
            (r#"{"mod"#, r#"{"mod"#),
        ] {
            assert_eq!(json(input), expected, "{input}");
            for split in 0..=input.len() {
                assert_eq!(
                    run(input.as_bytes(), false, &[split]),
                    expected.as_bytes(),
                    "{input} split at {split}"
                );
            }
        }
    }

    #[test]
    fn nesting_beyond_the_limit_passes_through() {
        let depth = usize::from(MAX_NESTING);
        let wrap = |levels: usize| {
            format!(
                "{{\"a\":{}{},\"model\":\"up\"}}",
                "[".repeat(levels),
                "]".repeat(levels)
            )
        };
        assert_eq!(
            json(&wrap(depth)),
            wrap(depth).replace("\"up\"", "\"called\"")
        );
        assert_eq!(json(&wrap(depth + 1)), wrap(depth + 1));
    }

    #[test]
    fn the_called_name_goes_in_escaped() {
        for called in [
            "a\"b",
            "back\\slash",
            "模型/名字",
            "tab\tnew\nline",
            "\u{1}",
        ] {
            let quoted = serde_json::to_vec(called).unwrap();
            let mut rewriter = ModelRewriter::new(Bytes::from(quoted), false);
            let mut out = Vec::new();
            drain(&mut rewriter, br#"{"id":"x","model":"up","n":1}"#, &mut out);
            let value: serde_json::Value = serde_json::from_slice(&out).unwrap();
            assert_eq!(value["model"], called);
            assert_eq!(value["id"], "x");
            assert_eq!(value["n"], 1);
        }
    }

    #[test]
    fn larger_than_max_body_is_not_buffered() {
        const FRAME: usize = 64 * 1024;
        // A 40 MiB embeddings body, above the 32 MiB `MAX_BODY`, `model` last.
        let vector = format!("[{}0.5]", "0.125,".repeat(2000));
        let item = format!(r#"{{"object":"embedding","index":0,"embedding":{vector}}}"#);
        let count = 40 * 1024 * 1024 / item.len() + 1;
        let mut body = String::with_capacity(count * (item.len() + 1) + 64);
        body.push_str(r#"{"object":"list","data":["#);
        for index in 0..count {
            if index > 0 {
                body.push(',');
            }
            body.push_str(&item);
        }
        body.push_str(r#"],"model":"up","usage":{"prompt_tokens":8,"total_tokens":8}}"#);
        assert!(body.len() > 32 * 1024 * 1024);

        let mut rewriter = ModelRewriter::new(name(), false);
        let mut sent = 0;
        let mut tail = Vec::new();
        for frame in body.as_bytes().chunks(FRAME) {
            rewriter.push(Bytes::copy_from_slice(frame));
            let mut out = 0;
            while let Some(piece) = rewriter.next_piece() {
                out += piece.len();
                if sent + out > body.len() - 128 {
                    tail.extend_from_slice(&piece);
                }
            }
            // Everything scanned leaves at once, whichever frame holds `model`.
            assert!(out <= FRAME + NAME.len() + 2);
            assert!(out + 4 >= frame.len());
            assert!(!rewriter.has_pending());
            sent += out;
        }
        assert_eq!(sent, body.len() + NAME.len() - "up".len());
        let tail = String::from_utf8_lossy(&tail);
        assert!(
            tail.contains(r#"],"model":"called","usage":{"prompt_tokens":8"#),
            "{tail}"
        );
    }

    #[test]
    fn scanning_allocates_nothing_per_frame() {
        // Frames shaped like hyper's: slices of one shared buffer.
        let mut input = String::new();
        for _ in 0..200 {
            input.push_str("data: {\"id\":\"c\",\"model\":\"up\",\"choices\":[{\"delta\":{\"content\":\"hello\\n\"}}],\"usage\":null}\n\n");
        }
        input.push_str("data: [DONE]\n\n");
        let mut buffer = BytesMut::from(input.as_bytes());
        let mut frames = Vec::new();
        while !buffer.is_empty() {
            let frame = buffer.split_to(buffer.len().min(37));
            frames.push(frame.freeze());
        }
        let name = name();
        // Moves the name to a shared allocation, as its first clone would.
        drop(name.clone());
        let mut rewriter = ModelRewriter::new(name, true);
        let mut total = 0;
        let ((allocations, _), ()) = crate::test_alloc::count(|| {
            for frame in frames {
                rewriter.push(frame);
                while let Some(piece) = rewriter.next_piece() {
                    total += piece.len();
                }
            }
        });
        assert_eq!(allocations, 0);
        assert_eq!(total, input.len() + 200 * (NAME.len() - "up".len()));
    }

    #[test]
    fn a_frame_without_model_is_forwarded_as_the_same_bytes() {
        let frame = Bytes::from_static(br#"{"object":"list","data":[1,2,3]"#);
        let mut rewriter = ModelRewriter::new(name(), false);
        rewriter.push(frame.clone());
        let piece = rewriter.next_piece().unwrap();
        assert_eq!(piece.as_ptr(), frame.as_ptr());
        assert_eq!(piece.len(), frame.len());
        assert!(rewriter.next_piece().is_none());
    }

    // ---- SSE ----

    #[test]
    fn sse_rewrites_each_event_and_leaves_the_rest() {
        for (input, expected) in [
            (
                "data: {\"model\":\"up\"}\n\ndata: {\"id\":1,\"model\":\"up\"}\n\ndata: [DONE]\n\n",
                "data: {\"model\":\"called\"}\n\ndata: {\"id\":1,\"model\":\"called\"}\n\ndata: [DONE]\n\n",
            ),
            // No space after the colon; a second space belongs to the value.
            ("data:{\"model\":\"up\"}\n\n", "data:{\"model\":\"called\"}\n\n"),
            ("data:  {\"model\":\"up\"}\n\n", "data:  {\"model\":\"called\"}\n\n"),
            // CRLF and bare CR.
            (
                "data: {\"model\":\"up\"}\r\n\r\ndata: {\"model\":\"up\"}\r\n\r\ndata: [DONE]\r\n\r\n",
                "data: {\"model\":\"called\"}\r\n\r\ndata: {\"model\":\"called\"}\r\n\r\ndata: [DONE]\r\n\r\n",
            ),
            (
                "data: {\"model\":\"up\"}\r\rdata: {\"model\":\"up\"}\r\r",
                "data: {\"model\":\"called\"}\r\rdata: {\"model\":\"called\"}\r\r",
            ),
            // Comments and other fields, also between `data` lines.
            (
                ": ping\n\nevent: chunk\nid: 7\nretry: 10\ndata: {\"model\":\"up\"}\n\n",
                ": ping\n\nevent: chunk\nid: 7\nretry: 10\ndata: {\"model\":\"called\"}\n\n",
            ),
            (
                "data: {\"a\":1,\n: model\nevent: \"model\":\"x\"\ndata: \"model\":\"up\"}\n\n",
                "data: {\"a\":1,\n: model\nevent: \"model\":\"x\"\ndata: \"model\":\"called\"}\n\n",
            ),
            // Multi-line data: key and value on different lines.
            (
                "data: {\"model\":\ndata: \"up\",\ndata: \"a\":1}\n\n",
                "data: {\"model\":\ndata: \"called\",\ndata: \"a\":1}\n\n",
            ),
            (
                "data: {\ndata\ndata: \"model\"\ndata: :\ndata: \"up\"}\n\n",
                "data: {\ndata\ndata: \"model\"\ndata: :\ndata: \"called\"}\n\n",
            ),
            // A scalar ends at the end of its line.
            (
                "data: {\"a\":1\ndata: ,\"model\":\"up\"}\n\n",
                "data: {\"a\":1\ndata: ,\"model\":\"called\"}\n\n",
            ),
            // Only `data` is data.
            (
                "database: {\"model\":\"up\"}\nDATA: {\"model\":\"up\"}\ndat: x\n\n",
                "database: {\"model\":\"up\"}\nDATA: {\"model\":\"up\"}\ndat: x\n\n",
            ),
            // Events without JSON do not disturb the next event.
            (
                "event: ping\n\ndata: hello\n\ndata: {\"model\":\"up\"}\n\n",
                "event: ping\n\ndata: hello\n\ndata: {\"model\":\"called\"}\n\n",
            ),
            // An unterminated or broken event ends at the blank line.
            (
                "data: {\"a\":\n\ndata: {bad\n\ndata: {\"model\":\"up\"}\n\n",
                "data: {\"a\":\n\ndata: {bad\n\ndata: {\"model\":\"called\"}\n\n",
            ),
            // A raw line break inside the value keeps the lines and the next event.
            (
                "data: {\"model\":\"u\ndata: p\"}\n\ndata: {\"model\":\"up\"}\n\n",
                "data: {\"model\":\"called\"\ndata: p\"}\n\ndata: {\"model\":\"called\"}\n\n",
            ),
            // Nested only.
            (
                "data: {\"a\":{\"model\":\"x\"}}\n\n",
                "data: {\"a\":{\"model\":\"x\"}}\n\n",
            ),
            // No final blank line.
            ("data: {\"model\":\"up\"}", "data: {\"model\":\"called\"}"),
            // A stream that is really a JSON body has no `data` field.
            ("{\"model\":\"up\"}", "{\"model\":\"up\"}"),
            ("", ""),
        ] {
            assert_eq!(sse(input), expected, "{input:?}");
            for split in 0..=input.len() {
                assert_eq!(
                    run(input.as_bytes(), true, &[split]),
                    expected.as_bytes(),
                    "{input:?} split at {split}"
                );
            }
        }
    }

    /// The expected event stream: each document's lines as `data` lines, a
    /// comment or field now and then, rewritten with the reference.
    fn sse_case(generator: &mut Generator, terminator: &str) -> (String, String) {
        let (mut input, mut expected) = (String::new(), String::new());
        for _ in 0..generator.rng.usize(1..5) {
            match generator.rng.u8(0..6) {
                0 => {
                    let raw = format!(": comment \"model\":\"x\"{terminator}{terminator}");
                    input.push_str(&raw);
                    expected.push_str(&raw);
                }
                1 => {
                    let raw = format!("data: [DONE]{terminator}{terminator}");
                    input.push_str(&raw);
                    expected.push_str(&raw);
                }
                2 => {
                    let raw = format!("event: ping{terminator}{terminator}");
                    input.push_str(&raw);
                    expected.push_str(&raw);
                }
                _ => {
                    let doc = generator.document();
                    let new = reference(&doc);
                    let original_lines: Vec<&str> = doc.split('\n').collect();
                    let new_lines: Vec<&str> = new.split('\n').collect();
                    assert_eq!(original_lines.len(), new_lines.len());
                    for (index, (old, new)) in original_lines.iter().zip(&new_lines).enumerate() {
                        if index > 0 && generator.rng.u8(0..4) == 0 {
                            let comment = format!(": \"model\":\"x\"{terminator}");
                            input.push_str(&comment);
                            expected.push_str(&comment);
                        }
                        let prefix = ["data: ", "data:"][generator.rng.usize(0..2)];
                        // A line starting with a space would lose it after `data:`.
                        let prefix = if old.starts_with(' ') {
                            "data: "
                        } else {
                            prefix
                        };
                        input.push_str(prefix);
                        input.push_str(old);
                        input.push_str(terminator);
                        expected.push_str(prefix);
                        expected.push_str(new);
                        expected.push_str(terminator);
                    }
                    input.push_str(terminator);
                    expected.push_str(terminator);
                }
            }
        }
        (input, expected)
    }

    #[test]
    fn sse_matches_the_reference_at_every_split_point() {
        let mut generator = Generator {
            rng: fastrand::Rng::with_seed(0x535345),
            newlines: true,
        };
        for round in 0..1500 {
            let terminator = ["\n", "\r\n", "\r"][round % 3];
            let (input, expected) = sse_case(&mut generator, terminator);
            assert_eq!(sse(&input), expected, "{input:?}");
            for split in 0..=input.len() {
                assert_eq!(
                    run(input.as_bytes(), true, &[split]),
                    expected.as_bytes(),
                    "{input:?} split at {split}"
                );
            }
            let splits = random_splits(&mut generator.rng, input.len());
            assert_eq!(
                run(input.as_bytes(), true, &splits),
                expected.as_bytes(),
                "{input:?} split at {splits:?}"
            );
        }
    }

    // ---- Body ----

    fn body_of(
        frames: Vec<Result<Frame<Bytes>, std::io::Error>>,
    ) -> impl Body<Data = Bytes, Error = std::io::Error> + Unpin {
        StreamBody::new(tokio_stream::iter(frames))
    }

    #[test]
    fn a_bom_at_the_start_of_sse_is_skipped_and_forwarded() {
        let input = b"\xef\xbb\xbfdata: {\"model\":\"up\"}\n\ndata: {\"model\":\"up\"}\n\n";
        let expected =
            b"\xef\xbb\xbfdata: {\"model\":\"called\"}\n\ndata: {\"model\":\"called\"}\n\n";
        for first in 0..=input.len() {
            assert_eq!(run(input, true, &[first]), expected, "split at {first}");
        }
        // Every way of cutting the BOM itself, one byte per frame included.
        for first in 0..=3 {
            for second in first..=3 {
                assert_eq!(
                    run(input, true, &[first, second]),
                    expected,
                    "split at {first}, {second}"
                );
            }
        }
        // A BOM followed by a comment, a blank line or nothing.
        for (input, expected) in [
            (
                &b"\xef\xbb\xbf: c\n\ndata: {\"model\":\"up\"}\n\n"[..],
                &b"\xef\xbb\xbf: c\n\ndata: {\"model\":\"called\"}\n\n"[..],
            ),
            (
                b"\xef\xbb\xbf\r\ndata: {\"model\":\"up\"}\r\n\r\n",
                b"\xef\xbb\xbf\r\ndata: {\"model\":\"called\"}\r\n\r\n",
            ),
            (b"\xef\xbb\xbf", b"\xef\xbb\xbf"),
        ] {
            for split in 0..=input.len() {
                assert_eq!(run(input, true, &[split]), expected, "{input:?} at {split}");
            }
        }
    }

    #[test]
    fn only_a_whole_bom_at_the_very_start_is_skipped() {
        for (input, expected) in [
            // Later in the stream it is part of an unknown field.
            (
                &b"data: {\"model\":\"up\"}\n\n\xef\xbb\xbfdata: {\"model\":\"up\"}\n\n"[..],
                &b"data: {\"model\":\"called\"}\n\n\xef\xbb\xbfdata: {\"model\":\"up\"}\n\n"[..],
            ),
            // Only one is skipped.
            (
                b"\xef\xbb\xbf\xef\xbb\xbfdata: {\"model\":\"up\"}\n\n",
                b"\xef\xbb\xbf\xef\xbb\xbfdata: {\"model\":\"up\"}\n\n",
            ),
            // Part of one ends with its line; the next line is read normally.
            (
                b"\xef\xbbdata: {\"model\":\"up\"}\ndata: x\n\ndata: {\"model\":\"up\"}\n\n",
                b"\xef\xbbdata: {\"model\":\"up\"}\ndata: x\n\ndata: {\"model\":\"called\"}\n\n",
            ),
            (
                b"\xef\ndata: {\"model\":\"up\"}\n\n",
                b"\xef\ndata: {\"model\":\"called\"}\n\n",
            ),
        ] {
            for split in 0..=input.len() {
                assert_eq!(run(input, true, &[split]), expected, "{input:?} at {split}");
            }
        }
    }

    #[test]
    fn a_json_body_with_a_bom_is_forwarded_unchanged() {
        let input = b"\xef\xbb\xbf{\"model\":\"up\"}";
        for split in 0..=input.len() {
            assert_eq!(run(input, false, &[split]), input);
        }
    }

    #[tokio::test]
    async fn checksums_are_dropped_from_trailers() {
        let mut trailers = hyper::HeaderMap::new();
        for name in REPRESENTATION_FIELDS {
            trailers.append(name, "stale".parse().unwrap());
        }
        trailers.append("digest", "stale-too".parse().unwrap());
        trailers.insert("x-end", "1".parse().unwrap());
        let body = RewriteBody::new(
            body_of(vec![
                Ok(Frame::data(Bytes::from_static(b"{\"model\":\"up\"}"))),
                Ok(Frame::trailers(trailers)),
            ]),
            ModelRewriter::new(name(), false),
        );
        let collected = body.collect().await.unwrap();
        let kept = collected.trailers().unwrap();
        assert_eq!(kept.len(), 1, "{kept:?}");
        assert_eq!(kept["x-end"], "1");

        // Trailers holding nothing else disappear, and the body still ends.
        let mut trailers = hyper::HeaderMap::new();
        trailers.insert("etag", "stale".parse().unwrap());
        let body = RewriteBody::new(
            body_of(vec![
                Ok(Frame::data(Bytes::from_static(b"{\"model\":\"up\"}"))),
                Ok(Frame::trailers(trailers)),
            ]),
            ModelRewriter::new(name(), false),
        );
        let collected = body.collect().await.unwrap();
        assert!(collected.trailers().is_none());
        assert_eq!(collected.to_bytes(), &br#"{"model":"called"}"#[..]);
    }

    #[tokio::test]
    async fn the_body_splices_frames_and_keeps_trailers_and_errors() {
        let mut trailers = hyper::HeaderMap::new();
        trailers.insert("x-end", "1".parse().unwrap());
        let body = RewriteBody::new(
            body_of(vec![
                Ok(Frame::data(Bytes::from_static(b"{\"model\":\"u"))),
                Ok(Frame::data(Bytes::from_static(b"p\",\"a\":1}"))),
                Ok(Frame::trailers(trailers)),
            ]),
            ModelRewriter::new(name(), false),
        );
        assert!(body.size_hint().exact().is_none());
        let collected = body.collect().await.unwrap();
        assert_eq!(collected.trailers().unwrap()["x-end"], "1");
        assert_eq!(collected.to_bytes(), &br#"{"model":"called","a":1}"#[..]);

        let mut body = RewriteBody::new(
            body_of(vec![
                Ok(Frame::data(Bytes::from_static(b"{\"model\":\"up\"}"))),
                Err(std::io::Error::other("cut")),
            ]),
            ModelRewriter::new(name(), false),
        );
        let mut out = Vec::new();
        let error = loop {
            match body.frame().await.expect("an error") {
                Ok(frame) => out.extend_from_slice(frame.data_ref().unwrap()),
                Err(error) => break error,
            }
        };
        assert_eq!(error.to_string(), "cut");
        assert_eq!(out, br#"{"model":"called"}"#);
    }

    #[tokio::test]
    async fn the_body_reports_its_end_only_when_drained() {
        // Full reports its end right after its only frame.
        let inner = http_body_util::Full::new(Bytes::from_static(br#"{"model":"up","a":1}"#));
        let mut body = RewriteBody::new(inner, ModelRewriter::new(name(), false));
        let mut pieces = Vec::new();
        while !body.is_end_stream() {
            let frame = body.frame().await.unwrap().unwrap();
            pieces.push(frame.into_data().unwrap());
        }
        assert_eq!(pieces.concat(), br#"{"model":"called","a":1}"#);
        assert_eq!(pieces.len(), 3, "prefix, name, suffix");
        assert!(body.frame().await.is_none());
    }
}

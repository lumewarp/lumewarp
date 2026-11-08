//! Small helpers shared by the MCP and A2A mock servers.

use sha2::{Digest, Sha256};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_millis() as u64
}

pub fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Lowercase hex, or `None` for odd lengths and non-hex characters.
pub fn hex_decode(input: &str) -> Option<Vec<u8>> {
    if !input.len().is_multiple_of(2) {
        return None;
    }
    let mut out = Vec::with_capacity(input.len() / 2);
    let bytes = input.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let hi = (bytes[index] as char).to_digit(16)?;
        let lo = (bytes[index + 1] as char).to_digit(16)?;
        out.push((hi * 16 + lo) as u8);
        index += 2;
    }
    Some(out)
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex_encode(&Sha256::digest(bytes))
}

/// Day count for `year-month-day`, proleptic Gregorian (Howard Hinnant).
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let month = i64::from(month);
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };
    (year, month as u32, day as u32)
}

/// `YYYY-MM-DDTHH:mm:ss.sssZ`, the ProtoJSON timestamp shape A2A uses.
pub fn iso8601_from_ms(unix_ms: u64) -> String {
    let secs = (unix_ms / 1000) as i64;
    let millis = unix_ms % 1000;
    let days = secs.div_euclid(86_400);
    let rest = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = rest / 3_600;
    let minute = (rest % 3_600) / 60;
    let second = rest % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z")
}

pub fn now_iso8601_ms() -> String {
    iso8601_from_ms(now_ms())
}

/// Parse the timestamp shape emitted above, including fractions of a second
/// with up to nine digits. Returns `None` on anything else.
pub fn parse_iso8601_ms(text: &str) -> Option<u64> {
    let text = text.strip_suffix('Z')?;
    let (date, time) = text.split_once('T')?;
    let mut date = date.split('-');
    let year: i64 = date.next()?.parse().ok()?;
    let month: u32 = date.next()?.parse().ok()?;
    let day: u32 = date.next()?.parse().ok()?;
    if date.next().is_some() {
        return None;
    }
    let mut time = time.split(':');
    let hour: u64 = time.next()?.parse().ok()?;
    let minute: u64 = time.next()?.parse().ok()?;
    let seconds = time.next()?;
    if time.next().is_some() {
        return None;
    }
    let (seconds, fraction) = match seconds.split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (seconds, None),
    };
    let seconds: u64 = seconds.parse().ok()?;
    let millis = match fraction {
        None => 0,
        Some(fraction) if !fraction.is_empty() && fraction.len() <= 9 => {
            let mut value: u64 = fraction.get(..3).unwrap_or(fraction).parse().ok()?;
            for _ in fraction.len().min(3)..3 {
                value *= 10;
            }
            value
        }
        Some(_) => return None,
    };
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || seconds > 59
    {
        return None;
    }
    let days = days_from_civil(year, month, day);
    if days < 0 {
        return None;
    }
    Some(days as u64 * 86_400_000 + hour * 3_600_000 + minute * 60_000 + seconds * 1_000 + millis)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_round_trip() {
        assert_eq!(hex_encode(&[0x00, 0xab, 0xff]), "00abff");
        assert_eq!(hex_decode("00abff"), Some(vec![0x00, 0xab, 0xff]));
        assert_eq!(hex_decode("abc"), None);
        assert_eq!(hex_decode("zz"), None);
    }

    #[test]
    fn iso8601_round_trip() {
        assert_eq!(iso8601_from_ms(0), "1970-01-01T00:00:00.000Z");
        let timestamp = "2026-11-08T07:00:00.123Z";
        let parsed = parse_iso8601_ms(timestamp).unwrap();
        assert_eq!(iso8601_from_ms(parsed), timestamp);
        assert_eq!(parse_iso8601_ms("not-a-time"), None);
        assert_eq!(parse_iso8601_ms("2026-13-08T07:00:00.000Z"), None);
    }
}

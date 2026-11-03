"""Unit tests for the pg_dump credential scanner; no stack, no Postgres, no binaries."""

import base64
import hashlib
import json

import pytest
from secret_scan import describe, scan_text

CHARS = "ABCdef0123456789-_" * 4
DISPLAY = "lw-AbCdEf"


def full_token(prefix: str, salt: str) -> str:
    # 32 random bytes encoded by secrets.token_urlsafe: exactly 43 urlsafe characters.
    body = (salt + CHARS)[:43]
    return prefix + body


def dump(*rows: tuple[str, str]) -> str:
    parts = ["--\n-- PostgreSQL database dump\n--\n"]
    for table, row in rows:
        parts.append(f"COPY public.{table} (id, value) FROM stdin;\n{row}\n\\.\n")
    return "".join(parts)


def positions(hits):
    return sorted((hit["kind"], hit["table"], hit["line"]) for hit in hits)


def test_a_full_token_of_every_kind_is_a_hit():
    keys = [
        full_token(prefix, salt)
        for prefix, salt in (("lw-", "a"), ("lwp-", "b"), ("lwg-", "c"))
    ]
    text = dump(
        ("api_keys", f"1\t{keys[0]}"),
        ("personal_access_tokens", f"1\t{keys[1]}"),
        ("gateway_tokens", f"1\t{keys[2]}"),
    )
    hits = scan_text(text, [])
    assert positions(hits) == [
        ("full token lw-", "api_keys", 5),
        ("full token lwg-", "gateway_tokens", 11),
        ("full token lwp-", "personal_access_tokens", 8),
    ]
    report = describe(hits)
    for key in keys:
        assert key not in report, "the report must never print the secret"
    assert "line 5" in report and "api_keys" in report


def test_display_prefixes_hashes_and_literal_prefixes_pass():
    token = full_token("lw-", "seed")
    digest = hashlib.sha256(token.encode()).digest()
    text = dump(
        ("api_keys", f"1\t{DISPLAY}\t\\x{digest.hex()}\t{base64.b64encode(digest).decode()}"),
        ("personal_access_tokens", f"1\tlwp-AbCdEf\t{base64.urlsafe_b64encode(digest).decode()}"),
        ("gateway_tokens", "1\tlwg-AbCdEf"),
    )
    # The full token is recorded, but only its 6-character display form and its hash are
    # stored: neither may trip the scanner.
    assert scan_text(text, [{"kind": "API key", "value": token}]) == []


def test_a_recorded_literal_is_found_plain_and_encoded():
    secret = "p25-session-value-0123456789abcdef"
    variants = {
        "plain": secret,
        "base64": base64.b64encode(secret.encode()).decode(),
        "base64-urlsafe": base64.urlsafe_b64encode(secret.encode()).decode(),
        "hex": secret.encode().hex(),
    }
    for encoding, needle in variants.items():
        text = dump(("api_keys", f"1\t{needle}"))
        hits = scan_text(text, [{"kind": "session cookie", "value": secret}])
        assert len(hits) == 1, (encoding, hits)
        assert hits[0]["kind"].startswith("session cookie")
        assert hits[0]["table"] == "api_keys"
        assert secret not in describe(hits)
        assert needle not in describe(hits)


def test_a_url_encoded_literal_is_found():
    url = "https://hooks.example.test/services/T000/B000/abcdefghijklmnop"
    from urllib.parse import quote, quote_plus

    for encoding in (quote(url, safe=""), quote_plus(url)):
        text = dump(("notifier_channels", f"1\t{encoding}"))
        hits = scan_text(text, [{"kind": "webhook URL", "value": url}])
        assert len(hits) == 1, (encoding, hits)
        assert url not in describe(hits)


def test_a_token_outside_a_copy_section_still_fails_without_a_table():
    token = full_token("lwg-", "outside")
    text = (
        "CREATE FUNCTION e2e_changed() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        f"BEGIN PERFORM '{token}'; RETURN NULL; END $$;\n"
    )
    [hit] = scan_text(text, [])
    assert hit["kind"] == "full token lwg-"
    assert hit["table"] is None
    assert "outside a table" in describe([hit])


def test_one_position_is_reported_once_even_when_pattern_and_literal_match():
    token = full_token("lw-", "both")
    text = dump(("api_keys", f"1\t{token}"))
    hits = scan_text(text, [{"kind": "API key", "value": token}])
    assert len(hits) == 1
    assert hits[0]["kind"] == "full token lw-"


def test_a_recorded_literal_is_required_to_match_completely():
    # A prefix of a recorded secret is not the secret (and is not a leak of it).
    secret = "p25-session-value-0123456789abcdef"
    text = dump(("api_keys", "1\tp25-session-value"))
    assert scan_text(text, [{"kind": "session cookie", "value": secret}]) == []


def test_hits_are_json_serialisable_without_the_secret():
    token = full_token("lw-", "json")
    hits = scan_text(dump(("api_keys", f"1\t{token}")), [])
    encoded = json.dumps(hits)
    assert token not in encoded
    assert json.loads(encoded) == hits


@pytest.mark.parametrize("prefix", ["lw-", "lwp-", "lwg-"])
def test_a_short_body_is_not_a_full_token(prefix):
    # The display prefix plus a few characters, as a name or a prefix column could hold.
    assert scan_text(dump(("api_keys", f"1\t{prefix}abcdefgh")), []) == []

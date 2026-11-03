"""Unit tests for the pg_dump credential scanner; no stack, Postgres, or binaries.

The real `pg_dump` cases run against a disposable database on the dev server when it is
reachable and `pg_dump` exists (they are skipped otherwise); the e2e scripts run this file
with both available, so the escaping, bytea and large-object cases are exercised there.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import secret_scan
from no_secret import assert_no_secret
from p2_identity import cookie_api
from secret_scan import (
    database_name,
    describe,
    dump_database,
    env_ref_literals,
    finalize,
    load_literals,
    scan_database,
    scan_text,
)
from stack import PG_DSN, Stack, pg

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


# ------------------------------------------------------------------ scanned shapes


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
    assert_no_secret(report, keys, "the scan report contains a secret")
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
        assert_no_secret(describe(hits), [secret, needle], "the scan report contains a secret")


def test_copy_and_jsonb_escaped_literals_are_found():
    # A text column's COPY text and a jsonb column's JSON-inside-COPY text, exactly as
    # pg_dump 16 writes backslash, TAB, newline and quote (verified by the real case below).
    secret = 'pw\\back\ttab\nnl"quote'
    row = (
        "1\t"
        + r'pw\\back\ttab\nnl"quote'
        + "\t"
        + r'{"k": "pw\\\\back\\ttab\\nnl\\"quote"}'
    )
    text = dump(("t", row))
    assert_no_secret(text, [secret], "the raw literal is in the dump before the scan")
    hits = scan_text(text, [{"kind": "env password", "value": secret}])
    kinds = {hit["kind"] for hit in hits}
    assert any(kind.endswith("(copy)") for kind in kinds), kinds
    assert any(kind.endswith("(copy-of-json)") for kind in kinds), kinds
    assert {hit["table"] for hit in hits} == {"t"}
    assert_no_secret(describe(hits), [secret], "the scan report contains the secret")


def test_a_bytea_value_is_decoded_and_its_token_is_a_hit():
    token = full_token("lw-", "bytea")
    # COPY escapes the backslash of PostgreSQL's `\x...` bytea format, so the dump has
    # two of them; the hex is the token's bytes, not its UTF-8 text.
    text = dump(("api_keys", f"1\t\\\\x{token.encode().hex()}"))
    hits = scan_text(text, [])
    [hit] = hits
    assert hit["kind"] == "full token lw- (bytea)"
    assert hit["table"] == "api_keys"
    assert_no_secret(describe(hits), [token], "the scan report contains the secret")


def test_a_recorded_literal_inside_bytea_is_found():
    secret = "p25-session-value-0123456789abcdef"
    body = b"prefix" + secret.encode() + b"suffix"
    text = dump(("api_keys", f"1\t\\\\x{body.hex()}"))
    hits = scan_text(text, [{"kind": "session cookie", "value": secret}])
    assert any(hit["kind"].startswith("session cookie") for hit in hits), hits
    assert_no_secret(describe(hits), [secret], "the scan report contains the secret")


def test_a_large_object_in_the_dump_is_itself_a_hit():
    text = (
        "--\n-- Data for Name: BLOBS; Type: BLOBS; Schema: -; Owner: -\n--\n\n"
        "SELECT pg_catalog.lo_create('4242');\n"
        "SELECT pg_catalog.lowrite(0, '\\xdeadbeef');\n"
    )
    hits = scan_text(text, [])
    kinds = {hit["kind"] for hit in hits}
    assert "large object (lo_create)" in kinds
    assert "large object (lowrite)" in kinds
    assert all(hit["table"] is None for hit in hits)


def test_a_url_encoded_literal_is_found():
    url = "https://hooks.example.test/services/T000/B000/abcdefghijklmnop"
    from urllib.parse import quote, quote_plus

    for encoding in (quote(url, safe=""), quote_plus(url)):
        text = dump(("notifier_channels", f"1\t{encoding}"))
        hits = scan_text(text, [{"kind": "webhook URL", "value": url}])
        assert len(hits) == 1, (encoding, hits)
        assert_no_secret(describe(hits), [url], "the scan report contains the secret")


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
    assert_no_secret(encoded, [token], "the serialised hits contain the secret")
    assert json.loads(encoded) == hits


@pytest.mark.parametrize("prefix", ["lw-", "lwp-", "lwg-"])
def test_a_short_body_is_not_a_full_token(prefix):
    # The display prefix plus a few characters, as a name or a prefix column could hold.
    assert scan_text(dump(("api_keys", f"1\t{prefix}abcdefgh")), []) == []


# ---------------------------------------------------------- the API-boundary recording


def test_cookie_api_records_the_secret_it_returns(tmp_path, monkeypatch):
    log = tmp_path / "secrets.jsonl"
    log.touch()
    monkeypatch.setenv("E2E_SECRET_LOG", str(log))
    value = full_token("lw-", "cookie")

    class Response:
        status_code = 201
        content = b"{}"

        def json(self):
            return {"key": {"id": "k"}, "secret": value}

    class Browser:
        def request(self, *args, **kwargs):
            return Response()

    body = cookie_api(
        Browser(), SimpleNamespace(api_url="http://cp/api/v1"), "POST", "/keys/k/rotate"
    )
    assert (body["secret"] == value) is True
    recorded = load_literals(str(log))
    assert len(recorded) == 1
    # Base64 is how the value could reach a column without matching the token pattern.
    leaked = dump(("api_keys", f"1\t{base64.b64encode(value.encode()).decode()}"))
    hits = scan_text(leaked, recorded)
    assert hits and hits[0]["table"] == "api_keys"
    assert_no_secret(describe(hits), [value], "the scan report contains the secret")


def test_stack_api_and_ok_record_the_secret_they_return(tmp_path, monkeypatch):
    log = tmp_path / "secrets.jsonl"
    log.touch()
    monkeypatch.setenv("E2E_SECRET_LOG", str(log))
    first, second = full_token("lwp-", "api"), full_token("lwg-", "ok")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ok"):
            return httpx.Response(200, json={"token": {"id": "t"}, "secret": second})
        return httpx.Response(201, json={"token": {"id": "t"}, "secret": first})

    class FakeStack:
        api = Stack.api
        ok = Stack.ok

        def __init__(self):
            self.admin = httpx.Client(transport=httpx.MockTransport(handler))
            self.api_url = "http://fake/api/v1"

    fake = FakeStack()
    response = Stack.api(fake, "POST", "/users/u/tokens")
    assert response.status_code == 201
    assert (Stack.ok(fake, "POST", "/ok")["secret"] == second) is True
    recorded = load_literals(str(log))
    assert len(recorded) == 2
    leaked = dump(
        ("personal_access_tokens", f"1\t{base64.b64encode(first.encode()).decode()}"),
        ("gateway_tokens", f"1\t{base64.b64encode(second.encode()).decode()}"),
    )
    hits = scan_text(leaked, recorded)
    assert {hit["table"] for hit in hits} == {"personal_access_tokens", "gateway_tokens"}


# ------------------------------------------------------- a failure never prints a secret


def test_a_planted_failure_through_real_assertion_rewriting_has_no_secret(tmp_path):
    secret = "P25-SECRET-" + "Ab3" * 8
    case = tmp_path / "test_planted.py"
    case.write_text(
        "from no_secret import assert_no_secret\n"
        "\n"
        "PART = 'P25-SECRET-'\n"
        "REST = 'Ab3' * 8\n"
        "SECRET = PART + REST\n"
        "\n"
        "\n"
        "def test_planted_failure():\n"
        "    text = 'audit details: ' + SECRET + ' end'\n"
        "    assert_no_secret(text, [SECRET], 'audit row')\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(case),
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=line",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert_no_secret(output, [secret], "pytest's failure report contains the secret")
    assert "position" in output and "secret #0" in output


# ---------------------------------------------------------------- fail-closed inputs


def test_a_missing_or_corrupt_recorded_secret_log_is_a_hard_failure(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        load_literals(None)
    with pytest.raises(FileNotFoundError):
        load_literals(str(tmp_path / "missing.jsonl"))
    corrupt = tmp_path / "secrets.jsonl"
    corrupt.write_text('{"kind": "API key", "value": "abcdefgh"}\nnot json\n')
    with pytest.raises(ValueError, match="corrupt"):
        load_literals(str(corrupt))
    # scan_database loads the log before it touches a database.
    with pytest.raises(ValueError, match="corrupt"):
        scan_database("postgresql://127.0.0.1:1/none", str(corrupt))
    # A line without a value is corrupt too, not silently skipped.
    corrupt.write_text('{"kind": "API key"}\n')
    with pytest.raises(ValueError, match="corrupt"):
        load_literals(str(corrupt))


def test_a_missing_pg_dump_binary_is_a_hard_failure(monkeypatch):
    monkeypatch.setenv("E2E_PG_DUMP", "/nonexistent/definitely-not-pg_dump")
    with pytest.raises(FileNotFoundError):
        dump_database("postgresql://127.0.0.1:1/none")


def test_a_failed_env_reference_query_is_a_hard_failure(monkeypatch):
    import asyncpg

    async def unreachable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(asyncpg, "connect", unreachable)
    with pytest.raises(RuntimeError, match="connection refused"):
        env_ref_literals("postgresql://127.0.0.1:1/none")


# ------------------------------------------------------- finalizer: no unchecked database


def run_dir(tmp_path, monkeypatch, *, run_id="feedface", manifest=(), log=""):
    (tmp_path / "dbs").write_text("".join(f"{name}\n" for name in manifest))
    (tmp_path / "secrets.jsonl").write_text(log)
    # A real-stack run always builds at least one Stack, which writes this marker.
    (tmp_path / "stacks").write_text("lumewarp_e2e_feedface_marker\n")
    monkeypatch.setenv("E2E_RUN_ID", run_id)
    monkeypatch.setenv("E2E_SECRET_LOG", str(tmp_path / "secrets.jsonl"))
    return tmp_path


def test_finalize_without_a_stack_needs_no_database_server(tmp_path, monkeypatch):
    directory = run_dir(tmp_path, monkeypatch)
    (directory / "stacks").unlink()

    def no_server(url):
        raise AssertionError("the scanner connected to Postgres for a no-stack run")

    monkeypatch.setattr(secret_scan, "_existing_databases", no_server)
    assert finalize(str(directory), "postgresql://server/lumewarp") == 0


def clean_result(name: str) -> dict:
    return {"database": name, "literals": 0, "dump_chars": 0, "hits": []}


def hit_result(name: str) -> dict:
    return {
        "database": name,
        "literals": 0,
        "dump_chars": 0,
        "hits": [{"kind": "full token lw-", "table": "t", "line": 1, "offset": 0, "length": 46}],
    }


def test_finalize_scans_an_unregistered_database_with_this_run_name(tmp_path, monkeypatch):
    name = "lumewarp_e2e_feedface_0123456789ab"
    directory = run_dir(tmp_path, monkeypatch)
    scanned: list[str] = []

    def fake_scan(dsn: str, literals: str | None) -> dict:
        scanned.append(database_name(dsn))
        return hit_result(database_name(dsn))

    monkeypatch.setattr(secret_scan, "_existing_databases", lambda url: {name})
    monkeypatch.setattr(secret_scan, "scan_database", fake_scan)
    assert finalize(str(directory), "postgresql://server/lumewarp") == 1
    assert scanned == [name]
    # It is registered afterwards so the scripts' reaper drops it too.
    assert name in (directory / "dbs").read_text().split()


def test_finalize_accepts_a_clean_unregistered_database_and_registers_it(tmp_path, monkeypatch):
    name = "lumewarp_e2e_feedface_ba9876543210"
    directory = run_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_scan, "_existing_databases", lambda url: {name})
    monkeypatch.setattr(
        secret_scan, "scan_database", lambda dsn, literals: clean_result(database_name(dsn))
    )
    assert finalize(str(directory), "postgresql://server/lumewarp") == 0
    assert name in (directory / "dbs").read_text().split()
    # A second finalize does not rescan what it already recorded.
    assert finalize(str(directory), "postgresql://server/lumewarp") == 0


def test_finalize_fails_when_a_registered_database_vanished_unscanned(tmp_path, monkeypatch):
    name = "lumewarp_e2e_feedface_aaaaaaaaaaaa"
    directory = run_dir(tmp_path, monkeypatch, manifest=[name])
    monkeypatch.setattr(secret_scan, "_existing_databases", lambda url: set())
    assert finalize(str(directory), "postgresql://server/lumewarp") == 1


def test_finalize_refuses_to_run_without_a_run_id(tmp_path, monkeypatch):
    directory = run_dir(tmp_path, monkeypatch, manifest=["lumewarp_e2e_other_aaaaaaaaaaaa"])
    (directory / "stacks").touch()
    monkeypatch.delenv("E2E_RUN_ID")
    with pytest.raises(RuntimeError, match="E2E_RUN_ID"):
        finalize(str(directory), "postgresql://server/lumewarp")


# ---------------------------------------------------------------- real pg_dump (16+)


@pytest.fixture(scope="module")
def real_dump():
    if not os.environ.get("E2E_PG_DUMP") and shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not installed")
    try:
        pg(PG_DSN, "SELECT 1")
    except Exception as err:  # noqa: BLE001 - without a server the real cases cannot run
        pytest.skip(f"Postgres is not reachable for the real pg_dump cases: {err}")


@contextmanager
def disposable_database():
    name = f"lumewarp_scan_ut_{uuid4().hex[:10]}"
    base, _, _ = PG_DSN.rpartition("/")
    pg(PG_DSN, f'CREATE DATABASE "{name}"')
    try:
        yield f"{base}/{name}"
    finally:
        pg(PG_DSN, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_real_pg_dump_keeps_an_escaped_text_and_jsonb_literal_findable(real_dump):
    secret = 'pw\\back\ttab\nnl"quote\x01'
    with disposable_database() as dsn:
        pg(dsn, "CREATE TABLE t (id int, txt text, j jsonb)")
        pg(dsn, "INSERT INTO t VALUES ($1, $2, $3)", 1, secret, json.dumps({"k": secret}))
        text = dump_database(dsn)
    assert_no_secret(text, [secret], "the raw literal is in the real dump before the scan")
    hits = scan_text(text, [{"kind": "env password", "value": secret}])
    kinds = {hit["kind"] for hit in hits}
    assert any(kind.endswith("(copy)") for kind in kinds), kinds
    assert any(kind.endswith("(copy-of-json)") for kind in kinds), kinds
    assert {hit["table"] for hit in hits} == {"t"}
    assert_no_secret(describe(hits), [secret], "the scan report contains the secret")


def test_a_real_pg_dump_of_bytea_and_a_large_object_is_a_hit(real_dump):
    token = full_token("lw-", "realbytea")
    with disposable_database() as dsn:
        pg(dsn, "CREATE TABLE b (id int, data bytea)")
        pg(dsn, "INSERT INTO b VALUES (1, $1)", token.encode())
        oid = pg(dsn, "SELECT lo_create(0) AS oid")[0]["oid"]
        # The token sits past the first lowrite() chunk (16 KiB): a dump-text search cannot
        # see it, so the large object itself has to be the failure.
        pg(
            dsn,
            "SELECT lowrite(lo_open($1, 131072), $2::bytea)",
            oid,
            b"x" * 16370 + token.encode(),
        )
        text = dump_database(dsn)
    hits = scan_text(text, [])
    bytea = [hit for hit in hits if hit["table"] == "b"]
    assert [hit["kind"] for hit in bytea] == ["full token lw- (bytea)"], hits
    assert any(hit["kind"] == "large object (lo_create)" for hit in hits), hits
    assert any(hit["kind"] == "large object (lowrite)" for hit in hits), hits
    assert_no_secret(describe(hits), [token], "the scan report contains the secret")


def test_finalize_finds_an_unregistered_database_with_a_plaintext_token(real_dump, tmp_path, monkeypatch):
    run_id = uuid4().hex[:8]
    name = f"lumewarp_e2e_{run_id}_{uuid4().hex[:12]}"
    base, _, _ = PG_DSN.rpartition("/")
    pg(PG_DSN, f'CREATE DATABASE "{name}"')
    dsn = f"{base}/{name}"
    try:
        # The env-reference query needs these two tables, as a migrated control-plane
        # database has them; create them so a failure can only be the token.
        pg(dsn, "CREATE TABLE upstreams (api_key_ref text)")
        pg(dsn, "CREATE TABLE notifier_channels (kind text, config jsonb)")
        pg(dsn, "CREATE TABLE leaked (value text)")
        pg(dsn, "INSERT INTO leaked VALUES ($1)", full_token("lw-", "unregistered"))
        directory = run_dir(tmp_path, monkeypatch, run_id=run_id)
        # No manifest entry: the finalizer has to discover the database by its name.
        assert finalize(str(directory), PG_DSN) == 1
        assert name in (directory / "dbs").read_text().split()
    finally:
        pg(PG_DSN, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

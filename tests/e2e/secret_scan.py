"""Scan an e2e run's Postgres databases for plaintext credentials.

The e2e harness records every secret it issues or receives (API keys, PATs, gateway
tokens, cookie sessions, CSRF tokens, the OIDC state/nonce/PKCE verifier, the break-glass
password and any env-referenced upstream key or webhook URL). Each stack is dumped with
`pg_dump` before its disposable database is dropped, and the dump is searched for:

1. **Full token shapes**: `lw-`, `lwp-` or `lwg-` followed by exactly 43 urlsafe-base64
   characters (the 32 random bytes `security.generate_secret` encodes). A stored
   `display_prefix` is the prefix plus six characters, so it never matches.
2. **Recorded literals**: each one plain, base64 (standard and urlsafe, padded and
   unpadded), percent-encoded, and hex. The OIDC `state` is stored only as a hash, but a
   plaintext copy anywhere is still a leak, so the raw value is searched for too.

Hits are reported by kind, position (line and character offset) and the table of the
pg_dump `COPY` section that contains them. The secret itself is never printed.

Usage:
    python tests/e2e/secret_scan.py finalize --run-dir <dir> --pg-url <server DSN>

`finalize` reads the run directory's `dbs` (every database the run created, written by
`stack.Stack.setup`) and `secret-scans.jsonl` (one result per database, written by
`Stack.teardown`), scans any database that is still present, and fails when a database
was neither scanned nor still exists, or when any scan found a hit. The e2e scripts call
it after pytest and before their reaper drops leftover databases.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import quote, quote_plus

# `lwp-` and `lwg-` have a four-character prefix; the lookarounds keep a full token from
# matching inside a longer run of token characters (and a display prefix has only 6).
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])(lwp-|lwg-|lw-)[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
COPY_RE = re.compile(r"^COPY (?:\S+\.)?(\S+)(?: \([^)]*\))? FROM stdin;$", re.MULTILINE)
# Variants shorter than this are too likely to hit ordinary text; every recorded secret
# is at least a 32-byte random token.
MIN_VARIANT_CHARS = 12


def _copy_sections(text: str) -> list[tuple[int, int, str]]:
    """(start, end, table) of every COPY data block in a plain-format pg_dump."""
    sections = []
    for match in COPY_RE.finditer(text):
        end = text.find("\n\\.\n", match.end())
        if end < 0:
            end = len(text)
        sections.append((match.end() + 1, end, match.group(1).strip('"')))
    return sections


def _table_at(sections: list[tuple[int, int, str]], offset: int) -> str | None:
    # Sections are ordered and do not overlap; a hit before the first COPY statement
    # (e.g. in a function body) has no table.
    for start, end, table in sections:
        if start <= offset <= end:
            return table
        if offset < start:
            break
    return None


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _variants(value: str) -> dict[str, str]:
    """Encodings a secret could reach a dump through, name -> needle."""
    raw = value.encode()
    encoded = {
        "plain": value,
        "base64": base64.b64encode(raw).decode(),
        "base64-unpadded": base64.b64encode(raw).decode().rstrip("="),
        "base64-urlsafe": base64.urlsafe_b64encode(raw).decode(),
        "base64-urlsafe-unpadded": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "url-quoted": quote(value, safe=""),
        "url-quoted-plus": quote_plus(value),
        "url-quoted-all": "".join(f"%{byte:02X}" for byte in raw),
        "url-quoted-all-lower": "".join(f"%{byte:02x}" for byte in raw),
        "hex": raw.hex(),
        "hex-upper": raw.hex().upper(),
    }
    # A token is already urlsafe base64; percent-encoding and base64 can collide with the
    # plain form, and searching the same needle twice only duplicates hits.
    unique: dict[str, str] = {}
    seen: set[str] = set()
    for name, needle in encoded.items():
        if len(needle) < MIN_VARIANT_CHARS or needle in seen:
            continue
        seen.add(needle)
        unique[name] = needle
    return unique


def _add_hit(hits: list[dict], seen: set[int], text, sections, offset, length, kind):
    # One position is one leak, even when a recorded literal and a token shape both match,
    # or its padded and unpadded base64 forms start at the same place.
    if offset in seen:
        return
    seen.add(offset)
    hits.append(
        {
            "kind": kind,
            "table": _table_at(sections, offset),
            "line": _line_at(text, offset),
            "offset": offset,
            "length": length,
        }
    )


def scan_text(text: str, literals: list[dict]) -> list[dict]:
    """Every full-token and recorded-literal hit in `dump text` (secrets never returned)."""
    sections = _copy_sections(text)
    hits: list[dict] = []
    seen: set[int] = set()
    for match in TOKEN_RE.finditer(text):
        prefix = match.group(1)
        _add_hit(
            hits,
            seen,
            text,
            sections,
            match.start(),
            len(match.group(0)),
            f"full token {prefix}",
        )
    for item in literals:
        kind = item.get("kind") or "recorded secret"
        value = item.get("value")
        if not isinstance(value, str) or not value:
            continue
        for encoding, needle in _variants(value).items():
            start = 0
            while True:
                at = text.find(needle, start)
                if at < 0:
                    break
                _add_hit(
                    hits, seen, text, sections, at, len(needle), f"{kind} ({encoding})"
                )
                start = at + len(needle)
    return hits


def describe(hits: list[dict]) -> str:
    """A report that names the kind, table and position, never the secret."""
    if not hits:
        return "no plaintext credentials"
    lines = [f"{len(hits)} plaintext credential hit(s):"]
    for hit in hits:
        table = hit["table"] or "(outside a table)"
        lines.append(
            f"  {hit['kind']} in {table} at line {hit['line']} offset {hit['offset']}"
            f" ({hit['length']} chars)"
        )
    return "\n".join(lines)


def load_literals(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    items: dict[str, dict] = {}
    with open(path) as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                items.setdefault(
                    item["value"],
                    {"kind": item.get("kind", "recorded secret"), "value": item["value"]},
                )
    return list(items.values())


def env_ref_literals(dsn: str) -> list[dict]:
    """Values of env vars the database references for upstream keys and webhooks.

    The point of an env reference is that the value is never stored, so finding the ref
    name in the dump is fine; the value must not be. No API exists for notifier channels
    yet, so a run may have none. A missing database (already dropped) yields nothing.
    """

    async def refs() -> list[str]:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT config->>'url_ref' AS ref FROM notifier_channels"
                " WHERE kind = 'webhook' AND config ? 'url_ref'"
                " UNION "
                "SELECT api_key_ref AS ref FROM upstreams WHERE api_key_ref IS NOT NULL"
            )
            return [row["ref"] for row in rows if row["ref"]]
        finally:
            await conn.close()

    import asyncio

    try:
        names = asyncio.run(refs())
    except Exception:  # noqa: BLE001 - a DB that is already gone has nothing to reference
        return []
    literals = []
    for name in sorted(set(names)):
        value = os.environ.get(name)
        if value:
            literals.append({"kind": f"env value of {name}", "value": value})
    return literals


def dump_database(dsn: str, *, timeout: float = 600) -> str:
    """Plain-format pg_dump of one database.

    `E2E_PG_DUMP` overrides the command (e.g. `docker exec -i lumewarp-dev-postgres-1
    pg_dump`), which a caller needs when its client is not the same major version as the
    server. The DSN carries the password, so no `PGPASSWORD` handling is required.
    """
    command = shlex.split(os.environ.get("E2E_PG_DUMP", "pg_dump"))
    argv = [*command, "--no-owner", "--no-acl", "--format=plain", dsn]
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result.stdout


def database_name(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def scan_database(dsn: str, literals_path: str | None = None) -> dict:
    """Dump one database and scan it. Returns a JSON-serialisable result (no secrets)."""
    literals = [*load_literals(literals_path), *env_ref_literals(dsn)]
    dump = dump_database(dsn)
    hits = scan_text(dump, literals)
    return {
        "database": database_name(dsn),
        "literals": len(literals),
        "dump_chars": len(dump),
        "hits": hits,
    }


def append_result(path: str | None, result: dict) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(result) + "\n")


def _run_dir_databases(run_dir: str) -> list[str]:
    path = os.path.join(run_dir, "dbs")
    if not os.path.exists(path):
        return []
    with open(path) as source:
        return [line.strip() for line in source if line.strip()]


def _scanned(run_dir: str) -> dict[str, dict]:
    path = os.path.join(run_dir, "secret-scans.jsonl")
    if not os.path.exists(path):
        return {}
    results = {}
    with open(path) as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("database"):
                results[item["database"]] = item
    return results


def _existing_databases(pg_url: str) -> set[str]:
    async def names() -> set[str]:
        import asyncpg

        conn = await asyncpg.connect(pg_url)
        try:
            rows = await conn.fetch("SELECT datname FROM pg_database")
            return {row["datname"] for row in rows}
        finally:
            await conn.close()

    import asyncio

    return asyncio.run(names())


def _dsn_for(pg_url: str, database: str) -> str:
    base, _, _ = pg_url.rpartition("/")
    return f"{base}/{database}"


def finalize(run_dir: str, pg_url: str) -> int:
    """Scan databases the run left behind and verify every created database was scanned."""
    created = _run_dir_databases(run_dir)
    if not created:
        print("secret scan: the run created no databases")
        return 0
    results = _scanned(run_dir)
    missing: list[str] = []
    existing = _existing_databases(pg_url)
    for name in created:
        if name in results:
            continue
        if name not in existing:
            missing.append(name)
            continue
        print(f"secret scan: scanning leftover database {name}")
        result = scan_database(_dsn_for(pg_url, name), os.environ.get("E2E_SECRET_LOG"))
        append_result(os.path.join(run_dir, "secret-scans.jsonl"), result)
        results[name] = result
    hits = [(name, item) for name, item in results.items() if name in created and item["hits"]]
    failed = False
    for name, item in hits:
        print(f"secret scan: {name}: {describe(item['hits'])}", file=sys.stderr)
        failed = True
    if missing:
        print(
            "secret scan: these databases were created but never scanned and no longer"
            f" exist: {', '.join(missing)}",
            file=sys.stderr,
        )
        failed = True
    if failed:
        return 1
    print(f"secret scan: {len(results)} database(s) scanned, no plaintext credentials")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["finalize", "scan"])
    parser.add_argument("--run-dir", default=os.environ.get("E2E_RUN_DIR"))
    parser.add_argument(
        "--pg-url", default=os.environ.get("E2E_PG_URL")
    )  # server DSN; tests create databases beneath it
    parser.add_argument("--dsn", help="scan one database by DSN (command: scan)")
    parser.add_argument("--literals", default=os.environ.get("E2E_SECRET_LOG"))
    args = parser.parse_args(argv)
    if args.command == "scan":
        if not args.dsn:
            parser.error("--dsn is required for scan")
        result = scan_database(args.dsn, args.literals)
        print(f"secret scan: {result['database']}: {describe(result['hits'])}")
        return 1 if result["hits"] else 0
    if not args.run_dir or not args.pg_url:
        parser.error("--run-dir and --pg-url (or E2E_RUN_DIR / E2E_PG_URL) are required")
    return finalize(args.run_dir, args.pg_url)


if __name__ == "__main__":
    sys.exit(main())

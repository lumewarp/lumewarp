"""Process harness for the end-to-end tests: Postgres database, control plane, mock
upstream and gateway, all real processes on ephemeral ports.
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import httpx
from secret_scan import (
    DB_PREFIX,
    MIN_CREDENTIAL_CHARS,
    RUN_ID_ENV,
    RUN_ID_FILE,
    SCAN_REPORT_ENV,
    SECRET_LOG_ENV,
    scan_disabled,
)

ROOT = Path(__file__).resolve().parents[2]
BREAK_GLASS_PASSWORD = "e2e-break-glass-password"
ENCRYPTION_KEY = "e2e-lumewarp-encryption-key-0123456789abcdef"
SCOPE = "e2e"
FORBIDDEN_PORTS = {9000, 5432, 8080, 8081}


def run_id() -> str:
    """The id that makes this run's database names recognisable to the finalizer."""
    value = os.environ.get(RUN_ID_ENV)
    if value:
        return value
    value = uuid.uuid4().hex[:8]
    os.environ[RUN_ID_ENV] = value
    return value


def record_secret(value: str | None, kind: str) -> None:
    """Remember a secret the run issued or received, for the post-run pg_dump scan.

    Append-only and 0600 inside the run directory, which the e2e scripts delete after
    scanning. With E2E_SECRET_LOG unset nothing is recorded; the session fixture sets it
    for a direct pytest run, and a scan without it is a hard failure.

    A credential shorter than `secret_scan.MIN_CREDENTIAL_CHARS` is a hard failure: the
    scan cannot search it without matching ordinary dump text, and silently dropping it
    would let a real leak pass. The message names the kind, never the value.
    """
    if not isinstance(value, str) or not value:
        return
    path = os.environ.get(SECRET_LOG_ENV)
    if not path:
        return
    if len(value) < MIN_CREDENTIAL_CHARS:
        raise RuntimeError(
            f"cannot record {kind}: a credential shorter than {MIN_CREDENTIAL_CHARS}"
            " characters cannot be searched reliably"
        )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps({"kind": kind, "value": value}) + "\n")


# `cp_env` names that merely contain a credential word but hold a setting, not a secret.
# Listed by full name: a suffix would also exclude a credential such as VAULT_SECRET_ID,
# and a value's length says nothing about whether it is a credential.
NON_SECRET_ENV_NAMES = frozenset(
    {"E2E_SECRET_LOG", "E2E_SECRET_SCAN", "E2E_SKIP_SECRET_SCAN"}
)

CREDENTIAL_ENV_PARTS = ("SECRET", "PASSWORD", "TOKEN", "API_KEY")


def is_secret_env_name(name: str) -> bool:
    """Whether a control-plane env var holds a credential (by name, never by value)."""
    upper = name.upper()
    if not any(part in upper for part in CREDENTIAL_ENV_PARTS):
        return False
    return upper not in NON_SECRET_ENV_NAMES


def record_env_secrets(env, source: str = "control-plane env") -> None:
    """Record credentials that reach only the control plane's environment.

    A setting whose name merely matches the filter is excluded by its full name, never by a
    suffix: `VAULT_SECRET_ID` is a credential. An unlisted setting with a short value fails
    the run loudly, and is then added to the list.
    """
    for name, value in env.items():
        if is_secret_env_name(name):
            record_secret(value, f"{source} {name}")


# Credential-bearing response fields. `token` is included because a token response can
# carry the value directly; a nested object (the token's metadata) is walked, not recorded.
RESPONSE_SECRET_FIELDS = {
    "secret": "credential",
    "csrf_token": "CSRF token",
    "access_token": "access token",
    "id_token": "ID token",
    "refresh_token": "refresh token",
    "token": "token",
    "api_key": "API key",
    "password": "password",
    "client_secret": "client secret",
    "code_verifier": "PKCE code_verifier",
    "nonce": "nonce",
    "state": "state",
}


def record_response_secrets(
    payload, source: str = "", *, endpoint: str | None = None
) -> None:
    """Record every credential an API response hands back, at the boundary.

    Called on the response path (Stack.api/ok and the cookie-session helper) so no test
    has to remember the new secret: a create or rotate response is recorded before the
    caller can use it.
    """
    if payload is None:
        return
    if not isinstance(payload, (dict, list)):
        try:
            if not payload.content:
                return
            payload = payload.json()
        except (AttributeError, ValueError):
            return

    # MCP catalog `state` is a typed review enum, not an OAuth state token.
    # Match endpoints AND the documented enum values; an unknown state value,
    # another endpoint, or an unspecified endpoint remains conservatively scanned.
    path = urlsplit(endpoint or "").path.removeprefix("/api/v1")
    parts = path.strip("/").split("/")
    catalog_review = path == "/mcp-servers/discover" or (
        len(parts) in (3, 5)
        and parts[0] == "mcp-servers"
        and parts[2] == "definitions"
        and (len(parts) == 3 or parts[4] in ("approve", "reject"))
    )

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                field = (
                    RESPONSE_SECRET_FIELDS.get(key.lower())
                    if isinstance(key, str)
                    else None
                )
                if (
                    isinstance(key, str)
                    and key.lower() == "state"
                    and catalog_review
                    and isinstance(value, str)
                    and value in {"approved", "pending", "rejected", "unrecorded"}
                ):
                    continue
                if field and isinstance(value, str):
                    # record_secret fails the run on a value too short to search, rather
                    # than dropping it here and reporting a clean database.
                    record_secret(value, f"{field} from {where}" if where else field)
                elif isinstance(value, (dict, list)):
                    walk(value, where)
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    walk(payload, source)


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, ROOT / default))


GATEWAY_BIN = env_path("E2E_GATEWAY_BIN", "gateway/target/debug/lumewarp")
MOCK_BIN = env_path("E2E_MOCK_BIN", "bench/mock-upstream/target/release/mock-upstream")
# Server-level DSN (any database on the server, used to create/drop the test database).
PG_DSN = os.environ.get(
    "E2E_PG_URL", "postgresql://lumewarp:lumewarp@127.0.0.1:55432/lumewarp"
)


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


def wait_for(
    predicate: Callable[[], object], timeout: float, what: str, interval: float = 0.05
):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            value = predicate()
            if value:
                return value
        except Exception as err:  # noqa: BLE001 - polled condition may fail transiently
            last = err
        if time.monotonic() > deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting for {what} (last error: {last})"
            )
        time.sleep(interval)


def pg(dsn: str, sql: str, *args):
    async def run():
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


class Proc:
    def __init__(
        self, name: str, argv: list[str], env: dict[str, str], logdir: Path
    ) -> None:
        self.name = name
        self.argv = argv
        self.env = env
        self.log = logdir / f"{name}.log"
        self.popen: subprocess.Popen | None = None

    def start(self) -> None:
        assert self.popen is None or self.popen.poll() is not None
        run_dir = os.environ.get("E2E_RUN_DIR")
        with open(self.log, "ab") as out:
            self.popen = subprocess.Popen(
                self.argv,
                env=self.env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if run_dir:  # lets scripts/e2e-core.sh reap strays if pytest itself is killed
            with open(Path(run_dir) / "pids", "a") as pids:
                pids.write(f"{self.popen.pid}\n")

    def alive(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    def stop(self, timeout: float = 15.0) -> None:
        p = self.popen
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.popen.wait()

    def text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""


def gateway_host(env: dict[str, str]) -> str:
    """The host part of the id `gateway_id.rs` derives from HOSTNAME."""
    cleaned = "".join(
        char
        for char in env.get("HOSTNAME", "")
        if (char.isascii() and char.isalnum()) or char in "._-"
    )[:60]
    return cleaned or "local"


class Gateway:
    def __init__(
        self,
        stack: "Stack",
        state_dir: Path,
        token: str,
        tag: str,
        extra_env: dict[str, str] | None = None,
    ):
        self.stack = stack
        self.state_dir = state_dir
        self.port = free_port()
        self.admin_port = free_port()
        local = stack.dir / f"{tag}.yaml"
        local.write_text(
            f"server:\n  listen: '127.0.0.1:{self.port}'\n"
            f"  admin_listen: '127.0.0.1:{self.admin_port}'\n  shutdown_grace_ms: 1000\n"
        )
        env = {
            **stack.base_env,
            "E2E_GATEWAY_TOKEN": token,
            "LUMEWARP_ENCRYPTION_KEY": ENCRYPTION_KEY,
            "RUST_LOG": "info",
        }
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        env.update(extra_env or {})
        # The process generates `<host>-<8 hex>` at startup; tests find its row by this
        # prefix (HOSTNAME is test-controlled where a test needs one).
        self.prefix = f"{gateway_host(env)}-"
        self.proc = Proc(
            tag,
            [
                str(GATEWAY_BIN),
                "--control-plane",
                f"http://127.0.0.1:{stack.grpc_port}",
                "--gateway-token-env",
                "E2E_GATEWAY_TOKEN",
                "--state-dir",
                str(state_dir),
                "--scope",
                SCOPE,
                "--local-config",
                str(local),
            ],
            env,
            stack.dir,
        )
        self.proc.start()
        self.http = httpx.Client(timeout=10)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def status(self) -> dict | None:
        """This process's row: the connected one, else its most recent disconnected row."""
        rows = [
            row
            for row in self.stack.gateway_items()
            if row["gateway_id"].startswith(self.prefix)
        ]
        connected = [row for row in rows if row["connected"]]
        return max(connected or rows, key=lambda row: row["last_seen_at"] or "") if rows else None

    def wait_listening(self) -> None:
        def up():
            assert self.proc.alive(), f"gateway exited:\n{self.proc.text()}"
            return (
                httpx.get(
                    f"http://127.0.0.1:{self.admin_port}/healthz", timeout=1
                ).status_code
                == 200
            )

        wait_for(up, 15, "gateway admin listener")

    def readyz(self) -> int:
        return httpx.get(
            f"http://127.0.0.1:{self.admin_port}/readyz", timeout=2
        ).status_code

    def wait_ready(self, timeout: float = 15) -> None:
        wait_for(lambda: self.readyz() == 200, timeout, "gateway /readyz 200")

    def metrics(self) -> str:
        return httpx.get(f"http://127.0.0.1:{self.admin_port}/metrics", timeout=2).text

    def metric(self, name: str) -> float | None:
        for line in self.metrics().splitlines():
            if line.startswith(name + " "):
                return float(line.split()[1])
        return None

    def chat(
        self, key: str | None, *, stream: bool = False, model: str = "mock"
    ) -> httpx.Response:
        headers = {"authorization": f"Bearer {key}"} if key else {}
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            body["stream"] = True
        return self.http.post(
            self.url("/v1/chat/completions"), json=body, headers=headers
        )

    def stop(self) -> None:
        self.proc.stop()
        self.http.close()

    def drain_usage(self) -> None:
        """Join producers and the reporter via graceful shutdown before reconciling DB.

        main.rs stops HTTP first, then awaits ReporterHandle::shutdown, which flushes
        queued batches and waits for ingestion ACKs. Process exit is the completion
        signal: unlike a queue gauge, it also covers a batch already in flight.
        """
        self.stop()
        assert self.proc.popen.returncode == 0, self.proc.text()
        log = self.proc.text()
        assert "usage events not reported before shutdown" not in log, log
        assert "usage reporter did not stop before the deadline" not in log, log


class Stack:
    """One database, control plane, mock upstream; gateways are started by the tests."""

    def __init__(self) -> None:
        self.dir = Path(
            tempfile.mkdtemp(prefix="lumewarp-e2e-", dir=os.environ.get("E2E_RUN_DIR"))
        )
        # The run id in the name is what lets the finalizer find a database whose creator
        # forgot to register it, and tell it apart from another run's databases.
        self.db_name = f"{DB_PREFIX}{run_id()}_{uuid.uuid4().hex[:12]}"
        self.http_port = free_port()
        self.grpc_port = free_port()
        self.metrics_port = free_port()
        self.mock_port = free_port()
        self.db_created = False
        self.gateways: list[Gateway] = []
        self.extra_mocks: list[Proc] = []
        self.api_url = f"http://127.0.0.1:{self.http_port}/api/v1"
        base, _, _ = PG_DSN.rpartition("/")
        self.dsn = f"{base}/{self.db_name}"
        self.sa_url = self.dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        self.base_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("LUMEWARP_", "OTEL_"))
        }
        self.cp_env = {
            **self.base_env,
            "LUMEWARP_DATABASE_URL": self.sa_url,
            "LUMEWARP_BREAK_GLASS_PASSWORD": BREAK_GLASS_PASSWORD,
            "LUMEWARP_DEV_MODE": "true",
            "LUMEWARP_SESSION_COOKIE_SECURE": "false",
            "LUMEWARP_SECRET_KEY": "e2e-lumewarp-secret-key-0123456789abcdef",
            "LUMEWARP_ENCRYPTION_KEY": ENCRYPTION_KEY,
            "LUMEWARP_SCOPE": SCOPE,
            "LUMEWARP_HTTP_BIND": f"127.0.0.1:{self.http_port}",
            "LUMEWARP_GRPC_BIND": f"127.0.0.1:{self.grpc_port}",
            "LUMEWARP_METRICS_BIND": f"127.0.0.1:{self.metrics_port}",
        }
        cp = [sys.executable, "-m", "lumewarp_control_plane"]
        self.cp_cmd = cp
        self.cp = Proc("control-plane", [*cp, "serve"], self.cp_env, self.dir)
        self.mock = Proc(
            "mock-upstream",
            [
                str(MOCK_BIN),
                "--listen",
                f"127.0.0.1:{self.mock_port}",
                "--latency-ms",
                "5",
                "--stream-chunks",
                "4",
                "--chunk-interval-ms",
                "2",
            ],
            self.base_env,
            self.dir,
        )
        self.admin = httpx.Client(timeout=15)
        self.gateway_token = ""
        # Every stack has one org for the tests' teams (there is no implicit default).
        self.org_id = ""

    # lifecycle ---------------------------------------------------------------------

    def setup(self) -> None:
        if run_dir := os.environ.get("E2E_RUN_DIR"):
            with open(Path(run_dir) / "dbs", "a") as dbs:
                dbs.write(f"{self.db_name}\n")
            (Path(run_dir) / RUN_ID_FILE).write_text(run_id() + "\n")
        # A credential that only reaches the control plane's environment (an OIDC client
        # secret, the secret key that seals the login cookie, ...) is still a database
        # leak if it is stored there, so record those values too. Non-secret names (a TTL,
        # a boolean) are excluded by name before the length rule applies.
        record_env_secrets(self.cp_env)
        pg(PG_DSN, f'CREATE DATABASE "{self.db_name}"')
        self.db_created = True
        self._run_cli("migrate")
        out = self._run_cli("bootstrap", "--name", "e2e", "--scope", SCOPE)
        self.gateway_token = out.strip().splitlines()[-1]
        assert self.gateway_token.startswith("lwg-"), out
        record_secret(self.gateway_token, "gateway token")
        record_secret(BREAK_GLASS_PASSWORD, "break-glass password")
        record_secret(ENCRYPTION_KEY, "LUMEWARP_ENCRYPTION_KEY")
        self.mock.start()
        wait_for(
            lambda: (
                httpx.get(
                    f"http://127.0.0.1:{self.mock_port}/health", timeout=1
                ).status_code
                == 200
            ),
            10,
            "mock upstream",
        )
        self.start_cp()
        self.org_id = self.ok("POST", "/orgs", json={"name": "e2e"})["id"]

    def _run_cli(self, *args: str) -> str:
        res = subprocess.run(
            [*self.cp_cmd, *args],
            check=False,
            env=self.cp_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 0, f"{args} failed:\n{res.stdout}\n{res.stderr}"
        return res.stdout

    def start_cp(self) -> None:
        self.cp.start()

        def up():
            assert self.cp.alive(), f"control plane exited:\n{self.cp.text()}"
            return (
                httpx.get(
                    f"http://127.0.0.1:{self.http_port}/readyz", timeout=1
                ).status_code
                == 200
            )

        wait_for(up, 30, "control plane /readyz")
        response = self.admin.post(
            self.api_url + "/session/break-glass",
            json={"password": BREAK_GLASS_PASSWORD},
        )
        assert response.status_code == 204, response.text
        info = self.admin.get(self.api_url + "/session").json()
        self.admin.headers["X-Lumewarp-CSRF"] = info["csrf_token"]
        from lumewarp_control_plane.auth.sessions import COOKIE_NAME

        record_secret(self.admin.cookies.get(COOKIE_NAME), "Lumewarp session cookie")
        record_response_secrets(info, "GET /session", endpoint="/session")

    def start_mock(self, tag: str, *args: str, latency_ms: int = 5) -> int:
        """Start another mock upstream (e.g. with fault injection); returns its port.

        `latency_ms` replaces the 5 ms default so a test can hold a request open.
        """
        port = free_port()
        proc = Proc(
            tag,
            [
                str(MOCK_BIN),
                "--listen",
                f"127.0.0.1:{port}",
                "--latency-ms",
                str(latency_ms),
                *args,
            ],
            self.base_env,
            self.dir,
        )
        self.extra_mocks.append(proc)
        proc.start()
        wait_for(
            lambda: (
                httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code
                == 200
            ),
            10,
            f"mock upstream {tag}",
        )
        return port

    def stop_cp(self) -> None:
        self.cp.stop()

    def start_gateway(
        self,
        state_dir: Path | None = None,
        tag: str | None = None,
        *,
        wait_ready: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> Gateway:
        tag = tag or f"gateway-{len(self.gateways)}"
        gw = Gateway(
            self,
            state_dir or self.dir / "state",
            self.gateway_token,
            tag,
            extra_env,
        )
        self.gateways.append(gw)
        gw.wait_listening()
        if wait_ready:
            gw.wait_ready()
        return gw

    def teardown(self) -> None:
        for gw in self.gateways:
            gw.stop()
        self.cp.stop()
        self.mock.stop()
        for proc in self.extra_mocks:
            proc.stop()
        self.admin.close()
        # This database is dropped per test/stack, so the pg_dump scan has to run here:
        # after the run, no test database is left to scan. The scripts' (and a direct
        # pytest session's) finalizer verifies that every created database has a scan
        # result and rescans leftovers. Scanning is the default; only the documented
        # opt-out turns it off.
        leak = None
        try:
            if self.db_created and not scan_disabled():
                leak = self._scan_secrets()
        except Exception as err:  # noqa: BLE001 - still drop, but fail the test
            leak = f"{self.db_name}: secret scan failed: {type(err).__name__}: {err}"
        try:
            pg(PG_DSN, f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        except Exception as err:  # noqa: BLE001
            print(f"warning: could not drop {self.db_name}: {err}", file=sys.stderr)
        if leak is not None:
            print(f"secret scan: {leak}", file=sys.stderr)
            raise AssertionError(leak)

    def _scan_secrets(self) -> str | None:
        """Dump before the drop; a report string when a plaintext credential is present."""
        log = os.environ.get(SECRET_LOG_ENV)
        report = os.environ.get(SCAN_REPORT_ENV)
        if not log or not report:
            raise RuntimeError(f"{SECRET_LOG_ENV} and {SCAN_REPORT_ENV} must be set")
        from secret_scan import append_result, describe, scan_database

        result = scan_database(self.dsn, log)
        append_result(report, result)
        if result["hits"]:
            return f"{self.db_name}: {describe(result['hits'])}"
        return None

    def logs(self) -> str:
        parts = []
        for log in sorted(self.dir.glob("*.log")):
            lines = [
                ln
                for ln in log.read_text(errors="replace").splitlines()
                if ' HTTP/1.1" 200' not in ln
            ]
            parts.append(f"===== {log.name} =====\n" + "\n".join(lines)[-6000:])
        return "\n".join(parts)

    def cleanup_dir(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # REST --------------------------------------------------------------------------

    def api(
        self, method: str, path: str, token: str | None = None, **kwargs
    ) -> httpx.Response:
        response = self.admin.request(
            method,
            self.api_url + path,
            headers={"authorization": f"Bearer {token}"} if token is not None else {},
            **kwargs,
        )
        # Every credential an endpoint issues is recorded here, before the caller sees it.
        record_response_secrets(response, f"{method.upper()} {path}", endpoint=path)
        return response

    def ok(self, method: str, path: str, token: str | None = None, **kwargs) -> dict:
        r = self.api(method, path, token, **kwargs)
        assert r.status_code in (200, 201), (method, path, r.status_code, r.text)
        return r.json()

    def seed(
        self,
        *,
        priced: bool = True,
        team_budget: int | None = None,
        owner_budget: int | None = None,
        owner_kind: str = "service_account",
    ) -> "Seeded":
        up = self.ok(
            "POST",
            "/upstreams",
            json={
                "name": "mock",
                "kind": "openai_compatible",
                "base_url": f"http://127.0.0.1:{self.mock_port}/v1",
            },
        )
        dep = {"name": "mock-a", "upstream_id": up["id"], "upstream_model": "mock", "model_type": "llm"}
        if priced:
            dep |= {
                "price_input_nano_usd_per_mtok": 1_000_000_000,
                "price_output_nano_usd_per_mtok": 2_000_000_000,
            }
        dep = self.ok("POST", "/models", json=dep)
        self.ok(
            "POST",
            "/routers",
            json={"name": "mock", "members": [{"model_id": dep["id"]}]},
        )
        body = {"name": "ml", "model_access": "all"}
        if team_budget is not None:
            body["budget_nano_usd"] = team_budget
        body["org_id"] = self.org_id
        team = self.ok("POST", "/teams", json=body)
        body = {}
        if owner_budget is not None:
            body["budget_nano_usd"] = owner_budget
        token = None
        if owner_kind == "user":
            token = self.create_pat("owner@e2e.test", team["id"], "member")
            owner_id = self.ok("GET", "/users/me", token)["user_id"]
            self.ok("PATCH", f"/teams/{team['id']}/members/{owner_id}", json=body)
            key_body = {}
        else:
            assert owner_kind == "service_account"
            owner_id = self.ok(
                "POST",
                f"/teams/{team['id']}/service-accounts",
                json={"name": "chat", **body},
            )["id"]
            key_body = {"service_account_id": owner_id}
        keys = {}
        for name in ("a", "b"):
            created = self.ok(
                "POST",
                f"/teams/{team['id']}/keys",
                token,
                json={"name": f"key-{name}", **key_body},
            )
            assert created["key"]["owner_kind"] == owner_kind
            keys[name] = Key(created["key"]["id"], created["secret"])
        self.wait_key_count(2)
        return Seeded(team["id"], owner_id, keys["a"], keys["b"])

    def create_pat(self, email: str, team_id: str, role: str) -> str:
        """User + membership via REST; the PAT row is inserted directly (the break-glass
        principal cannot own tokens)."""
        from lumewarp_control_plane.security import SecretKind, generate_secret

        user = self.ok("POST", "/users", json={"email": email})
        self.ok(
            "PUT",
            f"/teams/{team_id}/members/{user['id']}",
            json={"role": role},
        )
        new = generate_secret(SecretKind.PAT)
        record_secret(new.secret, "personal access token")
        pg(
            self.dsn,
            "INSERT INTO personal_access_tokens (id, user_id, name, token_hash, display_prefix) "
            "VALUES ($1, $2, 'e2e', $3, $4)",
            uuid.uuid4(),
            uuid.UUID(user["id"]),
            new.hash,
            new.display_prefix,
        )
        return new.secret

    def usage_rows(self) -> list[asyncpg.Record]:
        return pg(self.dsn, "SELECT * FROM usage_events ORDER BY id")

    def gateway_items(self) -> list[dict]:
        """One row per gateway process, connected first (the flat fleet API)."""
        return self.ok("GET", "/gateways", params={"limit": 500})["items"]

    def gateway_status(self, gateway: Gateway | None = None) -> dict | None:
        """The status row of one gateway, or of the most recently started one."""
        if gateway is not None:
            return gateway.status()
        if self.gateways:
            return self.gateways[-1].status()
        rows = self.gateway_items()
        return rows[0] if rows else None

    def wait_key_count(self, count: int) -> None:
        """Publishing is asynchronous (outbox + compiler loop): wait until a version has them."""
        wait_for(
            lambda: (
                self.ok("GET", "/config/versions", params={"limit": 1})["items"][0][
                    "key_count"
                ]
                == count
            ),
            10,
            f"config version with {count} keys",
        )

    def current_version(self) -> int:
        return self.ok("GET", "/config/versions", params={"limit": 1})["items"][0][
            "version"
        ]


class Key:
    def __init__(self, id: str, secret: str) -> None:
        self.id = id
        self.secret = secret


class Seeded:
    def __init__(self, team_id: str, owner_id: str, a: Key, b: Key) -> None:
        self.team_id, self.owner_id, self.a, self.b = team_id, owner_id, a, b

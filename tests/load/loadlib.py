"""Load-e2e helpers on top of the P1 e2e harness (tests/e2e/stack.py): a multi-team fleet of
keys, a Locust run driven from a key-pool file, a probe client whose requests are recorded
like Locust's, usage accounting against Postgres, and the per-scenario report.
"""

import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from stack import ROOT, SCOPE, Gateway, Proc, Stack, free_port, pg, wait_for


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


LOCUST = os.environ.get("LOAD_LOCUST", "uvx --from locust==2.32.4 locust").split()
LOCUSTFILE = Path(__file__).with_name("locustfile.py")
USERS = env_int("LOAD_USERS", 40)
USER_RPS = env_float("LOAD_USER_RPS", 10)
PROCESSES = env_int("LOAD_PROCESSES", 4)
STREAM_SHARE = env_float("LOAD_STREAM_SHARE", 0.3)
SAT_P99_S = env_float("LOAD_SAT_P99_S", 0.1)
SAT_STREAM_P99_S = env_float("LOAD_SAT_STREAM_P99_S", 0.25)
RESULTS = Path(os.environ.get("LOAD_RESULTS_DIR", ROOT / "tests/load/results/local"))

# nano-USD per million tokens; multiples of 1e6 so per-token cost is exact.
PRICES = {
    "mock-a": (1_000_000_000, 2_000_000_000),
    "mock-b": (3_000_000_000, 4_000_000_000),
}


def cost(model: str, prompt: int, completion: int) -> int:
    price_in, price_out = PRICES[model]
    return (prompt * price_in + completion * price_out + 500_000) // 1_000_000


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))]


def dist(values: list[float], scale: float = 1.0, digits: int = 1) -> dict:
    """p50/p90/p99/max (scaled, e.g. seconds -> ms)."""
    if not values:
        return {"n": 0}
    out = {"n": len(values)}
    for name, p in (("p50", 50), ("p90", 90), ("p99", 99), ("max", 100)):
        out[name] = round(pct(values, p) * scale, digits)
    return out


# --------------------------------------------------------------------------- fleet


class Fleet:
    """Teams x mixed service-account/member owners x keys behind one priced mock router."""

    def __init__(
        self,
        stack: Stack,
        *,
        teams: int = 6,
        owners_per_team: int = 2,
        keys_per_owner: int = 5,
        budgets: dict[int, int] | None = None,
        team_budgets: dict[int, int] | None = None,
    ) -> None:
        self.stack = stack
        self.upstream_id = stack.ok(
            "POST",
            "/upstreams",
            json={
                "name": "mock",
                "kind": "openai_compatible",
                "base_url": f"http://127.0.0.1:{stack.mock_port}/v1",
            },
        )["id"]
        self.models = {"mock-a": self.create_model("mock-a")}
        self.router_id = stack.ok(
            "POST",
            "/routers",
            json={"name": "mock", "members": [{"model_id": self.models["mock-a"]}]},
        )["id"]
        self.teams: list[dict] = []
        self.owners: list[dict] = []
        self.keys: list[dict] = []
        budgets = budgets or {}
        team_budgets = team_budgets or {}
        for t in range(teams):
            body = {"name": f"team-{t}"}
            if t in team_budgets:
                body["budget_nano_usd"] = team_budgets[t]
            team = stack.ok("POST", "/teams", json=body)
            self.teams.append(team)
            for owner_index in range(owners_per_team):
                body = {}
                if (index := len(self.owners)) in budgets:
                    body["budget_nano_usd"] = budgets[index]
                if owner_index % 2:
                    token = stack.create_pat(f"owner-{index}@load.test", team["id"], "member")
                    user_id = stack.ok("GET", "/users/me", token)["user_id"]
                    owner = stack.ok("PATCH", f"/teams/{team['id']}/members/{user_id}", json=body)
                    owner |= {
                        "id": user_id,
                        "owner_kind": "user",
                        "token": token,
                        "policy_path": f"/teams/{team['id']}/members/{user_id}",
                        "scope_type": "member",
                        "scope_id": str(uuid.uuid5(uuid.UUID(team["id"]), user_id)),
                    }
                else:
                    owner = stack.ok(
                        "POST",
                        f"/teams/{team['id']}/service-accounts",
                        json={"name": f"owner-{owner_index}", **body},
                    )
                    owner |= {
                        "owner_kind": "service_account",
                        "token": None,
                        "policy_path": f"/service-accounts/{owner['id']}",
                        "scope_type": "service_account",
                        "scope_id": owner["id"],
                    }
                self.owners.append(owner)
                for _ in range(keys_per_owner):
                    self.keys.append(self.create_key(owner["id"]))
        self.wait_published()

    def create_model(self, name: str) -> str:
        price_in, price_out = PRICES[name]
        return self.stack.ok(
            "POST",
            "/models",
            json={
                "name": name,
                "upstream_id": self.upstream_id,
                "upstream_model": "mock",
                "price_input_nano_usd_per_mtok": price_in,
                "price_output_nano_usd_per_mtok": price_out,
            },
        )["id"]

    def create_key(self, owner_id: str) -> dict:
        owner = next(o for o in self.owners if o["id"] == owner_id)
        body = {"name": f"key-{len(self.keys)}"}
        if owner["owner_kind"] == "service_account":
            body["service_account_id"] = owner_id
        created = self.stack.ok(
            "POST",
            f"/teams/{owner['team_id']}/keys",
            owner["token"],
            json=body,
        )
        assert created["key"]["owner_kind"] == owner["owner_kind"]
        return {
            "id": created["key"]["id"],
            "secret": created["secret"],
            "owner_id": owner_id,
            "owner_kind": owner["owner_kind"],
            "team_id": owner["team_id"],
        }

    def wait_published(self) -> None:
        self.stack.wait_key_count(len(self.keys))

    def owner_keys(self, index: int) -> list[dict]:
        owner_id = self.owners[index]["id"]
        return [k for k in self.keys if k["owner_id"] == owner_id]


def wait_acked(stack: Stack, timeout: float = 15) -> int:
    """Until every source mutation committed so far is compiled (publishing is async: outbox
    + compiler loop) and the gateway ACKed the resulting latest version; returns it."""
    revision = pg(stack.dsn, "SELECT source_revision FROM scopes WHERE name = $1", SCOPE)[0][
        "source_revision"
    ]
    wait_for(
        lambda: (
            not pg(
                stack.dsn,
                "SELECT 1 FROM config_outbox WHERE scope = $1 AND status = 'pending'"
                " AND source_revision <= $2 LIMIT 1",
                SCOPE,
                revision,
            )
        ),
        timeout,
        f"source revision {revision} compiled",
        interval=0.02,
    )
    seen = {}

    def acked():
        current = stack.current_version()
        status = stack.gateway_status() or {}
        seen["v"] = current
        return status.get("connected") and status.get("acked_version") == current

    wait_for(acked, timeout, "gateway ACKed the current version", interval=0.02)
    return seen["v"]


# --------------------------------------------------------------------------- clients


class Records:
    """Thread-safe list of request records in the locustfile's format."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.lock = threading.Lock()

    def add(self, record: dict) -> None:
        with self.lock:
            self.items.append(record)


class Probe:
    """Orchestrator-side requests through the gateway. They count for usage accounting
    exactly like Locust's, so every one is recorded."""

    def __init__(self, gw: Gateway, records: Records) -> None:
        self.gw = gw
        self.records = records
        self.http = httpx.Client(timeout=10)

    def chat(self, key: dict) -> tuple[int, str | None]:
        started = time.time()
        t0 = time.perf_counter()
        record = {"t": started, "k": key["id"], "s": False, "probe": True}
        try:
            r = self.http.post(
                self.gw.url("/v1/chat/completions"),
                json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                headers={"authorization": f"Bearer {key['secret']}"},
            )
            record["st"] = r.status_code
            code = None
            if r.status_code != 200:
                try:
                    code = r.json()["error"]["code"]
                except Exception:  # noqa: BLE001
                    code = "?"
                record["c"] = code
            return r.status_code, code
        except httpx.HTTPError as err:
            record["st"] = 0
            record["e"] = str(err)[:200]
            return 0, None
        finally:
            record["d"] = time.perf_counter() - t0
            self.records.add(record)

    def until(self, key: dict, want, timeout: float, interval: float = 0.02) -> float | None:
        """Polls until want(status, code); returns the wall time it first held, or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, code = self.chat(key)
            if want(status, code):
                return time.time()
            time.sleep(interval)
        return None

    def close(self) -> None:
        self.http.close()


class LoadRun:
    """One headless Locust run against a gateway; the key pool can change while it runs."""

    def __init__(self, stack: Stack, gw: Gateway, name: str, keys: list[dict], seconds: float):
        self.dir = stack.dir / f"locust-{name}"
        self.dir.mkdir()
        self.pool = self.dir / "pool.json"
        self.seconds = seconds
        self.set_keys(keys)
        env = {
            **stack.base_env,
            "LOAD_POOL": str(self.pool),
            "LOAD_OUT": str(self.dir),
            "LOAD_USER_RPS": str(USER_RPS),
            "LOAD_STREAM_SHARE": str(STREAM_SHARE),
        }
        argv = [
            *LOCUST,
            "-f",
            str(LOCUSTFILE),
            "--headless",
            "--users",
            str(USERS),
            "--spawn-rate",
            str(USERS),
            "--run-time",
            f"{int(seconds)}s",
            "--stop-timeout",
            "15",
            "--host",
            f"http://127.0.0.1:{gw.port}",
            "--csv",
            str(self.dir / "stats"),
            "--only-summary",
            "--loglevel",
            "WARNING",
            "--exit-code-on-error",
            "0",
        ]
        if PROCESSES > 1:
            # The master/worker channel defaults to *:5557; another Locust on the box (bench)
            # would cross-report into this run.
            port = str(free_port())
            argv += ["--processes", str(PROCESSES), "--master-bind-host", "127.0.0.1"]
            argv += ["--master-bind-port", port, "--master-host", "127.0.0.1"]
            argv += ["--master-port", port]
        self.proc = Proc(f"locust-{name}", argv, env, stack.dir)
        self.started = 0.0

    def set_keys(self, keys: list[dict]) -> None:
        tmp = self.pool.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": [{"id": k["id"], "secret": k["secret"]} for k in keys]}))
        os.replace(tmp, self.pool)

    def start(self) -> None:
        self.proc.start()

        def first_records():
            assert self.proc.alive(), f"locust exited early:\n{self.proc.text()[-3000:]}"
            return any(p.stat().st_size > 0 for p in self.dir.glob("requests-*.jsonl"))

        # The first uvx run may have to install locust.
        wait_for(first_records, 180, "first Locust requests", interval=0.1)
        self.started = time.time()

    def elapsed(self) -> float:
        return time.time() - self.started

    def sleep_until(self, offset: float) -> None:
        time.sleep(max(0.0, offset - self.elapsed()))

    def wait(self) -> list[dict]:
        assert self.proc.popen is not None
        try:
            self.proc.popen.wait(self.seconds + 120)
        except Exception:
            self.proc.kill()
            raise
        keep = RESULTS / self.dir.name
        keep.mkdir(parents=True, exist_ok=True)
        for path in [
            *self.dir.glob("stats_*.csv"),
            self.dir / "final-stats.json",
            self.proc.log,
        ]:
            if path.exists():
                shutil.copy(path, keep / path.name)
        records = []
        for path in sorted(self.dir.glob("requests-*.jsonl")):
            for line in path.read_text().splitlines():
                if line:
                    records.append(json.loads(line))
        assert self.proc.popen.returncode == 0, self.proc.text()
        # The quit event writes this after all users and workers have drained.
        count = json.loads((self.dir / "final-stats.json").read_text())["requests"]
        (keep / "record-count.json").write_text(
            json.dumps({"locust_requests": count, "records": len(records)}) + "\n"
        )
        assert count == len(records), (
            f"locust counted {count} requests, records have {len(records)}"
        )
        return records


# --------------------------------------------------------------------------- analysis


def summarize(records: list[dict], seconds: float) -> dict:
    statuses = Counter(r["st"] for r in records)
    ok = [r for r in records if r["st"] == 200 and not r.get("probe")]
    t = [r["t"] for r in ok]
    span = (max(t) - min(t)) if len(t) > 1 else seconds
    return {
        "requests": len(records),
        "statuses": {str(k): v for k, v in sorted(statuses.items())},
        "rps_ok": round(len(ok) / span, 1) if span else None,
        "latency_ms": dist([r["d"] for r in ok if not r["s"]], 1000),
        "stream_latency_ms": dist([r["d"] for r in ok if r["s"]], 1000),
    }


def hard_errors(records: list[dict], allow_conn: tuple[float, float] | None = None) -> list[dict]:
    """5xx, connection errors (outside `allow_conn` wall-time window), malformed bodies and
    any other unexpected status."""
    bad = []
    for r in records:
        st = r["st"]
        if st == 0 and allow_conn and allow_conn[0] <= r["t"] <= allow_conn[1]:
            continue
        if st == 0 or st >= 500 or st not in (200, 401, 403, 429) or r.get("p"):
            bad.append(r)
    return bad


def load_window(
    run: LoadRun, elapsed: float, reserve: float, records: list[dict], *, step_started: float
) -> dict:
    """A late orchestration step is diagnostic only with positive generator CPU evidence
    and healthy gateway latency. Product latency and unclassified overruns still fail."""
    late = elapsed >= run.seconds - reserve
    step_ended = run.started + elapsed
    generator_cpu_warning = False
    warning_before_step_at = None
    # Locust's default formatter uses local time, as does datetime.timestamp().
    for match in re.finditer(
        r"^\[([^\]]+)\] \S+/WARNING/[^\n]*CPU usage above 90%",
        run.proc.text(),
        re.MULTILINE | re.IGNORECASE,
    ):
        try:
            timestamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").timestamp()
        except ValueError:
            continue
        if timestamp < step_started:
            if warning_before_step_at is None or timestamp > warning_before_step_at:
                warning_before_step_at = timestamp
        elif timestamp <= step_ended:
            generator_cpu_warning = True
    requests = [r for r in records if not r.get("probe") and step_started <= r["t"] <= step_ended]
    latency = pct([r["d"] for r in requests if r["st"] == 200 and not r["s"]], 99)
    stream_latency = pct([r["d"] for r in requests if r["st"] == 200 and r["s"]], 99)
    generator_saturation = (
        elapsed <= run.seconds
        and generator_cpu_warning
        and latency is not None
        and latency < SAT_P99_S
        and stream_latency is not None
        and stream_latency < SAT_STREAM_P99_S
        and not hard_errors(requests)
    )
    return {
        "step_started_at": step_started,
        "step_ended_at": step_ended,
        "step_elapsed_s": round(elapsed, 3),
        "load_seconds": run.seconds,
        "reserve_s": reserve,
        "late": late,
        "generator_cpu_warning": generator_cpu_warning,
        "generator_cpu_warning_before_step": warning_before_step_at is not None,
        "generator_cpu_warning_before_step_at": warning_before_step_at,
        "gateway_p99_s": latency,
        "gateway_stream_p99_s": stream_latency,
        "p99_limit_s": SAT_P99_S,
        "stream_p99_limit_s": SAT_STREAM_P99_S,
        "diagnostic": "generator saturation" if late and generator_saturation else None,
        "ok": elapsed <= run.seconds and (not late or generator_saturation),
    }


def usage_counts(stack: Stack) -> Counter:
    rows = pg(
        stack.dsn,
        "SELECT key_id::text AS k, status, stream, count(*) AS n FROM usage_events"
        " GROUP BY 1, 2, 3",
    )
    return Counter({(r["k"], r["status"], r["stream"]): r["n"] for r in rows})


def settle_usage(stack: Stack, minimum: int, timeout: float = 90) -> int:
    """Waits until at least `minimum` usage rows landed and the count stopped moving."""
    count = lambda: pg(stack.dsn, "SELECT count(*) AS n FROM usage_events")[0]["n"]  # noqa: E731
    wait_for(lambda: count() >= minimum, timeout, f">= {minimum} usage rows", interval=0.25)
    last, stable_since = count(), time.monotonic()
    while time.monotonic() - stable_since < 3:
        time.sleep(0.25)
        now = count()
        if now != last:
            last, stable_since = now, time.monotonic()
    return last


def accounting(stack: Stack, records: list[dict]) -> dict:
    """Client-observed vs Postgres. 200s per (key, stream); rejections per (key, status):
    the gateway rejects at auth before reading the body, so those rows carry stream=false.
    200 and 429 must match exactly (every one comes from a known key); 401/403 rows may be
    fewer, since a key the gateway does not know (revoked and dropped from the snapshot, or
    not yet published) produces no event."""

    def slot(key: str, status: int, stream: bool) -> tuple:
        return (key, status, stream if status == 200 else None)

    client = Counter(slot(r["k"], r["st"], r["s"]) for r in records if r["st"] != 0)
    db: Counter = Counter()
    for (key, status, stream), n in usage_counts(stack).items():
        db[slot(key, status, stream)] += n
    mismatches = []
    for k in sorted(set(client) | set(db), key=str):
        c, d = client.get(k, 0), db.get(k, 0)
        exact = k[1] not in (401, 403)
        if (exact and c != d) or (not exact and d > c):
            mismatches.append({"key": k[0], "status": k[1], "stream": k[2], "client": c, "db": d})
    dupes = pg(stack.dsn, "SELECT count(*) - count(DISTINCT request_id) AS n FROM usage_events")[0][
        "n"
    ]
    return {
        "client_200": sum(n for (_, st, _), n in client.items() if st == 200),
        "db_200": sum(n for (_, st, _), n in db.items() if st == 200),
        "client_429": sum(n for (_, st, _), n in client.items() if st == 429),
        "db_429": sum(n for (_, st, _), n in db.items() if st == 429),
        "db_rows": sum(db.values()),
        "duplicate_request_ids": dupes,
        "mismatches": mismatches,
    }


def cost_check(stack: Stack) -> dict:
    """Per team and owner: token costs == event costs == usage totals."""
    rows = pg(
        stack.dsn,
        "SELECT team_id::text AS team, owner_kind, owner_id::text AS owner,"
        " model, prompt_tokens, completion_tokens,"
        " cost_nano_usd, cost_known FROM usage_events WHERE status = 200",
    )
    expected: Counter = Counter()
    recorded: Counter = Counter()
    bad_rows = 0
    for r in rows:
        want = cost(r["model"], r["prompt_tokens"], r["completion_tokens"])
        if not r["cost_known"] or r["cost_nano_usd"] != want or r["prompt_tokens"] <= 0:
            bad_rows += 1
        owner_scope = (
            str(uuid.uuid5(uuid.UUID(r["team"]), r["owner"]))
            if r["owner_kind"] == "user"
            else r["owner"]
        )
        for scope in (
            ("team", r["team"]),
            ("member" if r["owner_kind"] == "user" else "service_account", owner_scope),
        ):
            expected[scope] += want
            recorded[scope] += r["cost_nano_usd"]
    totals = pg(
        stack.dsn,
        "SELECT scope_type, scope_id::text AS id, sum(cost_nano_usd) AS c FROM usage_totals"
        " GROUP BY 1, 2",
    )
    totals_map = {(t["scope_type"], t["id"]): int(t["c"]) for t in totals}
    scopes = sorted(set(expected) | set(totals_map))
    wrong = [
        {
            "scope_type": p[0],
            "scope_id": p[1],
            "tokens_x_price": expected[p],
            "events": recorded[p],
            "totals": totals_map.get(p, 0),
        }
        for p in scopes
        if not (expected[p] == recorded[p] == totals_map.get(p, 0))
    ]
    return {
        "scopes": len(scopes),
        "rows_with_wrong_cost": bad_rows,
        "scopes_mismatched": wrong,
        "total_nano_usd": sum(v for (kind, _), v in expected.items() if kind == "team"),
    }


def stream_resets(gw: Gateway) -> int:
    """Config streams the gateway saw end (each one is a reconnect)."""
    return gw.proc.text().count("config stream ended")


def gateway_counter(gw: Gateway, name: str) -> float:
    value = gw.metric(name)
    return 0.0 if value is None else value


def write_report(name: str, data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2, default=str) + "\n")

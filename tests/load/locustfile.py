"""Locust workload for the control-plane-mode load e2e (tests/load).

Runs in its own Python (uvx locust==2.32.4), never imports the harness; loadpacing.py next to it
is stdlib only (Locust puts this directory on sys.path while it loads the file). The orchestrator
(test_load.py) owns the key pool: a JSON file it rewrites atomically while the run goes on
(revoked keys stay in it, new keys are appended once the gateway accepts them). Every
request is appended to `<LOAD_OUT>/requests-<pid>.jsonl` so the orchestrator can compare
what clients saw with what landed in Postgres, key by key. Record times are CLOCK_MONOTONIC, which
every process on the machine shares and which a wall clock step does not move. So are the CPU
warnings in `<LOAD_OUT>/cpu-warnings-<pid>.jsonl`.

Environment: LOAD_POOL, LOAD_OUT, LOAD_USER_RPS (per user), LOAD_STREAM_SHARE.
"""

import json
import logging
import os
import random
import time

from loadpacing import CpuWarningRecorder, Pacer
from locust import FastHttpUser, events, task
from locust.runners import WorkerRunner

POOL = os.environ["LOAD_POOL"]
OUT = os.environ["LOAD_OUT"]
USER_RPS = float(os.environ.get("LOAD_USER_RPS", "10"))
STREAM_SHARE = float(os.environ.get("LOAD_STREAM_SHARE", "0.3"))
EXPECTED_4XX = {401, 403, 429}


class _Pool:
    def __init__(self) -> None:
        self.keys: list[dict] = []
        self.mtime = 0.0
        self.checked = 0.0

    def get(self) -> list[dict]:
        now = time.monotonic()
        if now - self.checked > 0.2 or not self.keys:
            self.checked = now
            try:
                mtime = os.stat(POOL).st_mtime
                if mtime != self.mtime:
                    with open(POOL) as f:
                        self.keys = json.load(f)["keys"]
                    self.mtime = mtime
            except (OSError, ValueError):
                pass  # mid-rename or not written yet: keep the previous pool
        return self.keys


class _Log:
    """Per-process JSONL writer; opened lazily so forked worker processes get their own."""

    def __init__(self) -> None:
        self.pid = 0
        self.file = None
        self.flushed = 0.0

    def write(self, record: dict) -> None:
        if self.pid != os.getpid():
            self.pid = os.getpid()
            self.file = open(os.path.join(OUT, f"requests-{self.pid}.jsonl"), "a")
        self.file.write(json.dumps(record, separators=(",", ":")) + "\n")
        now = time.monotonic()
        if now - self.flushed > 0.5:
            self.file.flush()
            self.flushed = now

    def close(self) -> None:
        if self.file is not None:
            self.file.flush()
            self.file.close()
            self.file = None


pool = _Pool()
log = _Log()


@events.quitting.add_listener
def _flush(**_kwargs) -> None:
    log.close()


@events.init.add_listener
def _record_cpu_warnings(**_kwargs) -> None:
    # init fires in each process, after the fork and after Locust configured logging.
    logging.getLogger("locust.runners").addHandler(CpuWarningRecorder(OUT))


@events.init.add_listener
def _final_stats(environment, **_kwargs) -> None:
    if isinstance(environment.runner, WorkerRunner):
        return

    @environment.events.quit.add_listener
    def save(exit_code, **_kwargs) -> None:
        # quit fires after runner.quit drains users and collects the workers' last stats.
        # Periodic CSV snapshots can omit the final requests, especially in short runs.
        with open(os.path.join(OUT, "final-stats.json"), "w") as out:
            json.dump({"requests": environment.stats.total.num_requests}, out)


def _sse_problem(text: str) -> str | None:
    """None when the body is a well-formed SSE stream ending in [DONE]."""
    events_ = [e for e in text.split("\n\n") if e.strip()]
    if not events_ or events_[-1].strip() != "data: [DONE]":
        return "stream does not end with [DONE]"
    for event in events_[:-1]:
        for line in event.splitlines():
            if not line.startswith("data: "):
                return f"bad SSE line {line[:60]!r}"
            data = json.loads(line[6:])
            if data.get("usage") and not data.get("choices"):
                return "usage-only chunk leaked to a client that did not ask for it"
    return None


class GatewayUser(FastHttpUser):
    # A connection error must surface as status 0, not be retried by the client.
    max_retries = 0
    network_timeout = 30.0
    connection_timeout = 5.0

    def __init__(self, environment) -> None:
        super().__init__(environment)
        self.pacer = Pacer(1 / USER_RPS)

    def wait_time(self) -> float:
        return self.pacer.next()

    @task
    def chat(self) -> None:
        keys = pool.get()
        if not keys:
            time.sleep(0.1)
            return
        # `w` keeps a rotated key's total sampling weight the same while both of its
        # secrets are in the pool (0.5 each); `g` records which generation served.
        if len(keys) == 1:
            key = keys[0]
        else:
            key = random.choices(keys, weights=[float(k.get("w", 1)) for k in keys], k=1)[0]
        stream = random.random() < STREAM_SHARE
        body = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            body["stream"] = True
        started = time.monotonic()
        t0 = time.perf_counter()
        with self.client.post(
            "/v1/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {key['secret']}"},
            name="chat-stream" if stream else "chat",
            catch_response=True,
        ) as resp:
            elapsed = time.perf_counter() - t0
            status = resp.status_code or 0
            record = {
                "t": started,
                "d": elapsed,
                "k": key["id"],
                "g": key.get("gen", 0),
                "s": stream,
                "st": status,
            }
            try:
                self._check(resp, record, stream, status)
            except Exception as err:  # noqa: BLE001 - never lose a record to a checker bug
                record["p"] = f"checker error: {err!r}"[:200]
            log.write(record)
            # Expected rejections under churn are not locust failures; the orchestrator
            # asserts on them from the records.
            if status == 200 and not record.get("p") or status in EXPECTED_4XX:
                resp.success()
            else:
                resp.failure(record.get("p") or f"status {status} {record.get('e', '')}")

    @staticmethod
    def _check(resp, record: dict, stream: bool, status: int) -> None:
        """Validates the body and annotates the record (p = problem, c = error code,
        ra = Retry-After, e = connection error, b = unexpected body)."""
        problem = None
        if status == 200:
            try:
                if stream:
                    problem = _sse_problem(resp.text)
                else:
                    data = json.loads(resp.text)
                    if (
                        not data.get("choices")
                        or data.get("usage", {}).get("prompt_tokens", 0) <= 0
                    ):
                        problem = "non-stream body without choices/usage"
            except Exception as err:  # noqa: BLE001 - any parse error is a bad body
                problem = f"unparsable body: {err}"
        elif status in EXPECTED_4XX:
            try:
                record["c"] = json.loads(resp.text)["error"]["code"]
            except Exception:  # noqa: BLE001
                record["c"] = "?"
            if status == 429:
                record["ra"] = resp.headers.get("retry-after")
        elif status == 0:
            record["e"] = str(resp.error)[:200]
        else:
            record["b"] = (resp.text or "")[:200]
        if problem:
            record["p"] = problem

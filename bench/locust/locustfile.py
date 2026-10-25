"""Locust end-to-end load suite for the Lumewarp bench.

Driven by bench/scripts/run-locust.sh, which starts the same mock upstream,
Lumewarp and LiteLLM as bench/scripts/run.sh and points one headless locust run
at one target at a time (`--host`). Everything is configured through env vars
so the driver does not have to template this file:

  LW_TARGET            direct | lumewarp | litellm (labels only; default direct)
  LW_MODEL             model sent in every positive request (default mock)
  LW_ACCEPT_MODELS     comma-separated accepted response models (default LW_MODEL)
  LW_MAX_TOKENS        output token limit (default 16)
  LW_API_KEYS          comma-separated bearer keys; user i sends key i mod N.
                       Empty = no Authorization header.
  LW_WEIGHTS           task weights, `name=weight,...` (defaults below). Names:
                       chat, stream, completions, embeddings, models, negative
  LW_STREAM_USAGE_SHARE  share of streams that ask for
                       `stream_options.include_usage` (default 0.5)
  LW_NEGATIVE          1 enables negative requests (gateway targets only)
  LW_NEGATIVE_BAD_KEY  1 adds the bad-key negative (needs client auth)
  LW_USER_RPS          task runs per second per user (constant_throughput, 2)
  LW_STAGES            load shape `users:secs,users:secs,...` (cumulative steps)
  LW_SPAWN_RATE        users spawned per second when a stage starts (default:
                       the whole step within one second)
  LW_MEASURE_AFTER_SECS  samples before this offset are not dumped (warmup)
  LW_SAMPLES_DIR       each worker process writes `samples-<pid>.json` here
                       (LW_DUMP_DIR is a legacy alias):
                       full-precision latencies per request name, stream TTFT
                       and check-failure counters. Locust's own percentiles are
                       rounded to whole milliseconds, too coarse for sub-ms
                       gateway overhead, so bench/scripts/aggregate_locust.py
                       uses these dumps.

Every response is checked (status, JSON shape per endpoint, echoed model, SSE
framing, include_usage semantics). Negative requests are reported under their
own `neg_*` names and succeed when the gateway rejects them with a 4xx.

Run `python3 -I bench/locust/locustfile.py --selftest` to test the checkers
without locust installed. Run `uvx --from locust==2.32.4 --with
geventhttpclient==2.6.1 python -I bench/locust/locustfile.py --selftest-wiring`
to exercise GatewayUser against a temporary loopback HTTP server.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from typing import Any

DEFAULT_WEIGHTS = {
    "chat": 55,
    "stream": 25,
    "completions": 5,
    "embeddings": 10,
    "models": 5,
    "negative": 2,
}

POSITIVE_NAMES = (
    "chat",
    "chat_stream",
    "chat_stream_usage",
    "completions",
    "embeddings",
    "models",
)
STREAM_NAMES = ("chat_stream", "chat_stream_usage")
NEGATIVE_NAMES = ("neg_bad_key", "neg_unknown_model")


class CheckError(Exception):
    """A response that does not match the OpenAI-compatible contract."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def parse_weights(value: str | None) -> dict[str, int]:
    weights = dict(DEFAULT_WEIGHTS)
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or key not in DEFAULT_WEIGHTS:
            raise ValueError(f"LW_WEIGHTS: bad entry {item!r} (known: {', '.join(DEFAULT_WEIGHTS)})")
        weight = int(raw)
        if weight < 0:
            raise ValueError(f"LW_WEIGHTS: negative weight for {key}")
        weights[key] = weight
    return weights


def parse_stages(value: str | None) -> list[tuple[int, float]]:
    """`users:secs,...` -> [(users, end_offset_secs), ...]."""
    stages: list[tuple[int, float]] = []
    end = 0.0
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        users, sep, secs = item.partition(":")
        if not sep:
            raise ValueError(f"LW_STAGES: bad entry {item!r} (want users:secs)")
        n, d = int(users), float(secs)
        if n <= 0 or d <= 0:
            raise ValueError(f"LW_STAGES: users and secs must be > 0 in {item!r}")
        end += d
        stages.append((n, end))
    return stages


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


TARGET = os.environ.get("LW_TARGET", "direct")
MODEL = os.environ.get("LW_MODEL", "mock")
ACCEPT_MODELS = frozenset(m.strip() for m in os.environ.get("LW_ACCEPT_MODELS", MODEL).split(",") if m.strip())
MAX_TOKENS = int(os.environ.get("LW_MAX_TOKENS", "16"))
if not ACCEPT_MODELS or MAX_TOKENS <= 0:
    raise ValueError("LW_ACCEPT_MODELS must not be empty and LW_MAX_TOKENS must be positive")
API_KEYS = [k for k in os.environ.get("LW_API_KEYS", "").split(",") if k]
WEIGHTS = parse_weights(os.environ.get("LW_WEIGHTS"))
STREAM_USAGE_SHARE = float(os.environ.get("LW_STREAM_USAGE_SHARE", "0.5"))
NEGATIVE = _env_flag("LW_NEGATIVE")
NEGATIVE_BAD_KEY = _env_flag("LW_NEGATIVE_BAD_KEY")
USER_RPS = float(os.environ.get("LW_USER_RPS", "2"))
STAGES = parse_stages(os.environ.get("LW_STAGES"))
SPAWN_RATE = float(os.environ.get("LW_SPAWN_RATE", "0") or 0)
MEASURE_AFTER = float(os.environ.get("LW_MEASURE_AFTER_SECS", "0") or 0)
DUMP_DIR = os.environ.get("LW_SAMPLES_DIR", os.environ.get("LW_DUMP_DIR", ""))

CHAT_BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "benchmark"}],
    "max_tokens": MAX_TOKENS,
    "temperature": 0,
}
STREAM_BODY = {**CHAT_BODY, "stream": True}
STREAM_USAGE_BODY = {**STREAM_BODY, "stream_options": {"include_usage": True}}
COMPLETIONS_BODY = {"model": MODEL, "prompt": "benchmark", "max_tokens": MAX_TOKENS, "temperature": 0}
EMBEDDINGS_INPUT = ["benchmark one", "benchmark two"]
EMBEDDINGS_BODY = {"model": MODEL, "input": EMBEDDINGS_INPUT}
UNKNOWN_MODEL_BODY = {**CHAT_BODY, "model": "lumewarp-bench-no-such-model"}


# --------------------------------------------------------------------------- #
# Response checks (pure functions, covered by --selftest)
# --------------------------------------------------------------------------- #


def _require(cond: bool, reason: str) -> None:
    if not cond:
        raise CheckError(reason)


def _load(body: bytes | str | None) -> dict[str, Any]:
    try:
        doc = json.loads(body or b"")
    except ValueError:
        raise CheckError("body is not JSON") from None
    _require(isinstance(doc, dict), "body is not a JSON object")
    return doc


def check_chat(body: bytes | str | None, model: str | frozenset[str]) -> None:
    doc = _load(body)
    _require(doc.get("object") == "chat.completion", "chat: object != chat.completion")
    _require(doc.get("model") in _models(model), "chat: model not echoed")
    choices = doc.get("choices")
    _require(isinstance(choices, list) and choices, "chat: no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    _require(isinstance(message, dict), "chat: choices[0].message missing")
    _require(_has_text(message), "chat: no content or reasoning")
    _require(isinstance(doc.get("usage"), dict), "chat: usage missing")


def _models(model: str | frozenset[str]) -> frozenset[str]:
    return frozenset((model,)) if isinstance(model, str) else model


def _has_text(message: dict[str, Any]) -> bool:
    return any(isinstance(message.get(k), str) and message[k] for k in ("reasoning", "reasoning_content", "content"))


def check_completions(body: bytes | str | None, model: str | frozenset[str]) -> None:
    doc = _load(body)
    _require(doc.get("object") == "text_completion", "completions: object != text_completion")
    _require(doc.get("model") in _models(model), "completions: model not echoed")
    choices = doc.get("choices")
    _require(isinstance(choices, list) and choices, "completions: no choices")
    _require(
        isinstance(choices[0], dict) and isinstance(choices[0].get("text"), str),
        "completions: choices[0].text not a string",
    )


def check_embeddings(body: bytes | str | None, model: str | frozenset[str], inputs: int) -> None:
    doc = _load(body)
    _require(doc.get("object") == "list", "embeddings: object != list")
    _require(doc.get("model") in _models(model), "embeddings: model not echoed")
    data = doc.get("data")
    _require(isinstance(data, list) and len(data) == inputs, "embeddings: wrong data length")
    for item in data:
        vec = item.get("embedding") if isinstance(item, dict) else None
        _require(
            isinstance(vec, list) and vec and all(isinstance(x, (int, float)) for x in vec),
            "embeddings: embedding is not a numeric list",
        )


def check_models(body: bytes | str | None, model: str | frozenset[str]) -> None:
    doc = _load(body)
    _require(doc.get("object") == "list", "models: object != list")
    data = doc.get("data")
    _require(isinstance(data, list), "models: data not a list")
    _require(any(isinstance(m, dict) and m.get("id") in _models(model) for m in data), "models: model missing")


class SseChecker:
    """Incremental checker for one `chat.completion.chunk` SSE stream.

    Feed it one event at a time (the bytes between blank-line separators).
    Data lines are joined with newlines; comments and other SSE fields are ignored.
    `[DONE]` must be last. A chunk with a non-null `usage` is a usage
    chunk: none may reach the client unless include_usage was requested, and
    then exactly one, as the last JSON chunk.
    """

    def __init__(self, model: str | frozenset[str], include_usage: bool) -> None:
        self.model = model
        self.include_usage = include_usage
        self.done = False
        self.chunks = 0
        self.content_chunks = 0
        self.any_chunks = 0
        self.usage_chunks = 0
        self.last_was_usage = False

    def feed(self, event: bytes) -> bool:
        """Check one event; return True for the first content-bearing event."""
        data = []
        for line in event.splitlines():
            field, sep, value = line.partition(b":")
            if field == b"data":
                data.append(value.removeprefix(b" ") if sep else b"")
        payload = b"\n".join(data)
        if not payload:
            return False
        _require(not self.done, "sse: event after [DONE]")
        if payload == b"[DONE]":
            self.done = True
            return False
        try:
            chunk = json.loads(payload)
        except ValueError:
            raise CheckError("sse: data is not JSON") from None
        _require(isinstance(chunk, dict), "sse: chunk is not an object")
        _require(chunk.get("object") == "chat.completion.chunk", "sse: object != chat.completion.chunk")
        _require(chunk.get("model") in _models(self.model), "sse: model not echoed")
        self.chunks += 1
        if chunk.get("usage") is not None:
            _require(self.include_usage, "sse: usage chunk leaked without include_usage")
            self.usage_chunks += 1
            _require(self.usage_chunks == 1, "sse: more than one usage chunk")
            self.last_was_usage = True
            return False
        _require(self.usage_chunks == 0, "sse: chunk after the usage chunk")
        self.last_was_usage = False
        choices = chunk.get("choices")
        _require(isinstance(choices, list), "sse: choices not a list")
        first = False
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            if _has_text(delta):
                self.any_chunks += 1
            if isinstance(delta.get("content"), str) and delta["content"]:
                first = first or self.content_chunks == 0
                self.content_chunks += 1
        return first

    def finish(self, trailing: bytes = b"") -> None:
        _require(not trailing.strip(), "sse: trailing bytes without event separator")
        _require(self.done, "sse: stream did not end with [DONE]")
        _require(self.any_chunks > 0, "sse: no content or reasoning chunk")
        if self.include_usage:
            _require(self.usage_chunks == 1 and self.last_was_usage, "sse: missing final usage chunk")


def check_negative(status: int, name: str) -> None:
    _require(400 <= status < 500, f"{name}: expected 4xx, got {status}")


def read_sse(reader, checker: SseChecker, start: float) -> tuple[float | None, float | None]:  # type: ignore[no-untyped-def]
    ttft_any = None
    ttft_content = None
    lines: list[bytes] = []
    while True:
        # LF and CRLF are supported; CR-only line endings are unsupported.
        line = reader.readline(b"\n")
        if not line:
            break
        if line in (b"\n", b"\r\n"):
            first_content = checker.feed(b"".join(lines))
            elapsed = (time.perf_counter() - start) * 1000.0
            if checker.any_chunks and ttft_any is None:
                ttft_any = elapsed
            if first_content and ttft_content is None:
                ttft_content = elapsed
            lines.clear()
        else:
            lines.append(line)
    checker.finish(b"".join(lines) + reader.read())
    return ttft_any, ttft_content


# --------------------------------------------------------------------------- #
# Per-process sample store
# --------------------------------------------------------------------------- #


class Samples:
    def __init__(self) -> None:
        self.t0: float | None = None
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.ttft: dict[str, list[float]] = defaultdict(list)
        self.ttft_any: dict[str, list[float]] = defaultdict(list)
        self.ok: Counter[str] = Counter()
        self.fail: Counter[str] = Counter()
        self.status: dict[str, Counter[str]] = defaultdict(Counter)
        self.checks: Counter[str] = Counter()
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    def measuring(self) -> bool:
        now = time.monotonic()
        if self.t0 is None:
            self.t0 = now
        return now - self.t0 >= MEASURE_AFTER

    def record(self, name: str, response_time: float, ok: bool, status: int, reason: str | None) -> None:
        if not self.measuring():
            return
        now = time.time()
        if self.first_ts is None:
            self.first_ts = now
        self.last_ts = now
        self.status[name][str(status)] += 1
        if ok:
            self.ok[name] += 1
            self.latency[name].append(round(response_time, 3))
        else:
            self.fail[name] += 1
            if reason:
                self.checks[f"{name}: {reason}"] += 1

    def record_ttft(self, name: str, ttft_ms: float | None, ttft_any_ms: float | None = None) -> None:
        if self.measuring():
            if ttft_ms is not None:
                self.ttft[name].append(round(ttft_ms, 3))
            if ttft_any_ms is not None:
                self.ttft_any[name].append(round(ttft_any_ms, 3))

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "target": TARGET,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "latency_ms": dict(self.latency),
            "ttft_content_ms": dict(self.ttft),
            "ttft_any_ms": dict(self.ttft_any),
            "ok": dict(self.ok),
            "fail": dict(self.fail),
            "status": {k: dict(v) for k, v in self.status.items()},
            "checks": dict(self.checks),
        }

    def dump(self, force: bool = False) -> None:
        if not DUMP_DIR or (not force and not self.ok and not self.fail):
            return
        os.makedirs(DUMP_DIR, exist_ok=True)
        path = os.path.join(DUMP_DIR, f"samples-{os.getpid()}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh)
        os.replace(tmp, path)


SAMPLES = Samples()


# --------------------------------------------------------------------------- #
# Locust wiring (only when imported by locust)
# --------------------------------------------------------------------------- #


def _build_locust() -> None:
    from locust import LoadTestShape, constant_throughput, events, task  # noqa: F401
    from locust.contrib.fasthttp import FastHttpUser

    @events.request.add_listener
    def _on_request(name, response_time, exception, response=None, **_kw):  # type: ignore[no-untyped-def]
        status = getattr(response, "status_code", 0) if response is not None else 0
        reason = str(exception) if exception is not None else None
        SAMPLES.record(name, float(response_time or 0.0), exception is None, int(status or 0), reason)

    @events.test_start.add_listener
    def _on_test_start(**_kw):  # type: ignore[no-untyped-def]
        SAMPLES.t0 = time.monotonic()

    def _owns_samples(environment) -> bool:  # type: ignore[no-untyped-def]
        # The master of a --processes run executes no users. Every worker (or a
        # local runner) dumps even when idle, so the driver can wait for exactly
        # one dump per process.
        from locust.runners import MasterRunner

        return not isinstance(getattr(environment, "runner", None), MasterRunner)

    @events.test_stop.add_listener
    def _on_test_stop(environment=None, **_kw):  # type: ignore[no-untyped-def]
        if _owns_samples(environment):
            SAMPLES.dump(force=True)

    @events.quitting.add_listener
    def _on_quitting(environment=None, **_kw):  # type: ignore[no-untyped-def]
        if _owns_samples(environment):
            SAMPLES.dump(force=True)

    user_counter = iter(range(1 << 62))

    class GatewayUser(FastHttpUser):
        wait_time = constant_throughput(USER_RPS)
        # Streams and slow gateways must not hit the client timeout before the
        # gateway's own error; failures are then real server responses.
        network_timeout = 600.0
        connection_timeout = 60.0
        concurrency = 1
        insecure = True

        def on_start(self) -> None:
            index = next(user_counter)
            self.headers = {"Content-Type": "application/json"}
            if API_KEYS:
                self.headers["Authorization"] = f"Bearer {API_KEYS[index % len(API_KEYS)]}"

        def _post_json(self, path: str, name: str, body: dict[str, Any], check) -> None:  # type: ignore[no-untyped-def]
            with self.client.post(
                path, data=json.dumps(body), headers=self.headers, name=name, catch_response=True
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"status {resp.status_code}")
                    return
                try:
                    check(resp.content)
                except CheckError as err:
                    resp.failure(str(err))

        def chat(self) -> None:
            self._post_json("/v1/chat/completions", "chat", CHAT_BODY, lambda b: check_chat(b, ACCEPT_MODELS))

        def completions(self) -> None:
            self._post_json(
                "/v1/completions", "completions", COMPLETIONS_BODY, lambda b: check_completions(b, ACCEPT_MODELS)
            )

        def embeddings(self) -> None:
            self._post_json(
                "/v1/embeddings",
                "embeddings",
                EMBEDDINGS_BODY,
                lambda b: check_embeddings(b, ACCEPT_MODELS, len(EMBEDDINGS_INPUT)),
            )

        def models(self) -> None:
            with self.client.get("/v1/models", headers=self.headers, name="models", catch_response=True) as resp:
                if resp.status_code != 200:
                    resp.failure(f"status {resp.status_code}")
                    return
                try:
                    check_models(resp.content, ACCEPT_MODELS)
                except CheckError as err:
                    resp.failure(str(err))

        def stream(self) -> None:
            include_usage = random.random() < STREAM_USAGE_SHARE
            name = "chat_stream_usage" if include_usage else "chat_stream"
            body = STREAM_USAGE_BODY if include_usage else STREAM_BODY
            start = time.perf_counter()
            with self.client.post(
                "/v1/chat/completions",
                data=json.dumps(body),
                headers=self.headers,
                name=name,
                catch_response=True,
                stream=True,
            ) as resp:
                try:
                    if resp.status_code != 200:
                        resp.failure(f"status {resp.status_code}")
                        return
                    ctype = (resp.headers or {}).get("content-type", "")
                    if not ctype.startswith("text/event-stream"):
                        resp.failure(f"sse: content-type {ctype!r}")
                        return
                    checker = SseChecker(ACCEPT_MODELS, include_usage)
                    ttft_any, ttft_content = read_sse(resp.stream, checker, start)
                    # Report the whole stream, not just the headers, as this
                    # request's response time.
                    resp.request_meta["response_time"] = (time.perf_counter() - start) * 1000.0
                    SAMPLES.record_ttft(name, ttft_content, ttft_any)
                except CheckError as err:
                    resp.failure(str(err))
                except Exception as err:  # connection reset mid-stream etc.
                    resp.failure(f"sse: {type(err).__name__}: {err}")

        def negative(self) -> None:
            if NEGATIVE_BAD_KEY and random.random() < 0.5:
                name = "neg_bad_key"
                headers = {**self.headers, "Authorization": "Bearer lumewarp-bench-invalid-key"}
                body = CHAT_BODY
            else:
                name = "neg_unknown_model"
                headers = self.headers
                body = UNKNOWN_MODEL_BODY
            with self.client.post(
                "/v1/chat/completions", data=json.dumps(body), headers=headers, name=name, catch_response=True
            ) as resp:
                try:
                    check_negative(resp.status_code, name)
                    resp.success()
                except CheckError as err:
                    resp.failure(str(err))

        tasks = {
            fn: weight
            for fn, weight in (
                (chat, WEIGHTS["chat"]),
                (stream, WEIGHTS["stream"]),
                (completions, WEIGHTS["completions"]),
                (embeddings, WEIGHTS["embeddings"]),
                (models, WEIGHTS["models"]),
                (negative, WEIGHTS["negative"] if NEGATIVE else 0),
            )
            if weight > 0
        }

    globals()["GatewayUser"] = GatewayUser

    if STAGES:

        class StepShape(LoadTestShape):
            """Cumulative stages from LW_STAGES; stops after the last one."""

            def tick(self):  # type: ignore[no-untyped-def]
                elapsed = self.get_run_time()
                for users, end in STAGES:
                    if elapsed < end:
                        rate = SPAWN_RATE or float(users)
                        return users, rate
                return None

        globals()["StepShape"] = StepShape


# --------------------------------------------------------------------------- #
# Self-test (no locust needed)
# --------------------------------------------------------------------------- #


def _sse(chunks: list[dict[str, Any] | str]) -> list[bytes]:
    out = []
    for c in chunks:
        out.append(b"data: " + (c.encode() if isinstance(c, str) else json.dumps(c).encode()) + b"\n\n")
    return out


def _expect_fail(fn, reason: str) -> None:  # type: ignore[no-untyped-def]
    try:
        fn()
    except CheckError as err:
        assert reason in str(err), f"expected {reason!r}, got {err}"
        return
    raise AssertionError(f"expected CheckError {reason!r}")


def _run_sse(events: list[bytes], include_usage: bool, trailing: bytes = b"") -> SseChecker:
    checker = SseChecker("mock", include_usage)
    for event in events:
        checker.feed(event)
    checker.finish(trailing)
    return checker


def selftest() -> int:
    from io import BytesIO
    from unittest.mock import patch

    content = {"object": "chat.completion.chunk", "model": "mock", "usage": None,
               "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
    finish = {"object": "chat.completion.chunk", "model": "mock",
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    # Mock/Lumewarp shape: empty choices; LiteLLM shape: one empty delta.
    usage_mock = {"object": "chat.completion.chunk", "model": "mock", "choices": [],
                  "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
    usage_litellm = {**usage_mock, "choices": [{"index": 0, "delta": {}}]}

    c = _run_sse(_sse([content, content, finish, "[DONE]"]), False)
    assert c.content_chunks == 2 and c.usage_chunks == 0
    for usage in (usage_mock, usage_litellm):
        c = _run_sse(_sse([content, finish, usage, "[DONE]"]), True)
        assert c.usage_chunks == 1
    # TTFT marker: only the first content-bearing chunk.
    c = SseChecker("mock", False)
    assert c.feed(_sse([finish])[0]) is False
    assert c.feed(_sse([content])[0]) is True
    assert c.feed(_sse([content])[0]) is False
    c = SseChecker("mock", False)
    assert c.feed(_sse([{**content, "choices": content["choices"] * 2}])[0]) is True

    _expect_fail(lambda: _run_sse(_sse([content, finish, usage_mock, "[DONE]"]), False), "leaked")
    _expect_fail(lambda: _run_sse(_sse([content, finish, "[DONE]"]), True), "missing final usage")
    _expect_fail(lambda: _run_sse(_sse([content, usage_mock, usage_mock, "[DONE]"]), True), "more than one")
    _expect_fail(lambda: _run_sse(_sse([content, usage_mock, content, "[DONE]"]), True), "after the usage")
    _expect_fail(lambda: _run_sse(_sse([content, finish]), False), "did not end with [DONE]")
    _expect_fail(lambda: _run_sse(_sse([content, "[DONE]", content]), False), "after [DONE]")
    _expect_fail(lambda: _run_sse([b"event: x\n\n"], False), "did not end")
    _expect_fail(lambda: _run_sse([b"data: {nope\n\n"], False), "not JSON")
    _expect_fail(lambda: _run_sse(_sse([{**content, "model": "other"}, "[DONE]"]), False), "model not echoed")
    _expect_fail(lambda: _run_sse(_sse([finish, "[DONE]"]), False), "no content or reasoning")
    _expect_fail(lambda: _run_sse(_sse([content, "[DONE]"]), False, b"data: {}"), "trailing")

    class Reader(BytesIO):
        def readline(self, sep=b"\n"):
            assert sep == b"\n"
            return super().readline()

    for ending in (b"\n", b"\r\n"):
        for empty in (b"data:", b"data: "):
            event = empty + ending * 2
            c = SseChecker("mock", False)
            assert c.feed(event) is False
            c.feed(_sse([content])[0])
            c.feed(_sse(["[DONE]"])[0])
            assert c.feed(event) is False
            c.finish()
            assert c.chunks == 1 and c.content_chunks == 1 and c.any_chunks == 1
            with patch.object(time, "perf_counter", return_value=1.25):
                assert read_sse(
                    Reader(event + b"".join(_sse([content, "[DONE]"])) + event),
                    SseChecker("mock", False), 1.0,
                ) == (250.0, 250.0)
    _expect_fail(
        lambda: read_sse(
            Reader(b"".join(_sse([content, "[DONE]"])).replace(b"\n", b"\r")),
            SseChecker("mock", False), 0,
        ),
        "trailing",
    )

    for field in ("reasoning", "reasoning_content"):
        reasoning = {**content, "choices": [{"delta": {"content": None, field: "thinking"}}]}
        c = _run_sse(_sse([reasoning, finish, "[DONE]"]), False)
        assert c.any_chunks == 1 and c.content_chunks == 0
        for ending in (b"\n", b"\r\n"):
            # Pretty-printed JSON produces multiple data lines in one event.
            event = b"\n".join(b"data:" + line for line in json.dumps(reasoning, indent=2).encode().splitlines())
            frames = [b": ping\n\n", b"event: message\nid: 1\n" + event + b"\n\n"]
            frames += _sse([finish, usage_mock, "[DONE]"])
            frames.append(b": keepalive\n\n")
            reader = Reader(b"".join(frames).replace(b"\n", ending))
            with patch.object(time, "perf_counter", return_value=1.25):
                any_ms, content_ms = read_sse(reader, SseChecker("mock", True), 1.0)
            assert any_ms == 250.0 and content_ms is None
        with patch.object(time, "perf_counter", side_effect=[1.25, 1.5, 1.75]):
            any_ms, content_ms = read_sse(
                Reader(b"".join(_sse([reasoning, content, "[DONE]"]))), SseChecker("mock", False), 1.0
            )
        assert any_ms == 250.0 and content_ms == 500.0
    _expect_fail(
        lambda: read_sse(Reader(b"".join(_sse([content, "[DONE]"])) + b"data: {}"), SseChecker("mock", False), 0),
        "trailing",
    )
    accepted = frozenset(("alias", "mock"))
    c = SseChecker(accepted, False)
    assert c.feed(_sse([{**content, "model": "alias"}])[0])
    c.feed(_sse(["[DONE]"])[0])
    c.finish()

    chat = {"object": "chat.completion", "model": "mock", "usage": {},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}]}
    check_chat(json.dumps(chat), "mock")
    check_chat(json.dumps({**chat, "model": "alias"}), accepted)
    for field in ("reasoning", "reasoning_content"):
        check_chat(json.dumps({**chat, "choices": [{"message": {"content": None, field: "thinking"}}]}), "mock")
    for message in ({"content": None}, {"content": "", "reasoning": "", "reasoning_content": None}):
        _expect_fail(lambda: check_chat(json.dumps({**chat, "choices": [{"message": message}]}), "mock"), "no content")
    _expect_fail(lambda: check_chat(json.dumps({**chat, "model": "x"}), "mock"), "model not echoed")
    _expect_fail(lambda: check_chat(json.dumps({**chat, "choices": []}), "mock"), "no choices")
    _expect_fail(lambda: check_chat(b"<html>", "mock"), "not JSON")
    check_completions(json.dumps({"object": "text_completion", "model": "mock", "choices": [{"text": "x"}]}), "mock")
    _expect_fail(lambda: check_completions(json.dumps({"object": "text_completion", "model": "mock",
                                                       "choices": [{"text": 1}]}), "mock"), "text not")
    emb = {"object": "list", "model": "mock", "data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 1]}]}
    check_embeddings(json.dumps(emb), "mock", 2)
    _expect_fail(lambda: check_embeddings(json.dumps(emb), "mock", 3), "wrong data length")
    _expect_fail(lambda: check_embeddings(json.dumps({**emb, "data": [{"embedding": []}] * 2}), "mock", 2), "numeric")
    check_models(json.dumps({"object": "list", "data": [{"id": "mock"}]}), "mock")
    _expect_fail(lambda: check_models(json.dumps({"object": "list", "data": [{"id": "x"}]}), "mock"), "model missing")
    check_negative(401, "neg_bad_key")
    check_negative(404, "neg_unknown_model")
    _expect_fail(lambda: check_negative(200, "neg_bad_key"), "expected 4xx")
    _expect_fail(lambda: check_negative(500, "neg_bad_key"), "expected 4xx")

    assert parse_weights("chat=1, negative=0")["chat"] == 1
    assert parse_weights("")["stream"] == 25
    assert parse_stages("100:24,200:24") == [(100, 24.0), (200, 48.0)]
    for bad in ("nope=1", "chat"):
        try:
            parse_weights(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)

    s = Samples()
    s.t0 = time.monotonic() - MEASURE_AFTER - 1
    s.record("chat", 1.25, True, 200, None)
    s.record("chat", 3.0, False, 502, "status 502")
    s.record_ttft("chat_stream", 500.0, 250.0)
    s.record_ttft("chat_stream", None, 100.0)
    doc = s.to_json()
    assert "ttft_ms" not in doc
    assert doc["ttft_content_ms"] == {"chat_stream": [500.0]}
    assert doc["ttft_any_ms"] == {"chat_stream": [250.0, 100.0]}
    assert doc["latency_ms"]["chat"] == [1.25] and doc["checks"] == {"chat: status 502": 1}
    assert doc["fail"]["chat"] == 1 and doc["status"]["chat"] == {"200": 1, "502": 1}
    print("locustfile selftest: ok")
    return 0


def selftest_wiring() -> int:
    _build_locust()
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from threading import Thread
    from unittest.mock import patch

    from locust import events
    from locust.env import Environment

    echoed_model = sorted(ACCEPT_MODELS)[-1]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            assert body["model"] == MODEL and body["max_tokens"] == MAX_TOKENS
            if body.get("stream"):
                chunk = {"object": "chat.completion.chunk", "model": echoed_model,
                         "choices": [{"delta": {"content": None, "reasoning_content": "thinking"}}]}
                frames = [b": ping\r\n\r\n"]
                frames += [b"event: message\r\nid: 1\r\n" + _sse([chunk])[0].replace(b"\n", b"\r\n")]
                if body.get("stream_options", {}).get("include_usage"):
                    frames += _sse([{**chunk, "choices": [{"delta": {"content": "answer"}}]},
                                    {**chunk, "choices": [], "usage": {"total_tokens": 2}}])
                payload = b"".join(frames + _sse(["[DONE]"]))
                content_type = "text/event-stream"
            else:
                payload = json.dumps({"object": "chat.completion", "model": echoed_model, "usage": {},
                                      "choices": [{"message": {"content": None, "reasoning": "thinking"}}]}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with TemporaryDirectory() as directory, HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        samples = Samples()
        with patch.multiple(sys.modules[__name__], SAMPLES=samples, DUMP_DIR=directory, MEASURE_AFTER=0):
            try:
                env = Environment(events=events)
                with patch.object(globals()["GatewayUser"], "host", f"http://127.0.0.1:{server.server_port}"):
                    user = globals()["GatewayUser"](env)
                events.test_start.fire(environment=env)
                user.on_start()
                user.chat()
                for share in (0.0, 1.0):
                    with patch.object(sys.modules[__name__], "STREAM_USAGE_SHARE", share):
                        user.stream()
                events.test_stop.fire(environment=env)
                doc = json.loads((Path(directory) / f"samples-{os.getpid()}.json").read_text())
                assert "ttft_ms" not in doc, doc
                assert doc["ok"] == {"chat": 1, "chat_stream": 1, "chat_stream_usage": 1}, doc
                assert not doc["fail"] and not doc["checks"], doc
                assert len(doc["ttft_any_ms"]["chat_stream"]) == 1, doc
                assert "chat_stream" not in doc["ttft_content_ms"], doc
                assert len(doc["ttft_content_ms"]["chat_stream_usage"]) == 1, doc
                assert doc["ttft_any_ms"]["chat_stream_usage"][0] <= doc["ttft_content_ms"]["chat_stream_usage"][0]
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive()
    print("locustfile wiring selftest: ok (chat + reasoning-only SSE + usage SSE; samples dumped)")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    if sys.argv[1:] == ["--selftest-wiring"]:
        sys.exit(selftest_wiring())
    print("usage: locustfile.py --selftest | --selftest-wiring (or load with locust -f)", file=sys.stderr)
    sys.exit(2)
else:
    _build_locust()

#!/usr/bin/env python3
"""A6 scope overhead: keys with 0, 2 and 4 limiter scopes, plus a contended shared org scope.

The plan asks for the request-path cost of the rate-limit scopes the quota hierarchy adds
to a key. This starts the release gateway in standalone mode against the mock upstream and
runs the same fixed-rate chat load through one key per scenario:

    0 scopes          no limiter work at all (the baseline)
    2 scopes          team + key
    4 scopes          org + team + service account + key (the maximum)
    shared org        N keys, all on the same `org:<id>` bucket (high contention)

Lumewarp's per-request scope work is identical whether the shared bucket is reached by one
key or many, but the shared scenario uses several keys and parallel oha processes so the
bucket's lock really is contended from independent connections. Reported p99 is the
overhead of each scenario against the 0-scope baseline; the gate is "no regression"
(p99 overhead <= 0.2 ms, and no errors). This is not the LiteLLM A6 ratio; use
`bench/scripts/run.sh --quick` for that gate.

Usage:
    bench/scripts/scope_overhead.py [--no-build] [--quick|--full]

Environment overrides: MOCK_PORT (9100), GATEWAY_PORT (4100), RATE (1000),
DURATION_SECS (15), REPEAT (1), SHARED_KEYS (8), RESULTS_DIR (bench/results),
RUN_ID (UTC timestamp), GATEWAY_BIN, MOCK_BIN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_BIN = Path(os.environ.get("GATEWAY_BIN", ROOT / "gateway/target/release/lumewarp"))
MOCK_BIN = Path(
    os.environ.get("MOCK_BIN", ROOT / "bench/mock-upstream/target/release/mock-upstream")
)
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", ROOT / "bench/results"))
MOCK_PORT = int(os.environ.get("MOCK_PORT", "9100"))
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4100"))
SHARED_KEYS = int(os.environ.get("SHARED_KEYS", "8"))
# High enough that no scenario rejects at the measured rate: the cost of the limiter is
# what is measured, not its reject path.
SCOPE_RPM = 5_000_000
REQUEST_BODY = json.dumps(
    {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}
).encode()


def log(message: str) -> None:
    print(f"[scope-overhead] {message}", file=sys.stderr, flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as err:  # noqa: BLE001 - polled condition
            last = err
        time.sleep(0.05)
    raise SystemExit(f"timed out waiting for {what} (last error: {last})")


def scope(scope_id: str) -> str:
    return f'      - {{ id: "{scope_id}", requests_per_minute: {SCOPE_RPM} }}'


def config_yaml(listen: int, keys: list[tuple[str, list[str]]]) -> str:
    lines = [
        "server:",
        f"  listen: 127.0.0.1:{listen}",
        "  admin_listen: 127.0.0.1:0",
        "upstreams:",
        "  - name: mock",
        "    kind: openai_compatible",
        f"    base_url: http://127.0.0.1:{MOCK_PORT}/v1",
        "models:",
        "  - name: mock-model",
        "    upstream: mock",
        "    upstream_model: mock",
        "    type: llm",
        "routers:",
        "  - name: mock",
        "    members:",
        "      - {model: mock-model}",
        "teams:",
        "  - {id: t1}",
        "keys:",
    ]
    for key_id, scope_ids in keys:
        lines += [
            f"  - key_id: {key_id}",
            f"    key_sha256: '{hashlib.sha256(key_id.encode()).hexdigest()}'",
            "    allow_all_models: true",
            "    scopes:",
        ]
        if not scope_ids:
            lines[-1] = "    scopes: []"
        else:
            lines += [scope(scope_id) for scope_id in scope_ids]
    return "\n".join(lines) + "\n"


def new_key(prefix: str, scopes: list[str]) -> tuple[str, list[str]]:
    return (f"{prefix}-{secrets.token_hex(8)}", scopes)


def oha(rate: int, duration: int, connection: int, token: str, out: Path) -> None:
    body = REQUEST_BODY
    cmd = [
        "oha",
        "--no-tui",
        "--output-format",
        "json",
        "-o",
        str(out),
        "-q",
        str(rate),
        "--latency-correction",
        "-z",
        f"{duration}s",
        "-c",
        str(connection),
        "-m",
        "POST",
        "-T",
        "application/json",
        "-d",
        body,
        "-t",
        "600s",
        "-H",
        f"Authorization: Bearer {token}",
        f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def summarize(path: Path) -> dict:
    doc = json.loads(path.read_text())
    latencies = doc.get("latencyPercentiles") or {}
    metrics = doc.get("metrics") or {}
    statuses = {int(k): int(v) for k, v in (doc.get("statusCodeDistribution") or {}).items()}
    errors = dict(doc.get("errorDistribution") or {})
    # oha aborts in-flight requests at the -z deadline; that is the harness, not an error.
    errors.pop("aborted due to deadline", None)
    failed = sum(n for code, n in statuses.items() if not 200 <= code < 300)
    return {
        # oha reports seconds here; the rest of the bench reports milliseconds.
        "p50_ms": round(float(latencies.get("p50", 0)) * 1000, 3),
        "p90_ms": round(float(latencies.get("p90", 0)) * 1000, 3),
        "p99_ms": round(float(latencies.get("p99", 0)) * 1000, 3),
        "rps": round(float(metrics.get("requests_per_sec") or 0), 1),
        "errors": failed + sum(int(v) for v in errors.values()),
    }


def start_mock(logdir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            str(MOCK_BIN),
            "--listen",
            f"127.0.0.1:{MOCK_PORT}",
            "--latency-ms",
            "50",
            "--stream-chunks",
            "20",
            "--chunk-interval-ms",
            "10",
        ],
        cwd=logdir,
        stdout=(logdir / "mock.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    wait_for(
        lambda: (
            urllib.request.urlopen(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=1).status == 200
        ),
        10,
        "mock upstream",
    )
    return proc


def start_gateway(config: Path, logdir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [str(GATEWAY_BIN), "--config", str(config)],
        cwd=logdir,
        stdout=(logdir / "gateway.log").open("w"),
        stderr=subprocess.STDOUT,
    )


def wait_gateway(token: str) -> None:
    def ready() -> bool:
        request = urllib.request.Request(
            f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
            data=REQUEST_BODY,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=2).status == 200
        except urllib.error.HTTPError as err:
            return err.code == 200
        except Exception:  # noqa: BLE001
            return False

    wait_for(ready, 20, "gateway to answer with the key")


def run_scenario(
    name: str,
    keys: list[tuple[str, list[str]]],
    outdir: Path,
    rate: int,
    duration: int,
    repeat: int,
) -> dict:
    """One config, one gateway restart, `repeat` measured runs (median p99)."""
    scenario_dir = outdir / name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    config = scenario_dir / "config.yaml"
    config.write_text(config_yaml(GATEWAY_PORT, keys))
    samples: list[dict] = []
    gateway = start_gateway(config, scenario_dir)
    try:
        wait_gateway(keys[0][0])
        time.sleep(2)  # let the first requests settle into the limiter state
        for repetition in range(repeat):
            raw = scenario_dir / f"oha.rep{repetition}.json"
            if len(keys) == 1:
                oha(rate, duration, max(rate // 2, 64), keys[0][0], raw)
                samples.append(summarize(raw))
            else:
                # Independent processes on independent keys, all sharing the org bucket.
                per_process = max(rate // len(keys), 1)
                conns = max(per_process // 2, 8)
                outputs = [scenario_dir / f"oha.rep{repetition}.{i}.json" for i in range(len(keys))]
                procs = [
                    subprocess.Popen(
                        [
                            "oha",
                            "--no-tui",
                            "--output-format",
                            "json",
                            "-o",
                            str(output),
                            "-q",
                            str(per_process),
                            "--latency-correction",
                            "-z",
                            f"{duration}s",
                            "-c",
                            str(conns),
                            "-m",
                            "POST",
                            "-T",
                            "application/json",
                            "-d",
                            REQUEST_BODY,
                            "-t",
                            "600s",
                            "-H",
                            f"Authorization: Bearer {token}",
                            f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
                        ],
                        stdout=subprocess.DEVNULL,
                    )
                    for (token, _), output in zip(keys, outputs, strict=True)
                ]
                for proc in procs:
                    proc.wait()
                    if proc.returncode != 0:
                        raise SystemExit(f"oha failed in {name} repetition {repetition}")
                parts = [summarize(output) for output in outputs]
                samples.append(
                    {
                        "p50_ms": round(statistics.median(p["p50_ms"] for p in parts), 3),
                        "p90_ms": round(statistics.median(p["p90_ms"] for p in parts), 3),
                        "p99_ms": round(statistics.median(p["p99_ms"] for p in parts), 3),
                        "rps": round(sum(p["rps"] for p in parts), 1),
                        "errors": sum(p["errors"] for p in parts),
                    }
                )
    finally:
        gateway.terminate()
        gateway.wait(timeout=20)
    return {
        "scopes": len(keys[0][1]),
        "keys": len(keys),
        "samples": samples,
        "p50_ms": round(statistics.median(s["p50_ms"] for s in samples), 3),
        "p90_ms": round(statistics.median(s["p90_ms"] for s in samples), 3),
        "p99_ms": round(statistics.median(s["p99_ms"] for s in samples), 3),
        "rps": round(statistics.median(s["rps"] for s in samples), 1),
        "errors": sum(s["errors"] for s in samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--quick", action="store_true", help="15 s per scenario (default)")
    parser.add_argument("--full", action="store_true", help="3 repetitions of 30 s")
    args = parser.parse_args()
    duration = 30 if args.full else 15
    repeat = 3 if args.full else 1
    rate = int(os.environ.get("RATE", "1000"))
    run_id = os.environ.get("RUN_ID") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    outdir = RESULTS_DIR / f"scope-overhead-{run_id}"
    outdir.mkdir(parents=True, exist_ok=True)

    if not args.no_build:
        log("building the gateway and the mock upstream (release)")
        subprocess.run(
            ["cargo", "build", "--release", "-p", "lumewarp-gateway"],
            cwd=ROOT / "gateway",
            check=True,
        )
        subprocess.run(
            ["cargo", "build", "--release", "--bin", "mock-upstream"],
            cwd=ROOT / "bench/mock-upstream",
            check=True,
        )
    for binary in (GATEWAY_BIN, MOCK_BIN):
        if not binary.exists():
            raise SystemExit(f"missing {binary}; run without --no-build")

    mock = start_mock(outdir)
    scenarios = [
        ("scopes-0", [new_key("k0", [])]),
        ("scopes-2", [new_key("k2", ["team:t1", "key:"])]),
        ("scopes-4", [new_key("k4", ["org:o1", "team:t1", "sa:s1", "key:"])]),
        (
            "scopes-org-shared",
            [new_key(f"ks{i}", ["org:shared", "team:t1", "key:"]) for i in range(SHARED_KEYS)],
        ),
    ]
    # The key scope id must contain the key id; fill it in once the key id exists.
    normalized: list[tuple[str, list[tuple[str, list[str]]]]] = []
    for name, keys in scenarios:
        normalized.append(
            (
                name,
                [
                    (key_id, [s if s != "key:" else f"key:{key_id}" for s in scopes])
                    for key_id, scopes in keys
                ],
            )
        )

    results: dict[str, dict] = {}
    try:
        for name, keys in normalized:
            log(f"{name}: {len(keys)} key(s), {len(keys[0][1])} scopes, {rate} rps for {duration}s")
            results[name] = run_scenario(name, keys, outdir, rate, duration, repeat)
    finally:
        mock.terminate()
        mock.wait(timeout=10)

    baseline = results["scopes-0"]["p99_ms"]
    for _name, result in results.items():
        result["overhead_p99_ms"] = round(result["p99_ms"] - baseline, 3)
        result["regression"] = result["errors"] > 0 or result["overhead_p99_ms"] > 0.2
    document = {
        "run_id": run_id,
        "rate": rate,
        "duration_secs": duration,
        "repeat": repeat,
        "shared_keys": SHARED_KEYS,
        "scope_rpm": SCOPE_RPM,
        "gateway_bin": str(GATEWAY_BIN),
        "mock_bin": str(MOCK_BIN),
        "baseline_p99_ms": baseline,
        "regression": any(r["regression"] for r in results.values()),
        "scenarios": results,
    }
    (outdir / "results.json").write_text(json.dumps(document, indent=2) + "\n")
    summary = [
        "# Scope overhead (A6)",
        "",
        f"Rate {rate} rps, mock latency 50 ms, {duration}s per run, {repeat} repetition(s), "
        f"{SHARED_KEYS} shared-org keys.",
        "",
        "| scenario | scopes/key | keys | p50 ms | p90 ms | p99 ms | overhead p99 ms | errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, result in results.items():
        summary.append(
            f"| {name} | {result['scopes']} | {result['keys']} | {result['p50_ms']} | "
            f"{result['p90_ms']} | {result['p99_ms']} | {result['overhead_p99_ms']} | "
            f"{result['errors']} |"
        )
    summary += [
        "",
        "Overhead is against the 0-scope key on the same gateway; the gate is <= 0.2 ms and "
        "zero errors. A6 (Lumewarp vs LiteLLM) is `bench/scripts/run.sh --quick`.",
        "",
    ]
    (outdir / "summary.md").write_text("\n".join(summary))
    print("\n".join(summary))
    print(f"results: {outdir}")
    return 1 if document["regression"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

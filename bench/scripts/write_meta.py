#!/usr/bin/env python3
"""Write <run-dir>/meta.json: machine info, versions and knobs for one bench run.

Called by bench/scripts/run.sh once all servers are healthy. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _read_first(path: str, prefix: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(prefix):
                    return line.split(":", 1)[1].strip() if ":" in line else line.strip()
    except OSError:
        pass
    return None


def _cpu_model() -> str:
    model = _read_first("/proc/cpuinfo", "model name")
    return model or platform.processor() or "unknown"


def _mem_total_mb() -> int | None:
    value = _read_first("/proc/meminfo", "MemTotal:")
    if not value:
        return None
    try:
        return int(value.split()[0]) // 1024
    except (IndexError, ValueError):
        return None


def _os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def _run(args: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_info(repo: Path) -> dict[str, object]:
    sha = _run(["git", "rev-parse", "--short", "HEAD"], repo)
    describe = _run(["git", "describe", "--always", "--dirty"], repo)
    dirty = bool(_run(["git", "status", "--porcelain"], repo))
    return {"sha": sha or None, "describe": describe or None, "dirty": dirty}


def _binary_info(path: Path) -> dict[str, object]:
    info: dict[str, object] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        info["sha256"] = digest.hexdigest()
        info["mtime"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return info


def _parse_pairs(value: str) -> dict[str, int]:
    pairs: dict[str, int] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, _, val = item.partition(":")
        pairs[key] = int(val)
    return pairs


def _parse_list(value: str) -> list[int]:
    return [int(part) for part in value.replace(",", " ").split() if part]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", required=True, choices=("quick", "full"))
    parser.add_argument(
        "--auth",
        required=True,
        choices=("on", "off"),
        help="whether a per-run bearer key was configured on both gateways",
    )
    parser.add_argument("--started-at", required=True, help="ISO-8601 UTC start time")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--mock-port", type=int, required=True)
    parser.add_argument("--lumewarp-port", type=int, required=True)
    parser.add_argument("--litellm-port", type=int, required=True)
    parser.add_argument("--mock-latency-ms", type=int, required=True)
    parser.add_argument("--mock-stream-chunks", type=int, required=True)
    parser.add_argument("--mock-chunk-interval-ms", type=int, required=True)
    parser.add_argument("--litellm-version", required=True)
    parser.add_argument("--litellm-workers", type=int, required=True)
    parser.add_argument("--duration-secs", type=int, required=True)
    parser.add_argument("--warmup-secs", type=int, required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--rates", required=True, help="space/comma separated target RPS")
    parser.add_argument("--stream-concurrencies", required=True)
    parser.add_argument("--connections", required=True, help="rate:conns,rate:conns")
    parser.add_argument("--request-body", required=True)
    parser.add_argument("--mock-bin", required=True, type=Path)
    parser.add_argument("--sse-bench-bin", required=True, type=Path)
    parser.add_argument("--lumewarp-bin", required=True, type=Path)
    args = parser.parse_args(argv)

    meta = {
        "run_id": args.run_id,
        "profile": args.profile,
        "auth": args.auth,
        "started_at": args.started_at,
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "machine": {
            "nproc": len(os.sched_getaffinity(0)),
            "cpu_model": _cpu_model(),
            "kernel": platform.release(),
            "os": _os_name(),
            "mem_total_mb": _mem_total_mb(),
        },
        "git": _git_info(args.repo_root),
        "versions": {
            "litellm": args.litellm_version,
            "oha": _run(["oha", "--version"]),
            "python": _run(["python3", "--version"]),
            "rustc": _run(["rustc", "--version"]),
            "cargo": _run(["cargo", "--version"]),
        },
        "binaries": {
            "lumewarp": _binary_info(args.lumewarp_bin),
            "mock_upstream": _binary_info(args.mock_bin),
            "sse_bench": _binary_info(args.sse_bench_bin),
        },
        "config": {
            "ports": {
                "mock": args.mock_port,
                "lumewarp": args.lumewarp_port,
                "litellm": args.litellm_port,
            },
            "mock_latency_ms": args.mock_latency_ms,
            "mock_stream_chunks": args.mock_stream_chunks,
            "mock_chunk_interval_ms": args.mock_chunk_interval_ms,
            "litellm_workers": args.litellm_workers,
            "duration_secs": args.duration_secs,
            "warmup_secs": args.warmup_secs,
            "repetitions": args.repeat,
            "rates": _parse_list(args.rates),
            "stream_concurrencies": _parse_list(args.stream_concurrencies),
            "connections": _parse_pairs(args.connections),
        },
        "request": {"model": "mock", "body": json.loads(args.request_body)},
    }
    out = args.run_dir / "meta.json"
    out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

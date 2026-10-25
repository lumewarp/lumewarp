#!/usr/bin/env python3
"""Aggregate one Locust suite run into results.json + summary.md.

Input layout (written by bench/scripts/run-locust.sh):

    <locust-dir>/meta.json                 knobs, versions, auth on/off
    <locust-dir>/scenarios.jsonl           one object per target/scenario/repetition
    <locust-dir>/<scenario>/<target>.rep<M>/
        samples/samples-<pid>.json         per worker process: full-precision
                                           latencies, TTFT, ok/fail, statuses,
                                           check failures (see locustfile.py)
        server.cpu.csv                     proc_sampler of the target server
        generator.cpu.csv                  proc_sampler of the locust tree
        locust.log, locust_*.csv, report.html   locust's own output

Locust's CSV percentiles are rounded to whole milliseconds, so latencies and
overhead come from the sample dumps. Overhead = target − direct for the same
scenario; with repetitions, scenario metrics are medians and overhead p99 also
reports min/max across repetitions.

If <locust-dir>/../summary.md exists (a run.sh run with the same RUN_ID), the
Locust section is inserted into it between markers as well.

Stdlib only; `python3 -I aggregate_locust.py --run-dir DIR` or `--selftest`.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import random
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aggregate import _fmt, read_cpu  # noqa: E402  (stdlib-only sibling)

TARGETS = ("direct", "lumewarp", "litellm")
# Upstream-proxied non-stream requests. `models` is not in the latency group:
# Lumewarp answers it from its config snapshot while direct (the mock) adds
# its fixed latency, so it would show as negative overhead.
NONSTREAM = ("chat", "completions", "embeddings")
STREAM = ("chat_stream", "chat_stream_usage")
POSITIVE = NONSTREAM + ("models",) + STREAM
NEGATIVE = ("neg_bad_key", "neg_unknown_model")
MAX_ERROR_RATE = 0.01
MIN_RATE_RATIO = 0.9
MARK_BEGIN = "<!-- locust:begin -->"
MARK_END = "<!-- locust:end -->"


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100) of unsorted values."""
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return round(vals[0], 3)
    pos = (len(vals) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 3)


def _dist(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
        "max": round(max(values), 3) if values else None,
        "count": len(values),
    }


def _median(values: list[Any], digits: int = 3) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.median(vals), digits) if vals else None


def load_samples(sample_dir: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "latency_ms": {},
        "ttft_any_ms": {},
        "ttft_content_ms": {},
        "ok": Counter(),
        "fail": Counter(),
        "status": {},
        "checks": Counter(),
        "first_ts": None,
        "last_ts": None,
        "processes": 0,
    }
    for path in sorted(sample_dir.glob("samples-*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        merged["processes"] += 1
        for key in ("latency_ms", "ttft_any_ms", "ttft_content_ms"):
            # Missing new fields identify legacy dumps; an explicitly empty
            # content field means a reasoning-only stream, not a legacy dump.
            source = doc.get(key, doc.get("ttft_ms", {})) if key != "latency_ms" else doc.get(key)
            for name, vals in (source or {}).items():
                merged[key].setdefault(name, []).extend(float(v) for v in vals)
        merged["ok"].update(doc.get("ok") or {})
        merged["fail"].update(doc.get("fail") or {})
        merged["checks"].update(doc.get("checks") or {})
        for name, codes in (doc.get("status") or {}).items():
            merged["status"].setdefault(name, Counter()).update(codes)
        for key, pick in (("first_ts", min), ("last_ts", max)):
            value = doc.get(key)
            if value is not None:
                merged[key] = value if merged[key] is None else pick(merged[key], value)
    return merged


def step_rates(run_dir: Path, entry: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Use cumulative history counts, including negatives, not sliding-window RPS."""
    config = meta.get("config") or {}
    offered = config.get("step_offered", meta.get("step_offered"))
    if entry["scenario"] != "step" or not offered:
        return []
    stages = [(int(u), float(s)) for u, s in (stage.split(":") for stage in entry["stages"].split(","))]
    if len(stages) != len(offered):
        raise ValueError("step_offered must match the manifest stages")
    history = run_dir / "locust_stats_history.csv"
    points = []
    if history.is_file():
        with history.open(newline="", encoding="utf-8") as fh:
            points = [
                (float(row["Timestamp"]), float(row["Total Request Count"]), int(row["User Count"]))
                for row in csv.DictReader(fh) if row["Name"] == "Aggregated"
            ]
    points.sort()

    reports = [point for previous, point in zip(points, points[1:]) if point[1] > previous[1]]

    # User counts are current, but cumulative counts arrive from workers every
    # three seconds. Measure only after spawning and that reporting lead-in.
    boundaries = []
    cursor = 0
    previous_users = 0
    for users, secs in stages:
        ramp = next((i for i in range(cursor, len(points)) if points[i][2] != previous_users), None)
        reached = next(
            (i for i in range(ramp, len(points)) if points[i][2] == users), None
        ) if ramp is not None else None
        boundaries.append((ramp, reached))
        if reached is not None:
            cursor = reached + 1
            previous_users = users
    out = []
    stage_end = next((t for t, _, users in points if users), points[0][0] if points else 0.0)
    for i, ((users, secs), rate) in enumerate(zip(stages, offered)):
        stage_end += secs
        ramp, reached = boundaries[i]
        start = end = 0.0
        leadin = None
        interior = []
        if reached is not None:
            spawn = points[reached][0] - points[ramp][0]
            leadin = max(3.0, spawn) + 1.0
            start = points[reached][0] + leadin
            end = points[reached][0] + secs
            if i + 1 < len(boundaries) and boundaries[i + 1][0] is not None:
                end = min(end, points[boundaries[i + 1][0]][0])
            # Never interpolate a staircase counter: both endpoints must be
            # worker report points strictly inside the measurement window.
            interior = [point for point in reports if start < point[0] < end]
            if interior:
                # Staggered workers can update different phases each second.
                # Compare equal phases, allowing half a second of timestamp rounding.
                phase_start = interior[0][0]
                interior = [
                    point for point in interior
                    if abs(math.remainder(point[0] - phase_start, 3.0)) <= 0.5
                ]
        observed = interior[-1][0] - interior[0][0] if len(interior) >= 2 else 0.0
        complete = observed >= 6.0
        achieved = (
            round((interior[-1][1] - interior[0][1]) / observed, 1)
            if complete else None
        )
        flags = []
        if reached is None and points and points[-1][0] >= stage_end:
            flags.append(f"step {i + 1}: users never reached")
        if achieved is not None and achieved < MIN_RATE_RATIO * float(rate):
            flags.append(f"step {i + 1}: rps {achieved:.0f} < 90% of offered {rate}")
        out.append({
            "step": i + 1, "users": users, "offered_rps": float(rate), "rps": achieved,
            "observed_secs": observed, "leadin_secs": leadin, "flags": flags,
            "report_start_ts": interior[0][0] if interior else None,
            "report_end_ts": interior[-1][0] if interior else None,
            "diagnostics": [] if complete else ["incomplete history"],
        })
    return out


def count_cpu_warnings(log: Path) -> int:
    try:
        return log.read_text(encoding="utf-8", errors="replace").count("CPU usage above")
    except OSError:
        return 0


def summarize_run(locust_dir: Path, entry: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    run_dir = locust_dir / entry["dir"]
    config = meta.get("config") or {}
    processes = int(config.get("locust_processes") or 1)
    gen_limit = float(config.get("gen_cpu_limit_pct") or 85)
    s = load_samples(run_dir / "samples")

    lat = s["latency_ms"]
    nonstream_vals = [v for name in NONSTREAM for v in lat.get(name, [])]
    stream_vals = [v for name in STREAM for v in lat.get(name, [])]
    ttft = {
        key: _dist([v for name in STREAM for v in s[key].get(name, [])])
        for key in ("ttft_any_ms", "ttft_content_ms")
    }
    ok = sum(s["ok"].get(n, 0) for n in POSITIVE)
    fail = sum(s["fail"].get(n, 0) for n in POSITIVE)
    total = ok + fail
    span = None
    if s["first_ts"] is not None and s["last_ts"] is not None:
        span = s["last_ts"] - s["first_ts"]
    measured = float(entry.get("measured_secs") or 0) or None
    # Wall-clock span of the samples, bounded by the configured window (the
    # last stream can finish during --stop-timeout).
    secs = min(x for x in (span, measured) if x) if (span or measured) else None
    all_requests = sum(s["ok"].values()) + sum(s["fail"].values())
    rps = round(all_requests / secs, 1) if secs else None
    offered = float(entry.get("offered_rps") or 0) or None

    negatives = {
        name: {
            "ok": s["ok"].get(name, 0),
            "fail": s["fail"].get(name, 0),
            "status": dict(sorted(s["status"].get(name, {}).items())),
        }
        for name in NEGATIVE
        if s["ok"].get(name) or s["fail"].get(name)
    }

    server_cpu = read_cpu(run_dir / "server.cpu.csv")
    gen_cpu = read_cpu(run_dir / "generator.cpu.csv")
    gen_per_proc = (
        round(gen_cpu["avg_pct"] / processes, 1) if gen_cpu.get("avg_pct") is not None else None
    )
    cpu_warnings = count_cpu_warnings(run_dir / "locust.log")

    error_rate = fail / total if total else 1.0
    flags: list[str] = []
    if error_rate > MAX_ERROR_RATE:
        flags.append(f"failures {error_rate * 100:.2f}% > {MAX_ERROR_RATE * 100:.0f}%")
    if offered and rps is not None and rps < MIN_RATE_RATIO * offered:
        flags.append(f"rps {rps:.0f} < {MIN_RATE_RATIO * 100:.0f}% of offered {offered:.0f}")
    if gen_per_proc is not None and gen_per_proc > gen_limit:
        flags.append(f"generator CPU {gen_per_proc:.0f}%/process > {gen_limit:.0f}%")
    if cpu_warnings:
        flags.append(f"locust CPU warnings x{cpu_warnings}")
    accepted = sum(v["fail"] for v in negatives.values())
    if accepted:
        flags.append(f"{accepted} negative requests not rejected with 4xx")
    steps = step_rates(run_dir, entry, meta)
    flags.extend(flag for step in steps for flag in step["flags"])

    return {
        "target": entry["target"],
        "scenario": entry["scenario"],
        "repetition": entry.get("repetition", 1),
        "nonstream_ms": _dist(nonstream_vals),
        "stream_total_ms": _dist(stream_vals),
        **ttft,
        "ttft_headline_ms": ttft["ttft_any_ms"],
        "per_endpoint_ms": {name: _dist(lat.get(name, [])) for name in POSITIVE if name in lat},
        "requests": total,
        "failures": fail,
        "error_rate": round(error_rate, 6),
        "rps": rps,
        "offered_rps": offered,
        "steps": steps,
        "negatives": negatives,
        "checks": dict(s["checks"].most_common(10)),
        "cpu": server_cpu,
        "generator_cpu": {**gen_cpu, "per_process_avg_pct": gen_per_proc, "cpu_warnings": cpu_warnings},
        "generator_saturated": bool(
            (gen_per_proc is not None and gen_per_proc > gen_limit) or cpu_warnings
        ),
        "processes_dumped": s["processes"],
        "flags": flags,
        "dir": entry["dir"],
    }


def _median_run(reps: list[dict[str, Any]]) -> dict[str, Any]:
    def med(path: tuple[str, ...], digits: int = 3) -> float | None:
        vals = []
        for r in reps:
            cur: Any = r
            for key in path:
                cur = cur.get(key) if isinstance(cur, dict) else None
            vals.append(cur)
        return _median(vals, digits)

    out: dict[str, Any] = {"repetitions": len(reps)}
    for group in ("nonstream_ms", "stream_total_ms", "ttft_headline_ms", "ttft_any_ms", "ttft_content_ms"):
        out[group] = {k: med((group, k)) for k in ("p50", "p90", "p99", "max")}
    out["per_endpoint_ms"] = {
        name: {k: med(("per_endpoint_ms", name, k)) for k in ("p50", "p99")}
        for name in POSITIVE
        if any(name in r["per_endpoint_ms"] for r in reps)
    }
    for key in ("rps", "offered_rps"):
        out[key] = med((key,))
    out["error_rate"] = med(("error_rate",), 6)
    out["requests"] = sum(r["requests"] for r in reps)
    out["failures"] = sum(r["failures"] for r in reps)
    out["cpu"] = {k: med(("cpu", k)) for k in ("avg_pct", "max_pct", "max_mem_mb")}
    out["mem_kind"] = next((r["cpu"].get("mem_kind") for r in reps if r["cpu"].get("mem_kind")), None)
    out["generator_cpu"] = {
        k: med(("generator_cpu", k)) for k in ("avg_pct", "max_pct", "per_process_avg_pct")
    }
    negatives: dict[str, dict[str, Any]] = {}
    for r in reps:
        for name, v in r["negatives"].items():
            n = negatives.setdefault(name, {"ok": 0, "fail": 0, "status": Counter()})
            n["ok"] += v["ok"]
            n["fail"] += v["fail"]
            n["status"].update(v["status"])
    out["negatives"] = {k: {**v, "status": dict(sorted(v["status"].items()))} for k, v in negatives.items()}
    checks: Counter[str] = Counter()
    for r in reps:
        checks.update(r["checks"])
    out["checks"] = dict(checks.most_common(10))
    flagged = [r for r in reps if r["flags"]]
    by_flag: dict[str, list[str]] = {}
    for r in flagged:
        for f in r["flags"]:
            by_flag.setdefault(f, []).append(f"rep{r['repetition']}")
    out["flags"] = [f"{f} ({','.join(where)})" for f, where in sorted(by_flag.items())]
    out["flagged_repetitions"] = len(flagged)
    out["generator_saturated"] = any(r["generator_saturated"] for r in reps)
    out["raw"] = [r["dir"] for r in reps]
    out["steps"] = [
        {
            "step": step["step"],
            "offered_rps": step["offered_rps"],
            "rps": _median([r["steps"][i]["rps"] for r in reps]),
            "observed_secs": _median([r["steps"][i]["observed_secs"] for r in reps]),
            "flags": [
                f"{flag} (rep{r['repetition']})"
                for r in reps for flag in r["steps"][i]["flags"]
            ],
            "diagnostics": [
                f"{diagnostic} (rep{r['repetition']})"
                for r in reps for diagnostic in r["steps"][i]["diagnostics"]
            ],
        }
        for i, step in enumerate(reps[0]["steps"])
    ]
    return out


def _sub(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 3)


def bootstrap_ci(values: list[float]) -> list[float | None]:
    """Percentile 95% CI for the median of paired repetition overheads."""
    if not values:
        return [None, None]
    rng = random.Random(0)
    samples = [statistics.median(rng.choices(values, k=len(values))) for _ in range(1000)]
    return [percentile(samples, 2.5), percentile(samples, 97.5)]


def overhead_interval(values: list[float]) -> dict[str, Any]:
    n = len(values)
    bounds = bootstrap_ci(values) if n >= 5 else (
        [min(values), max(values)] if values else [None, None]
    )
    return {"kind": "ci95" if n >= 5 else "range", "n": n, "bounds": bounds}


def build_results(locust_dir: Path) -> dict[str, Any]:
    meta = json.loads((locust_dir / "meta.json").read_text(encoding="utf-8"))
    manifest = [
        json.loads(line)
        for line in (locust_dir / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runs = [summarize_run(locust_dir, e, meta) for e in manifest]
    scenarios: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in runs:
        if r["scenario"] not in order:
            order.append(r["scenario"])
    for name in order:
        group: dict[str, Any] = {}
        for target in TARGETS:
            reps = [r for r in runs if r["scenario"] == name and r["target"] == target]
            if reps:
                group[target] = _median_run(reps)
        direct_reps = {r["repetition"]: r for r in runs if r["scenario"] == name and r["target"] == "direct"}
        direct = group.get("direct")
        for target, s in group.items():
            if target == "direct" or not direct:
                continue
            pairs = [
                (r, direct_reps[r["repetition"]])
                for r in runs
                if r["scenario"] == name and r["target"] == target and r["repetition"] in direct_reps
            ]
            s["overhead_ms"] = {}
            s["overhead_intervals_ms"] = {}
            s["overhead_noise_floor_ms"] = {}
            for label, metric in (("nonstream", "nonstream_ms"), ("ttft", "ttft_any_ms")):
                for quantile in ("p50", "p99"):
                    per_rep = [_sub(r[metric][quantile], d[metric][quantile]) for r, d in pairs]
                    per_rep = [v for v in per_rep if v is not None]
                    key = f"{label}_{quantile}"
                    s["overhead_ms"][key] = _median(per_rep)
                    s[f"overhead_{key}_min"] = min(per_rep) if per_rep else None
                    s[f"overhead_{key}_max"] = max(per_rep) if per_rep else None
                    s["overhead_intervals_ms"][key] = overhead_interval(per_rep)
                    baseline = [
                        d[metric][quantile] for d in direct_reps.values()
                        if d[metric][quantile] is not None
                    ]
                    s["overhead_noise_floor_ms"][key] = (
                        round(max(baseline) - min(baseline), 3) if len(baseline) >= 2 else None
                    )
            s["overhead_paired_repetitions"] = len(pairs)
            s["overhead_comparable"] = (
                not s["flags"] and not direct["flags"] and len(pairs) == s["repetitions"]
            )
        scenarios[name] = group
    return {"meta": meta, "scenarios": scenarios, "runs": runs}


def _range(median: Any, low: Any, high: Any) -> str:
    text = _fmt(median)
    if low is not None and high is not None:
        text += f" [{_fmt(low)}–{_fmt(high)}]"
    return text


def _overhead(s: dict[str, Any], metric: str) -> str:
    value = s["overhead_ms"][metric]
    floor = s["overhead_noise_floor_ms"][metric]
    text = _range(value, s[f"overhead_{metric}_min"], s[f"overhead_{metric}_max"])
    if value is not None and floor is not None and abs(value) <= floor:
        text += f" (within floor {_fmt(floor)})"
    return text


def render_section(results: dict[str, Any], heading: str = "##") -> str:
    meta = results.get("meta") or {}
    config = meta.get("config") or {}
    versions = meta.get("versions") or {}
    lines: list[str] = []
    add = lines.append
    sub = heading + "#"
    add(f"{heading} Locust suite")
    add("")
    add(
        f"- {meta.get('profile', '?')} profile, `{versions.get('locust', '?')}`, "
        f"{config.get('locust_processes', '?')} generator processes, "
        f"{config.get('user_rps', '?')} tasks/s per user, "
        f"{config.get('repetitions', 1)} repetition(s) (medians shown)"
    )
    add(
        f"- LiteLLM workers: {config.get('litellm_workers', meta.get('litellm_workers', '?'))}; "
        f"model: `{config.get('model', meta.get('model', '?'))}`; "
        f"max_tokens: {config.get('max_tokens', meta.get('max_tokens', '?'))}"
    )
    add("- RPS and offered both include negative requests; failure % covers positive requests only.")
    keys = meta.get("client_keys") or 0
    if meta.get("auth") == "on":
        add(f"- client auth: on ({keys} keys on Lumewarp; LiteLLM master key; direct open)")
    else:
        add("- client auth: none on both gateways (bench config)")
    add(
        "- mix (weights): "
        + (config.get("weights") or "chat=55,stream=25,completions=5,embeddings=10,models=5,negative=2")
        + f"; include_usage share of streams {config.get('stream_usage_share', 0.5)}"
    )
    add("- non-stream = chat + completions + embeddings (`models` only in the per-endpoint table:")
    add("  Lumewarp serves it locally); stream total = whole SSE body;")
    add("  headline TTFT = first non-empty reasoning/reasoning_content/content delta (ttft_any);")
    add("  ttft_content = first non-empty content delta. Legacy TTFT samples supply both.")
    add("  overhead = target − direct within the same repetition, then median [min–max].")
    add("  n < 5 paired repetitions: range (n=N), not a confidence interval.")
    if any(
        interval["kind"] == "ci95"
        for group in results.get("scenarios", {}).values() for s in group.values()
        for interval in s.get("overhead_intervals_ms", {}).values()
    ):
        add("  n ≥ 5: bootstrap 95% CI of the paired median (seed 0, 1000 resamples).")
    add("  Noise floor = max–min of direct's same-metric percentile across repetitions;")
    add("  overhead within that spread is annotated, never hidden; detail floor is n/a (1 rep) when unavailable.")
    add("  Locust is closed-loop (no")
    add("  coordinated-omission correction), so the oha numbers in run.sh stay the A6 reference.")
    add("")
    for name, group in (results.get("scenarios") or {}).items():
        add(f"{sub} {name}")
        add("")
        add(
            "| target | p50 ms | p90 ms | p99 ms | max ms | rps | offered | fail % | "
            "ovh p99 ms | ttft_any p99 ms | ttft_content p99 ms | ttft_any ovh p99 | stream p99 ms | cpu avg/max % | "
            "PSS max MiB | gen cpu/proc % | flags |"
        )
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for target in TARGETS:
            s = group.get(target)
            if not s:
                continue
            ns, ttft, st = s["nonstream_ms"], s["ttft_headline_ms"], s["stream_total_ms"]
            cpu, gen = s["cpu"], s["generator_cpu"]
            if target == "direct":
                ovh_p99, ttft_ovh = "baseline", "baseline"
            else:
                ovh_p99 = _overhead(s, "nonstream_p99") if s.get("overhead_ms") else "–"
                ttft_ovh = _overhead(s, "ttft_p99") if s.get("overhead_ms") else "–"
                if not s.get("overhead_comparable"):
                    ovh_p99 += " (n/c)"
            flags = "; ".join(s["flags"]) if s["flags"] else "–"
            add(
                f"| {target} | {_fmt(ns['p50'])} | {_fmt(ns['p90'])} | {_fmt(ns['p99'])} | "
                f"{_fmt(ns['max'])} | {_fmt(s['rps'], 0)} | {_fmt(s['offered_rps'], 0)} | "
                f"{_fmt((s['error_rate'] or 0) * 100, 2)} | {ovh_p99} | {_fmt(ttft['p99'])} | "
                f"{_fmt(s['ttft_content_ms']['p99'])} | {ttft_ovh} | {_fmt(st['p99'])} | "
                f"{_fmt(cpu['avg_pct'], 1)}/{_fmt(cpu['max_pct'], 1)} | "
                f"{_fmt(cpu['max_mem_mb'], 1)} | {_fmt(gen['per_process_avg_pct'], 1)} | {flags} |"
            )
        add("")
        add("| target | overhead metric | median [min–max] ms | uncertainty ms | noise floor ms |")
        add("|---|---|---:|---:|---:|")
        for target, s in group.items():
            for metric in s.get("overhead_ms") or {}:
                interval = s["overhead_intervals_ms"][metric]
                low, high = interval["bounds"]
                label = "95% CI" if interval["kind"] == "ci95" else "range"
                floor = s["overhead_noise_floor_ms"][metric]
                add(
                    f"| {target} | {metric} | "
                    f"{_overhead(s, metric)} | {label} (n={interval['n']}) "
                    f"[{_fmt(low)}, {_fmt(high)}] | "
                    f"{_fmt(floor) if floor is not None else 'n/a (1 rep)'} |"
                )
        add("")
        if any(s.get("steps") for s in group.values()):
            add("Step boundaries follow the first history row reaching each user count.")
            add("Interior rates skip max(3 s worker reporting, observed spawn time) + 1 s.")
            add("Rates use the first changed-count report and the latest same-phase report inside the window.")
            add("Same phase means a span that is a multiple of 3 s (tolerance ±0.5 s).")
            add("Report spans <6 s are incomplete history, not CI failures.")
            add("")
            add("| target | step | achieved rps | offered rps | interior s | flags | diagnostics |")
            add("|---|---:|---:|---:|---:|---|---|")
            for target, s in group.items():
                for step in s["steps"]:
                    flags = "; ".join(step["flags"]) or "–"
                    diagnostics = "; ".join(step["diagnostics"]) or "–"
                    add(
                        f"| {target} | {step['step']} | {_fmt(step['rps'], 1)} | "
                        f"{_fmt(step['offered_rps'], 1)} | {_fmt(step['observed_secs'], 1)} | "
                        f"{flags} | {diagnostics} |"
                    )
            add("")
        add("| target | " + " | ".join(f"{n} p50/p99" for n in POSITIVE) + " | negatives (status: count) |")
        add("|---|" + "---:|" * len(POSITIVE) + "---|")
        for target in TARGETS:
            s = group.get(target)
            if not s:
                continue
            cells = []
            for n in POSITIVE:
                d = s["per_endpoint_ms"].get(n)
                cells.append(f"{_fmt(d['p50'])}/{_fmt(d['p99'])}" if d else "–")
            neg = "; ".join(
                f"{n}: " + ", ".join(f"{c}×{k}" for c, k in v["status"].items())
                + (f" ({v['fail']} not rejected)" if v["fail"] else "")
                for n, v in s["negatives"].items()
            ) or "–"
            add(f"| {target} | " + " | ".join(cells) + f" | {neg} |")
        add("")
        checks = {t: s["checks"] for t, s in group.items() if s.get("checks")}
        if checks:
            add("Failed checks (top reasons, all repetitions):")
            add("")
            for target, reasons in checks.items():
                for reason, count in reasons.items():
                    add(f"- {target}: `{reason}` × {count}")
            add("")
    add("`n/c` = not comparable (target or direct flagged: >1% failures, <90% of the offered")
    add("rate, or a generator-bound run). Negative requests (`neg_*`) are excluded from the")
    add("latency and failure columns; they pass when the gateway answers 4xx.")
    return "\n".join(lines) + "\n"


def render_summary(results: dict[str, Any]) -> str:
    meta = results.get("meta") or {}
    machine = meta.get("machine") or {}
    config = meta.get("config") or {}
    head = [
        "# Lumewarp Locust summary",
        "",
        f"- run: `{meta.get('run_id', '?')}`",
        f"- machine: {machine.get('nproc', '?')} × {machine.get('cpu_model', '?')} ({machine.get('kernel', '?')})",
        f"- lumewarp: `{(meta.get('git') or {}).get('describe') or '?'}`",
        f"- litellm: `{(meta.get('versions') or {}).get('litellm', '?')}` "
        f"({config.get('litellm_workers', '?')} workers)",
        f"- mock: latency {config.get('mock_latency_ms', '?')} ms, {config.get('mock_stream_chunks', '?')} "
        f"stream chunks × {config.get('mock_chunk_interval_ms', '?')} ms",
        "",
    ]
    return "\n".join(head) + "\n" + render_section(results)


def insert_section(summary: Path, section: str) -> None:
    text = summary.read_text(encoding="utf-8")
    block = f"{MARK_BEGIN}\n{section}{MARK_END}\n"
    if MARK_BEGIN in text and MARK_END in text:
        before = text.split(MARK_BEGIN, 1)[0]
        after = text.split(MARK_END, 1)[1].lstrip("\n")
        text = before + block + after
    else:
        text = text.rstrip("\n") + "\n\n" + block
    summary.write_text(text, encoding="utf-8")


def write_outputs(locust_dir: Path, results: dict[str, Any]) -> None:
    locust_dir.mkdir(parents=True, exist_ok=True)
    (locust_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    (locust_dir / "summary.md").write_text(render_summary(results), encoding="utf-8")
    parent = locust_dir.parent / "summary.md"
    if parent.is_file():
        insert_section(parent, render_section(results))


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _write_dump(path: Path, latency: dict[str, list[float]], ttft: dict[str, list[float]],
                fail: dict[str, int] | None = None, status: dict[str, dict[str, int]] | None = None) -> None:
    ok = {k: len(v) for k, v in latency.items()}
    for name, codes in (status or {}).items():
        ok[name] = sum(n for c, n in codes.items() if c.startswith("4"))
    doc = {
        "pid": 1, "first_ts": 1000.0, "last_ts": 1010.0, "latency_ms": latency, "ttft_ms": ttft,
        "ok": ok, "fail": fail or {}, "status": status or {},
        "checks": {"chat: status 502": fail["chat"]} if fail and "chat" in fail else {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_cpu(path: Path, cpu: list[float], pss_kb: int) -> None:
    rows = ["elapsed_s,cpu_pct,pss_kb,nprocs"] + [f"{i},{c},{pss_kb},1" for i, c in enumerate(cpu)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = root / "locust"
        run.mkdir()
        meta = {"run_id": "t", "profile": "quick", "auth": "on", "client_keys": 4,
                "config": {"locust_processes": 2, "gen_cpu_limit_pct": 85, "repetitions": 2,
                           "litellm_workers": 4, "max_tokens": 16, "model": "mock"}}
        (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        manifest = []
        base = [50.0 + i / 100 for i in range(100)]  # p99 = 50.9801
        for rep in (1, 2):
            for target, add, gen in (("direct", 0.0, 40.0), ("lumewarp", 0.2 * rep, 50.0),
                                     ("litellm", 30.0, 190.0)):
                rel = f"step/{target}.rep{rep}"
                d = run / rel
                lat = {"chat": [v + add for v in base], "chat_stream": [250.0 + add]}
                fail = {"chat": 5} if target == "litellm" else {}
                status = {"neg_unknown_model": {"404": 3}} if target == "lumewarp" else None
                if target == "litellm":
                    status = {"neg_bad_key": {"400": 2, "200": 1}}
                _write_dump(d / "samples" / "samples-1.json", lat, {"chat_stream": [55.0 + add]}, fail, status)
                if target == "litellm":
                    raw = json.loads((d / "samples" / "samples-1.json").read_text())
                    raw["fail"]["neg_bad_key"] = 1
                    (d / "samples" / "samples-1.json").write_text(json.dumps(raw))
                _write_cpu(d / "server.cpu.csv", [10.0, 20.0], 2048)
                _write_cpu(d / "generator.cpu.csv", [gen, gen], 1024)
                (d / "locust.log").write_text("", encoding="utf-8")
                manifest.append({"target": target, "scenario": "step", "repetition": rep,
                                 "measured_secs": 10, "offered_rps": 10, "dir": rel})
        (run / "scenarios.jsonl").write_text("\n".join(json.dumps(m) for m in manifest) + "\n")
        (root / "summary.md").write_text("# bench\n\nold\n", encoding="utf-8")

        results = build_results(run)
        write_outputs(run, results)
        step = results["scenarios"]["step"]
        assert step["direct"]["nonstream_ms"]["p99"] == 50.98, step["direct"]["nonstream_ms"]
        lw = step["lumewarp"]
        # Overhead: median of reps (0.2, 0.4) = 0.3, min/max per rep.
        assert lw["overhead_ms"]["nonstream_p99"] == 0.3, lw["overhead_ms"]
        assert (lw["overhead_nonstream_p99_min"], lw["overhead_nonstream_p99_max"]) == (0.2, 0.4)
        assert lw["overhead_ms"]["ttft_p99"] == 0.3
        assert lw["overhead_comparable"] is True and not lw["flags"], lw["flags"]
        assert lw["negatives"]["neg_unknown_model"]["status"] == {"404": 6}
        assert lw["rps"] == 10.4 and lw["cpu"]["max_mem_mb"] == 2.0
        ll = step["litellm"]
        assert ll["failures"] == 10 and ll["error_rate"] == round(5 / 106, 6)
        assert ll["generator_saturated"] is True and ll["overhead_comparable"] is False
        assert any("generator CPU 95%" in f for f in ll["flags"]), ll["flags"]
        assert any("not rejected" in f for f in ll["flags"]), ll["flags"]
        assert step["direct"]["generator_cpu"]["per_process_avg_pct"] == 20.0
        summary = (run / "summary.md").read_text()
        assert "| lumewarp | 50.80" in summary and "0.30 [0.20–0.40]" in summary, summary
        assert "(n/c)" in summary and "neg_bad_key: 200×2, 400×4 (2 not rejected)" in summary
        parent = (root / "summary.md").read_text()
        assert parent.startswith("# bench") and parent.count(MARK_BEGIN) == 1
        write_outputs(run, results)  # idempotent re-insert
        assert (root / "summary.md").read_text().count(MARK_BEGIN) == 1
        assert ci_check(results) == []
        lw["negatives"]["neg_unknown_model"]["fail"] = 1
        lw["error_rate"] = 0.5
        assert len(ci_check(results)) == 2, ci_check(results)
        assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5 and percentile([], 50) is None

        # A single bad repetition must fail the real CLI gate despite a clean
        # scenario median, for either gated target (LiteLLM stays informational).
        for target in ("direct", "lumewarp"):
            for bad in ("error_rate", "flags"):
                gated = {
                    "scenarios": {"step": {
                        t: {"error_rate": 0, "requests": 400, "flags": []}
                        for t in ("direct", "lumewarp")
                    }},
                    "runs": [
                        {"scenario": "step", "target": t, "repetition": rep,
                         "error_rate": 0.02 if bad == "error_rate" and t == target and rep == 4 else 0,
                         "requests": 100,
                         "flags": ["generator saturated"] if bad == "flags" and t == target and rep == 4 else []}
                        for t in ("direct", "lumewarp") for rep in range(1, 5)
                    ],
                }
                (run / "results.json").write_text(json.dumps(gated), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    assert main(["--run-dir", str(run), "--ci-check"]) == 1
                assert f"{target} rep4" in output.getvalue()
                assert len(ci_check(gated)) == 1
        for n in (0, 1, 3, 4, 5):
            values = [float(i) for i in range(1, n + 1)]
            interval = overhead_interval(values)
            assert interval["n"] == n
            assert interval["kind"] == ("ci95" if n >= 5 else "range")
            assert interval["bounds"] == ([1.0, float(n)] if n else [None, None])
            assert interval == overhead_interval(values)

        # Legacy dumps supply both TTFTs. Explicit new fields take precedence,
        # including an empty content distribution for reasoning-only streams.
        sample_dir = run / manifest[0]["dir"] / "samples"
        samples = load_samples(sample_dir)
        assert samples["ttft_any_ms"] == samples["ttft_content_ms"] == {"chat_stream": [55.0]}
        path = sample_dir / "samples-1.json"
        raw = json.loads(path.read_text())
        raw.update(ttft_any_ms={"chat_stream": [3.0]}, ttft_content_ms={})
        path.write_text(json.dumps(raw), encoding="utf-8")
        samples = load_samples(sample_dir)
        assert samples["ttft_any_ms"] == {"chat_stream": [3.0]}
        assert samples["ttft_content_ms"] == {}
        raw["ttft_content_ms"] = {"chat_stream": [8.0]}
        path.write_text(json.dumps(raw), encoding="utf-8")
        summarized = summarize_run(run, manifest[0], meta)
        assert summarized["ttft_any_ms"]["p99"] == 3.0
        assert summarized["ttft_content_ms"]["p99"] == 8.0
        assert summarized["ttft_headline_ms"] == summarized["ttft_any_ms"]
        assert "ttft_ms" not in summarized and "ttft_ms" not in samples

        # Trimmed Aggregated rows from w1-driver-smoke's direct history:
        # users start at zero and worker counters update every three seconds.
        meta["config"]["step_offered"] = [20, 40]
        entry = {**manifest[0], "stages": "10:5,20:5"}
        history = run / entry["dir"] / "locust_stats_history.csv"
        history.write_text(
            "Timestamp,User Count,Name,Total Request Count\n"
            "1792874996,0,Aggregated,0\n1792874997,0,Aggregated,0\n"
            "1792874998,10,Aggregated,0\n1792874999,10,Aggregated,0\n"
            "1792875000,10,Aggregated,46\n1792875001,10,Aggregated,46\n"
            "1792875002,10,Aggregated,46\n1792875003,20,Aggregated,113\n"
            "1792875004,20,Aggregated,113\n1792875005,20,Aggregated,113\n"
            "1792875006,20,Aggregated,231\n",
            encoding="utf-8",
        )
        steps = step_rates(history.parent, entry, meta)
        assert [s["observed_secs"] for s in steps] == [0.0, 0.0]
        assert all(not s["flags"] and s["diagnostics"] and s["rps"] is None for s in steps)
        step_run = summarize_run(run, entry, meta)
        step_summary = _median_run([step_run])
        assert not step_summary["flags"] and all(s["diagnostics"] for s in step_summary["steps"])
        assert ci_check({
            "scenarios": {"step": {"direct": step_summary, "lumewarp": step_summary}},
            "runs": [step_run, {**step_run, "target": "lumewarp"}],
        }) == []
        rendered = render_summary({"meta": meta, "scenarios": {"step": {"direct": step_summary}}})
        columns = None
        for line in rendered.splitlines():
            if line.startswith("|"):
                if columns is None:
                    columns = line.count("|")
                assert line.count("|") == columns
            else:
                columns = None
        # One-second history rows with single-phase and staggered worker counters.
        # CI/default quick interiors are 11/20 s (both L mod 3 = 2).
        for stage_users, duration in (((20, 50), 15), ((100, 200, 300, 400, 500), 24)):
            entry["stages"] = ",".join(f"{users}:{duration}" for users in stage_users)
            meta["config"]["step_offered"] = [users * 2 for users in stage_users]
            for share in (0.97, 0.80):
                for phases in ((0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)):
                    for startup in (1, 2):
                        for tail in (1, 2):
                            rows = ["Timestamp,User Count,Name,Total Request Count"]
                            counts = [0.0] * len(phases)
                            reported = [0] * len(phases)
                            weight = sum(range(1, len(phases) + 1))
                            for elapsed in range(startup + duration * len(stage_users) - tail + 1):
                                users = stage_users[(elapsed - startup) // duration] if elapsed >= startup else 0
                                for worker, phase in enumerate(phases):
                                    counts[worker] += users * 2 * share * (worker + 1) / weight
                                    if elapsed % 3 == phase:
                                        reported[worker] = round(counts[worker])
                                rows.append(f"{1000 + elapsed},{users},Aggregated,{sum(reported)}")
                            history.write_text("\n".join(rows) + "\n", encoding="utf-8")
                            steps = step_rates(history.parent, entry, meta)
                            for step in steps:
                                assert not step["diagnostics"], (duration, share, phases, startup, tail, step)
                                assert step["observed_secs"] >= 6 and step["observed_secs"] % 3 == 0
                                assert (step["report_start_ts"] - 1000) % 3 in phases
                                assert (step["report_end_ts"] - 1000) % 3 in phases
                                # Each worker rounds its integer counter; delta
                                # error is bounded by one request per worker.
                                assert abs(step["rps"] - step["offered_rps"] * share) <= (
                                    len(phases) / step["observed_secs"] + 0.05
                                )
                                assert bool(step["flags"]) == (share < 0.9)
                            assert bool(summarize_run(run, entry, meta)["flags"]) == (share < 0.9)

        # Trimmed direct history from w1-gate-ci-shape, relative timestamps.
        # 22->30 mixes worker phases; 22->28 is 602/6 = 100.3 RPS.
        meta["config"]["step_offered"] = [40, 100]
        entry["stages"] = "20:15,50:15"
        history.write_text(
            "Timestamp,User Count,Name,Total Request Count\n"
            "0,0,Aggregated,0\n2,20,Aggregated,0\n16,20,Aggregated,560\n"
            "17,50,Aggregated,560\n18,50,Aggregated,650\n19,50,Aggregated,806\n"
            "21,50,Aggregated,920\n22,50,Aggregated,1106\n24,50,Aggregated,1220\n"
            "25,50,Aggregated,1410\n27,50,Aggregated,1524\n28,50,Aggregated,1708\n"
            "30,50,Aggregated,1822\n",
            encoding="utf-8",
        )
        actual = step_rates(history.parent, entry, meta)[1]
        assert not actual["flags"] and not actual["diagnostics"]
        assert actual["report_start_ts"] == 22 and actual["report_end_ts"] == 28
        assert actual["observed_secs"] == 6 and abs(actual["rps"] - 100.2) < 1

        meta["config"]["step_offered"] = [20, 40]
        entry["stages"] = "10:15,20:15"
        # One report interval is insufficient; exactly two is measurable.
        for last in (1011, 1014):
            history.write_text(
                "Timestamp,User Count,Name,Total Request Count\n"
                "1000,0,Aggregated,0\n1002,10,Aggregated,0\n"
                + "".join(f"{ts},10,Aggregated,{(ts - 1002) * 20}\n" for ts in range(1005, last + 1, 3)),
                encoding="utf-8",
            )
            first = step_rates(history.parent, entry, meta)[0]
            assert first["observed_secs"] == last - 1008
            assert first["rps"] == (20.0 if last == 1014 else None)
            assert bool(first["diagnostics"]) == (last == 1011) and not first["flags"]

        # Half-second phase tolerance is inclusive; just outside it is not a pair.
        for last in (1014.5, 1014.501):
            history.write_text(
                "Timestamp,User Count,Name,Total Request Count\n"
                "1000,0,Aggregated,0\n1002,10,Aggregated,0\n"
                f"1008,10,Aggregated,120\n1011,10,Aggregated,180\n{last},10,Aggregated,250\n",
                encoding="utf-8",
            )
            first = step_rates(history.parent, entry, meta)[0]
            assert first["rps"] == (20.0 if last == 1014.5 else None)
            assert bool(first["diagnostics"]) == (last != 1014.5) and not first["flags"]

        # Missing users are a failure only after history covers the stage.
        for last in (1031, 1032):
            history.write_text(
                "Timestamp,User Count,Name,Total Request Count\n"
                "1000,0,Aggregated,0\n1002,10,Aggregated,0\n"
                + "".join(f"{ts},10,Aggregated,{(ts - 1002) * 20}\n" for ts in range(1005, last + 1)),
                encoding="utf-8",
            )
            missing = step_rates(history.parent, entry, meta)[1]
            assert missing["rps"] is None and missing["diagnostics"]
            assert missing["flags"] == (["step 2: users never reached"] if last == 1032 else [])
            assert bool(summarize_run(run, entry, meta)["flags"]) == (last == 1032)
        # Slow spawning extends the reporting lead-in beyond four seconds.
        entry["stages"] = "10:20,20:20"
        history.write_text(
            "Timestamp,User Count,Name,Total Request Count\n"
            "1000,0,Aggregated,0\n1001,1,Aggregated,0\n1008,10,Aggregated,0\n"
            "1016,10,Aggregated,160\n1019,10,Aggregated,220\n"
            "1022,10,Aggregated,280\n1025,10,Aggregated,340\n1028,10,Aggregated,400\n",
            encoding="utf-8",
        )
        slow_spawn = step_rates(history.parent, entry, meta)
        assert slow_spawn[0]["leadin_secs"] == 8.0 and slow_spawn[0]["rps"] == 20.0
        assert slow_spawn[1]["rps"] is None and slow_spawn[1]["diagnostics"]
        history.unlink()
        assert all(
            not s["flags"] and s["diagnostics"] and s["rps"] is None
            for s in step_rates(history.parent, entry, meta)
        )

        # Different baseline latencies expose subtraction-of-medians bugs.
        meta["config"].pop("step_offered")
        manifest = []
        for rep, baseline, overhead in ((1, 0.0, 1.0), (2, 100.0, 2.0), (3, 10.0, 30.0)):
            for target, offset in (("direct", baseline), ("lumewarp", baseline + overhead)):
                rel = f"paired/{target}.rep{rep}"
                _write_dump(run / rel / "samples/samples-1.json",
                            {"chat": [50 + offset]}, {"chat_stream": [55 + offset]})
                manifest.append({"target": target, "scenario": "paired", "repetition": rep,
                                 "measured_secs": 10, "dir": rel})
        # Direct drifts 60/80/100 ms, but a paired 30 ms regression is real.
        for rep, baseline in enumerate((60.0, 80.0, 100.0), 1):
            for target, overhead in (("direct", 0.0), ("lumewarp", 30.0)):
                rel = f"regression/{target}.rep{rep}"
                _write_dump(run / rel / "samples/samples-1.json",
                            {"chat": [baseline + overhead]}, {"chat_stream": [baseline + overhead]})
                manifest.append({"target": target, "scenario": "regression", "repetition": rep,
                                 "measured_secs": 10, "dir": rel})
        (run / "scenarios.jsonl").write_text("\n".join(json.dumps(m) for m in manifest) + "\n")
        results = build_results(run)
        paired = results["scenarios"]["paired"]["lumewarp"]
        assert all(v == 2.0 for v in paired["overhead_ms"].values())
        assert paired["overhead_nonstream_p50_min"] == 1.0
        assert paired["overhead_ttft_p99_max"] == 30.0
        assert all(
            interval == {"kind": "range", "n": 3, "bounds": [1.0, 30.0]}
            for interval in paired["overhead_intervals_ms"].values()
        )
        assert all(floor == 100.0 for floor in paired["overhead_noise_floor_ms"].values())
        regression = results["scenarios"]["regression"]["lumewarp"]
        assert all(v == 30.0 for v in regression["overhead_ms"].values())
        assert all(floor == 40.0 for floor in regression["overhead_noise_floor_ms"].values())
        for name, expected in (("paired", "2.00 [1.00–30.00]"), ("regression", "30.00 [30.00–30.00]")):
            section = render_section({"scenarios": {name: results["scenarios"][name]}})
            row = next(line for line in section.splitlines() if line.startswith("| lumewarp |"))
            assert row.split("|")[9].strip().startswith(expected), row
        for group in results["scenarios"].values():
            for s in group.values():
                assert "ttft_ms" not in s and s["ttft_headline_ms"] == s["ttft_any_ms"]
        assert all("ttft_ms" not in r for r in results["runs"])
        assert results == build_results(run)
        # The CLI can re-aggregate read-only inputs into an isolated output dir.
        scratch = root / "scratch/locust"
        original_results = (run / "results.json").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--run-dir", str(run), "--output-dir", str(scratch)]) == 0
        assert json.loads((scratch / "results.json").read_text()) == results
        assert (run / "results.json").read_bytes() == original_results
    print("aggregate_locust selftest: ok")
    return 0


def ci_check(results: dict[str, Any]) -> list[str]:
    """Hard failures for the CI smoke: Lumewarp must serve the whole mix
    correctly and reject every negative request. Latency is not gated here
    (run.sh + ci_check.py own that)."""
    problems: list[str] = []
    for name, group in (results.get("scenarios") or {}).items():
        for target in ("direct", "lumewarp"):
            s = group.get(target)
            if s is None:
                problems.append(f"{name}: no {target} results")
                continue
            if (s.get("error_rate") or 0) > MAX_ERROR_RATE:
                problems.append(f"{name}: {target} failures {s['error_rate'] * 100:.2f}% > 1%")
            if not s.get("requests"):
                problems.append(f"{name}: {target} served no requests")
            reps = [
                r for r in results.get("runs", [])
                if r["scenario"] == name and r["target"] == target
            ]
            for r in reps:
                reasons = list(r.get("flags") or [])
                if r.get("error_rate", 0) > MAX_ERROR_RATE:
                    reasons.append(f"failures {r['error_rate'] * 100:.2f}% > 1%")
                if not r.get("requests"):
                    reasons.append("served no requests")
                if reasons:
                    problems.append(
                        f"{name}: {target} rep{r['repetition']}: " + "; ".join(dict.fromkeys(reasons))
                    )
            if not reps and s.get("flags"):
                problems.append(f"{name}: {target}: " + "; ".join(s["flags"]))
        lw = group.get("lumewarp") or {}
        for neg, v in (lw.get("negatives") or {}).items():
            if v.get("fail"):
                problems.append(f"{name}: lumewarp accepted {v['fail']} {neg} requests")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="<results>/<RUN_ID>/locust")
    parser.add_argument("--output-dir", type=Path, help="write reports here instead of --run-dir")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--ci-check", action="store_true", help="exit 1 on Lumewarp/direct failures or accepted negatives"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.run_dir:
        parser.error("--run-dir is required")
    if args.ci_check:
        results = json.loads(((args.output_dir or args.run_dir) / "results.json").read_text(encoding="utf-8"))
        problems = ci_check(results)
        for problem in problems:
            print(f"::error::locust: {problem}")
        if not problems:
            print("locust ci check: ok")
        return 1 if problems else 0
    results = build_results(args.run_dir)
    output_dir = args.output_dir or args.run_dir
    write_outputs(output_dir, results)
    print(f"wrote {output_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

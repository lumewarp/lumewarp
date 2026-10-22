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
import json
import math
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
        "ttft_ms": {},
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
        for key in ("latency_ms", "ttft_ms"):
            for name, vals in (doc.get(key) or {}).items():
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
    ttft_vals = [v for name in STREAM for v in s["ttft_ms"].get(name, [])]
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
    rps = round(total / secs, 1) if secs else None
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

    return {
        "target": entry["target"],
        "scenario": entry["scenario"],
        "repetition": entry.get("repetition", 1),
        "nonstream_ms": _dist(nonstream_vals),
        "stream_total_ms": _dist(stream_vals),
        "ttft_ms": _dist(ttft_vals),
        "per_endpoint_ms": {name: _dist(lat.get(name, [])) for name in POSITIVE if name in lat},
        "requests": total,
        "failures": fail,
        "error_rate": round(error_rate, 6),
        "rps": rps,
        "offered_rps": offered,
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
    for group in ("nonstream_ms", "stream_total_ms", "ttft_ms"):
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
    return out


def _sub(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 3)


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
            s["overhead_ms"] = {
                "nonstream_p50": _sub(s["nonstream_ms"]["p50"], direct["nonstream_ms"]["p50"]),
                "nonstream_p99": _sub(s["nonstream_ms"]["p99"], direct["nonstream_ms"]["p99"]),
                "ttft_p50": _sub(s["ttft_ms"]["p50"], direct["ttft_ms"]["p50"]),
                "ttft_p99": _sub(s["ttft_ms"]["p99"], direct["ttft_ms"]["p99"]),
            }
            per_rep = [
                _sub(r["nonstream_ms"]["p99"], direct_reps[r["repetition"]]["nonstream_ms"]["p99"])
                for r in runs
                if r["scenario"] == name and r["target"] == target and r["repetition"] in direct_reps
            ]
            per_rep = [v for v in per_rep if v is not None]
            s["overhead_nonstream_p99_min"] = min(per_rep) if per_rep else None
            s["overhead_nonstream_p99_max"] = max(per_rep) if per_rep else None
            s["overhead_comparable"] = not s["flags"] and not direct["flags"]
        scenarios[name] = group
    return {"meta": meta, "scenarios": scenarios, "runs": runs}


def _range(median: Any, low: Any, high: Any) -> str:
    text = _fmt(median)
    if low is not None and high is not None and low != high:
        text += f" [{_fmt(low)}–{_fmt(high)}]"
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
    add("  overhead = target − direct for the same scenario. Locust is closed-loop (no")
    add("  coordinated-omission correction), so the oha numbers in run.sh stay the A6 reference.")
    add("")
    for name, group in (results.get("scenarios") or {}).items():
        add(f"{sub} {name}")
        add("")
        add(
            "| target | p50 ms | p90 ms | p99 ms | max ms | rps | offered | fail % | "
            "ovh p99 ms | ttft p99 ms | ttft ovh p99 | stream p99 ms | cpu avg/max % | "
            "PSS max MiB | gen cpu/proc % | flags |"
        )
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for target in TARGETS:
            s = group.get(target)
            if not s:
                continue
            ns, ttft, st = s["nonstream_ms"], s["ttft_ms"], s["stream_total_ms"]
            ovh = s.get("overhead_ms") or {}
            cpu, gen = s["cpu"], s["generator_cpu"]
            if target == "direct":
                ovh_p99, ttft_ovh = "baseline", "baseline"
            else:
                ovh_p99 = _range(
                    ovh.get("nonstream_p99"),
                    s.get("overhead_nonstream_p99_min"),
                    s.get("overhead_nonstream_p99_max"),
                )
                ttft_ovh = _fmt(ovh.get("ttft_p99"))
                if not s.get("overhead_comparable"):
                    ovh_p99 += " (n/c)"
            flags = "; ".join(s["flags"]) if s["flags"] else "–"
            add(
                f"| {target} | {_fmt(ns['p50'])} | {_fmt(ns['p90'])} | {_fmt(ns['p99'])} | "
                f"{_fmt(ns['max'])} | {_fmt(s['rps'], 0)} | {_fmt(s['offered_rps'], 0)} | "
                f"{_fmt((s['error_rate'] or 0) * 100, 2)} | {ovh_p99} | {_fmt(ttft['p99'])} | "
                f"{ttft_ovh} | {_fmt(st['p99'])} | {_fmt(cpu['avg_pct'], 1)}/{_fmt(cpu['max_pct'], 1)} | "
                f"{_fmt(cpu['max_mem_mb'], 1)} | {_fmt(gen['per_process_avg_pct'], 1)} | {flags} |"
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
                "config": {"locust_processes": 2, "gen_cpu_limit_pct": 85, "repetitions": 2}}
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
        assert lw["rps"] == 10.1 and lw["cpu"]["max_mem_mb"] == 2.0
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
        lw = group.get("lumewarp") or {}
        for neg, v in (lw.get("negatives") or {}).items():
            if v.get("fail"):
                problems.append(f"{name}: lumewarp accepted {v['fail']} {neg} requests")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="<results>/<RUN_ID>/locust")
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
        results = json.loads((args.run_dir / "results.json").read_text(encoding="utf-8"))
        problems = ci_check(results)
        for problem in problems:
            print(f"::error::locust: {problem}")
        if not problems:
            print("locust ci check: ok")
        return 1 if problems else 0
    results = build_results(args.run_dir)
    write_outputs(args.run_dir, results)
    print(f"wrote {args.run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

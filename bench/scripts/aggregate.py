#!/usr/bin/env python3
"""Aggregate one bench run into results.json + summary.md.

Input layout (written by bench/scripts/run.sh):

    <run-dir>/meta.json          machine/versions/knobs for this run
    <run-dir>/scenarios.jsonl    one JSON object per measured scenario/repetition
    <run-dir>/<raw>              oha JSON / sse-bench JSON
    <run-dir>/<cpu>              proc_sampler CSV (CPU% + PSS)

Overhead is defined as: latency through the gateway minus latency direct to the
mock upstream, measured at the same offered load on the same machine. It is only
comparable where both direct and the target achieved the offered rate; other
rows are marked informational.

With repetitions (REPEAT > 1), scenario metrics are medians across repetitions
and overhead p99 additionally reports min/max.

Stdlib only; `python3 -I aggregate.py --run-dir DIR` or `--selftest`.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

TARGETS = ("direct", "lumewarp", "lumewarp-rewrite", "litellm")
LATENCY_KEYS = ("p50", "p90", "p99", "mean", "max")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds: Any) -> float | None:
    try:
        return round(float(seconds) * 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _median(values: list[Any], digits: int = 3) -> float | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return round(statistics.median(vals), digits)


def _median_int(values: list[Any]) -> int | None:
    value = _median(values, 0)
    return int(value) if value is not None else None


def summarize_oha(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract latency / throughput / errors from one oha JSON document."""
    latencies = doc.get("latencyPercentiles") or {}
    metrics = doc.get("metrics") or {}
    latency_ms = metrics.get("latency_ms") or {}

    statuses: dict[int, int] = {}
    for code, count in (doc.get("statusCodeDistribution") or {}).items():
        try:
            statuses[int(code)] = int(count)
        except (TypeError, ValueError):
            continue
    error_counts = {str(k): int(v) for k, v in (doc.get("errorDistribution") or {}).items()}
    # Requests still in flight when oha's -z deadline hits are aborted by the
    # harness, not by the server: report them, but don't count them as errors.
    deadline_aborts = error_counts.pop("aborted due to deadline", 0)
    other_errors = sum(error_counts.values())
    responses = sum(statuses.values())
    successful = sum(count for code, count in statuses.items() if 200 <= code < 300)
    considered = responses + other_errors
    error_rate = ((responses - successful) + other_errors) / considered if considered else 0.0

    return {
        "latency_ms": {
            "p50": _ms(latencies.get("p50")),
            "p90": _ms(latencies.get("p90")),
            "p99": _ms(latencies.get("p99")),
            "mean": _num(latency_ms.get("mean")),
            "max": _num(latency_ms.get("max")),
        },
        "rps": round(_num(metrics.get("requests_per_sec")) or 0.0, 3),
        "error_rate": round(error_rate, 6),
        "responses": responses,
        "deadline_aborts": deadline_aborts,
        "errors": error_counts,
    }


def summarize_sse(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields we care about from one sse-bench JSON document.

    Newer sse-bench builds emit `ok_rps` (throughput of successful requests,
    excluding failed ones). Prefer it when present, fall back to `rps`.
    """
    requests = int(doc.get("requests") or 0)
    errors = int(doc.get("errors") or 0)
    ttft = doc.get("ttft_ms") or {}
    total = doc.get("total_ms") or {}
    rps_value = doc.get("ok_rps")
    if rps_value is None:
        rps_value = doc.get("rps")
    return {
        "rps": round(_num(rps_value) or 0.0, 3),
        "ttft_ms": {
            "p50": _num(ttft.get("p50")),
            "p90": _num(ttft.get("p90")),
            "p99": _num(ttft.get("p99")),
            "max": _num(ttft.get("max")),
        },
        "total_ms": {
            "p50": _num(total.get("p50")),
            "p90": _num(total.get("p90")),
            "p99": _num(total.get("p99")),
            "max": _num(total.get("max")),
        },
        "requests": requests,
        "errors": errors,
        "error_rate": round(errors / requests, 6) if requests else 0.0,
    }


def read_cpu(path: Path) -> dict[str, Any]:
    """avg/max CPU% and memory from a proc_sampler CSV.

    New CSVs carry PSS (`pss_kb`); older runs carry RSS (`rss_kb`). `mem_kind`
    records which one was used so the label is not silently wrong.
    """
    empty = {
        "avg_pct": None,
        "max_pct": None,
        "avg_mem_mb": None,
        "max_mem_mb": None,
        "mem_kind": None,
        "samples": 0,
    }
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return empty
    cpu = [float(r["cpu_pct"]) for r in rows if r.get("cpu_pct") not in (None, "")]
    if rows and "pss_kb" in rows[0]:
        mem_col, mem_kind = "pss_kb", "pss"
    elif rows and "rss_kb" in rows[0]:
        mem_col, mem_kind = "rss_kb", "rss"
    else:
        mem_col, mem_kind = None, None
    mem = [int(r[mem_col]) for r in rows if mem_col and r.get(mem_col) not in (None, "")]
    if not cpu and not mem:
        return empty
    return {
        "avg_pct": round(statistics.fmean(cpu), 2) if cpu else None,
        "max_pct": round(max(cpu), 2) if cpu else None,
        "avg_mem_mb": round(statistics.fmean(mem) / 1024.0, 1) if mem else None,
        "max_mem_mb": round(max(mem) / 1024.0, 1) if mem else None,
        "mem_kind": mem_kind,
        "samples": len(rows),
    }


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return loaded


def load_manifest(run_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with (run_dir / "scenarios.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _saturation_reasons(scenario: dict[str, Any], offered_rate: int | None) -> list[str]:
    reasons = []
    if offered_rate is not None and scenario["rps"] < 0.9 * offered_rate:
        reasons.append(f"achieved {scenario['rps']:.0f} rps < 90% of {offered_rate}")
    if scenario["error_rate"] > 0.01:
        reasons.append(f"error rate {scenario['error_rate'] * 100:.2f}%")
    return reasons


def _median_scenario(reps: list[dict[str, Any]], kind: str, key: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "target": reps[0]["target"],
        "repetitions": len(reps),
        "raw": [r["raw"] for r in reps],
        "duration_secs": reps[0].get("duration_secs"),
        "cpu": {
            "avg_pct": _median([r["cpu"].get("avg_pct") for r in reps]),
            "max_pct": _median([r["cpu"].get("max_pct") for r in reps]),
            "avg_mem_mb": _median([r["cpu"].get("avg_mem_mb") for r in reps]),
            "max_mem_mb": _median([r["cpu"].get("max_mem_mb") for r in reps]),
            "samples": _median_int([r["cpu"].get("samples") for r in reps]),
        },
        "mem_kind": next(
            (r["cpu"].get("mem_kind") for r in reps if r["cpu"].get("mem_kind")), None
        ),
    }
    if kind == "nonstream":
        out["rate"] = key
        out["connections"] = reps[0].get("connections")
        out["latency_ms"] = {
            k: _median([r["latency_ms"].get(k) for r in reps]) for k in LATENCY_KEYS
        }
        out["rps"] = _median([r["rps"] for r in reps])
        out["error_rate"] = _median([r["error_rate"] for r in reps], 6)
        out["responses"] = _median_int([r["responses"] for r in reps])
        out["deadline_aborts"] = _median_int([r["deadline_aborts"] for r in reps])
    else:
        out["concurrency"] = key
        out["rps"] = _median([r["rps"] for r in reps])
        out["error_rate"] = _median([r["error_rate"] for r in reps], 6)
        out["requests"] = _median_int([r["requests"] for r in reps])
        out["errors"] = _median_int([r["errors"] for r in reps])
        out["ttft_ms"] = {k: _median([r["ttft_ms"].get(k) for r in reps]) for k in LATENCY_KEYS}
        out["total_ms"] = {k: _median([r["total_ms"].get(k) for r in reps]) for k in LATENCY_KEYS}
    return out


def _delta(gateway: Any, direct: Any) -> float | None:
    if gateway is None or direct is None:
        return None
    return round(float(gateway) - float(direct), 3)


def _overhead_nonstream(
    reps: list[dict[str, Any]], direct_reps: list[dict[str, Any]]
) -> tuple[dict[str, float | None] | None, float | None, float | None, int]:
    by_rep = {r["repetition"]: r for r in direct_reps}
    rows: list[dict[str, float | None]] = []
    for rep in reps:
        direct = by_rep.get(rep["repetition"])
        if direct is None:
            continue
        rows.append(
            {
                "p50": _delta(rep["latency_ms"].get("p50"), direct["latency_ms"].get("p50")),
                "p90": _delta(rep["latency_ms"].get("p90"), direct["latency_ms"].get("p90")),
                "p99": _delta(rep["latency_ms"].get("p99"), direct["latency_ms"].get("p99")),
            }
        )
    if not rows:
        return None, None, None, 0
    med = {k: _median([row[k] for row in rows]) for k in ("p50", "p90", "p99")}
    p99s = [row["p99"] for row in rows if row["p99"] is not None]
    return med, (min(p99s) if p99s else None), (max(p99s) if p99s else None), len(rows)


def _overhead_stream(
    reps: list[dict[str, Any]], direct_reps: list[dict[str, Any]]
) -> tuple[dict[str, float | None] | None, float | None, float | None, int]:
    by_rep = {r["repetition"]: r for r in direct_reps}
    rows: list[dict[str, float | None]] = []
    for rep in reps:
        direct = by_rep.get(rep["repetition"])
        if direct is None:
            continue
        rows.append(
            {
                "ttft_p50": _delta(rep["ttft_ms"].get("p50"), direct["ttft_ms"].get("p50")),
                "ttft_p99": _delta(rep["ttft_ms"].get("p99"), direct["ttft_ms"].get("p99")),
                "total_p50": _delta(rep["total_ms"].get("p50"), direct["total_ms"].get("p50")),
                "total_p99": _delta(rep["total_ms"].get("p99"), direct["total_ms"].get("p99")),
            }
        )
    if not rows:
        return None, None, None, 0
    med = {k: _median([row[k] for row in rows]) for k in ("ttft_p50", "ttft_p99", "total_p50", "total_p99")}
    ttft = [row["ttft_p99"] for row in rows if row["ttft_p99"] is not None]
    return med, (min(ttft) if ttft else None), (max(ttft) if ttft else None), len(rows)


def _summarize_rep_reasons(
    reps: list[dict[str, Any]], offered_rate: int | None
) -> tuple[bool, int, list[str]]:
    per_rep = [(r["repetition"], _saturation_reasons(r, offered_rate)) for r in reps]
    all_reasons = [reason for _rep, reasons in per_rep for reason in reasons]
    if not all_reasons:
        return False, 0, []
    counts = Counter(all_reasons)
    if len(reps) == 1:
        reasons_out = list(counts)
    else:
        reasons_out = [
            f"{count}/{len(reps)} repetitions: {reason}" for reason, count in counts.items()
        ]
    saturated_reps = sum(1 for _rep, reasons in per_rep if reasons)
    return True, saturated_reps, reasons_out


def _finalize_nonstream(
    group: dict[str, dict[str, Any]],
    rep_lists: dict[str, list[dict[str, Any]]],
    rate: int,
    flags: list[dict[str, Any]],
    kind: str = "nonstream",
) -> None:
    # First pass: saturation state (needed to judge direct's comparability).
    for target in TARGETS:
        scenario = group.get(target)
        if scenario is None:
            continue
        saturated, saturated_reps, reasons = _summarize_rep_reasons(rep_lists[target], rate)
        scenario["saturated"] = saturated
        scenario["saturated_repetitions"] = saturated_reps
        scenario["saturation_reasons"] = reasons
        if saturated:
            flags.append({"scenario": f"{kind}/{rate}/{target}", "reasons": reasons})

    direct = group.get("direct")
    direct_ok = bool(
        direct is not None
        and not direct["saturated"]
        and (direct["error_rate"] or 0) <= 0.01
        and (direct["rps"] or 0) >= 0.9 * rate
    )

    # Second pass: overhead vs direct + whether the comparison is meaningful.
    for target in TARGETS:
        scenario = group.get(target)
        if scenario is None:
            continue
        if target == "direct":
            scenario["overhead_ms"] = {"p50": 0.0, "p90": 0.0, "p99": 0.0}
            scenario["overhead_p99_min"] = 0.0 if scenario["repetitions"] else None
            scenario["overhead_p99_max"] = 0.0 if scenario["repetitions"] else None
            scenario["overhead_repetitions"] = scenario["repetitions"]
        elif direct is not None:
            med, p99_min, p99_max, matched = _overhead_nonstream(reps=rep_lists[target], direct_reps=rep_lists["direct"])
            scenario["overhead_ms"] = med
            scenario["overhead_p99_min"] = p99_min
            scenario["overhead_p99_max"] = p99_max
            scenario["overhead_repetitions"] = matched
        else:
            scenario["overhead_ms"] = None
            scenario["overhead_p99_min"] = None
            scenario["overhead_p99_max"] = None
            scenario["overhead_repetitions"] = 0

        target_ok = bool(
            not scenario["saturated"]
            and (scenario["error_rate"] or 0) <= 0.01
            and (scenario["rps"] or 0) >= 0.9 * rate
        )
        scenario["overhead_comparable"] = bool(target_ok and direct_ok)


def _finalize_stream(
    group: dict[str, dict[str, Any]],
    rep_lists: dict[str, list[dict[str, Any]]],
    concurrency: int,
    flags: list[dict[str, Any]],
) -> None:
    # First pass: saturation state.
    for target in TARGETS:
        scenario = group.get(target)
        if scenario is None:
            continue
        saturated, saturated_reps, reasons = _summarize_rep_reasons(rep_lists[target], None)
        scenario["saturated"] = saturated
        scenario["saturated_repetitions"] = saturated_reps
        scenario["saturation_reasons"] = reasons
        if saturated:
            flags.append({"scenario": f"stream/{concurrency}/{target}", "reasons": reasons})

    direct = group.get("direct")
    direct_ok = bool(direct is not None and (direct["error_rate"] or 0) <= 0.01)

    # Second pass: overhead vs direct + comparability.
    for target in TARGETS:
        scenario = group.get(target)
        if scenario is None:
            continue
        if target == "direct":
            scenario["overhead_ms"] = {
                "ttft_p50": 0.0,
                "ttft_p99": 0.0,
                "total_p50": 0.0,
                "total_p99": 0.0,
            }
            scenario["overhead_ttft_p99_min"] = 0.0 if scenario["repetitions"] else None
            scenario["overhead_ttft_p99_max"] = 0.0 if scenario["repetitions"] else None
            scenario["overhead_repetitions"] = scenario["repetitions"]
        elif direct is not None:
            med, ttft_min, ttft_max, matched = _overhead_stream(reps=rep_lists[target], direct_reps=rep_lists["direct"])
            scenario["overhead_ms"] = med
            scenario["overhead_ttft_p99_min"] = ttft_min
            scenario["overhead_ttft_p99_max"] = ttft_max
            scenario["overhead_repetitions"] = matched
        else:
            scenario["overhead_ms"] = None
            scenario["overhead_ttft_p99_min"] = None
            scenario["overhead_ttft_p99_max"] = None
            scenario["overhead_repetitions"] = 0

        scenario["overhead_comparable"] = bool(
            direct_ok and not scenario["saturated"] and (scenario["error_rate"] or 0) <= 0.01
        )


def build_results(run_dir: Path, meta: dict[str, Any], manifest: list[dict[str, Any]]) -> dict[str, Any]:
    nonstream_reps: dict[int, dict[str, list[dict[str, Any]]]] = {}
    stream_reps: dict[int, dict[str, list[dict[str, Any]]]] = {}
    embeddings_reps: dict[int, dict[str, list[dict[str, Any]]]] = {}

    for entry in manifest:
        target = entry["target"]
        raw = run_dir / entry["raw"]
        if not raw.is_file():
            raise SystemExit(f"aggregate: missing raw result {raw}")
        cpu = (
            read_cpu(run_dir / entry["cpu"])
            if entry.get("cpu")
            else read_cpu(Path("/nonexistent"))
        )
        doc = _load_json(raw)
        repetition = int(entry.get("repetition", 1))

        if entry["kind"] in ("nonstream", "embeddings"):
            rate = int(entry["rate"])
            scenario = summarize_oha(doc)
            scenario.update(
                {
                    "target": target,
                    "rate": rate,
                    "repetition": repetition,
                    "connections": entry.get("connections"),
                    "duration_secs": entry.get("duration_secs"),
                    "cpu": cpu,
                    "raw": entry["raw"],
                }
            )
            by_kind = nonstream_reps if entry["kind"] == "nonstream" else embeddings_reps
            by_kind.setdefault(rate, {}).setdefault(target, []).append(scenario)
        elif entry["kind"] == "stream":
            concurrency = int(entry["concurrency"])
            scenario = summarize_sse(doc)
            scenario.update(
                {
                    "target": target,
                    "concurrency": concurrency,
                    "repetition": repetition,
                    "duration_secs": entry.get("duration_secs"),
                    "cpu": cpu,
                    "raw": entry["raw"],
                }
            )
            stream_reps.setdefault(concurrency, {}).setdefault(target, []).append(scenario)
        else:
            raise SystemExit(f"aggregate: unknown scenario kind {entry['kind']!r}")

    flags: list[dict[str, Any]] = []
    nonstream: dict[int, dict[str, dict[str, Any]]] = {}
    for rate, per_target in sorted(nonstream_reps.items()):
        group: dict[str, dict[str, Any]] = {}
        for target in TARGETS:
            reps = per_target.get(target)
            if not reps:
                continue
            reps.sort(key=lambda r: r["repetition"])
            group[target] = _median_scenario(reps, "nonstream", rate)
        _finalize_nonstream(group, per_target, rate, flags)
        nonstream[rate] = group

    stream: dict[int, dict[str, dict[str, Any]]] = {}
    for concurrency, per_target in sorted(stream_reps.items()):
        group = {}
        for target in TARGETS:
            reps = per_target.get(target)
            if not reps:
                continue
            reps.sort(key=lambda r: r["repetition"])
            group[target] = _median_scenario(reps, "stream", concurrency)
        _finalize_stream(group, per_target, concurrency, flags)
        stream[concurrency] = group

    embeddings: dict[int, dict[str, dict[str, Any]]] = {}
    for rate, per_target in sorted(embeddings_reps.items()):
        group = {}
        for target in TARGETS:
            reps = per_target.get(target)
            if not reps:
                continue
            reps.sort(key=lambda r: r["repetition"])
            group[target] = _median_scenario(reps, "nonstream", rate)
        _finalize_nonstream(group, per_target, rate, flags, "embeddings")
        embeddings[rate] = group

    return {
        "meta": meta,
        "nonstream": {str(k): v for k, v in sorted(nonstream.items())},
        "stream": {str(k): v for k, v in sorted(stream.items())},
        "embeddings": {str(k): v for k, v in sorted(embeddings.items())},
        "rewrite": _rewrite(nonstream_reps, stream_reps, embeddings_reps, nonstream, stream, embeddings),
        "a6": _a6(nonstream),
        "flags": flags,
    }


def _rewrite(
    nonstream_reps: dict[int, dict[str, list[dict[str, Any]]]],
    stream_reps: dict[int, dict[str, list[dict[str, Any]]]],
    embeddings_reps: dict[int, dict[str, list[dict[str, Any]]]],
    nonstream: dict[int, dict[str, dict[str, Any]]],
    stream: dict[int, dict[str, dict[str, Any]]],
    embeddings: dict[int, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The scanner's cost: lumewarp-rewrite minus lumewarp (the fast path), same
    gateway process and repetition, so direct and the machine cancel out."""
    rows: list[dict[str, Any]] = []

    def add(
        scenario: str,
        load: int,
        reps: dict[str, list[dict[str, Any]]],
        group: dict[str, dict[str, Any]],
        latency: str,
        overhead_key: str,
    ) -> None:
        fast_reps = {r["repetition"]: r for r in reps.get("lumewarp", [])}
        deltas = []
        for rep in reps.get("lumewarp-rewrite", []):
            fast = fast_reps.get(rep["repetition"])
            if fast is not None:
                deltas.append(_delta(rep[latency].get("p99"), fast[latency].get("p99")))
        deltas = [d for d in deltas if d is not None]
        if not deltas:
            return
        fast = group["lumewarp"]
        rewrite = group["lumewarp-rewrite"]
        rows.append(
            {
                "scenario": scenario,
                "load": load,
                "fast_overhead_p99_ms": (fast.get("overhead_ms") or {}).get(overhead_key),
                "rewrite_overhead_p99_ms": (rewrite.get("overhead_ms") or {}).get(overhead_key),
                "delta_p99_ms": _median(deltas),
                "delta_p99_min_ms": min(deltas),
                "delta_p99_max_ms": max(deltas),
                "repetitions": len(deltas),
                "comparable": bool(fast.get("overhead_comparable") and rewrite.get("overhead_comparable")),
            }
        )

    for rate, reps in sorted(nonstream_reps.items()):
        add("non-stream", rate, reps, nonstream[rate], "latency_ms", "p99")
    for concurrency, reps in sorted(stream_reps.items()):
        add("stream TTFT", concurrency, reps, stream[concurrency], "ttft_ms", "ttft_p99")
    for rate, reps in sorted(embeddings_reps.items()):
        add("embeddings", rate, reps, embeddings[rate], "latency_ms", "p99")
    return rows


def _a6(nonstream: dict[int, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """A6 exit gate at 1000 RPS; other rates are informational.

    Pass requires the p99-overhead thresholds *and* that neither Lumewarp nor
    the direct baseline was saturated, and that Lumewarp's error rate is <= 1%.
    """
    out: dict[str, Any] = {}
    for rate, group in sorted(nonstream.items()):
        lw = group.get("lumewarp")
        ll = group.get("litellm")
        direct = group.get("direct")
        lw_p99 = ((lw or {}).get("overhead_ms") or {}).get("p99")
        ll_p99 = ((ll or {}).get("overhead_ms") or {}).get("p99")
        ratio = None
        if lw_p99 is not None and ll_p99 is not None and ll_p99 > 0:
            ratio = round(lw_p99 / ll_p99, 4)

        reasons: list[str] = []
        if lw is None:
            reasons.append("missing lumewarp result")
        else:
            if lw.get("saturated"):
                reasons.append(
                    "lumewarp saturated: " + "; ".join(lw.get("saturation_reasons") or ["yes"])
                )
            if (lw.get("error_rate") or 0) > 0.01:
                reasons.append(f"lumewarp error rate {lw['error_rate'] * 100:.2f}% > 1%")
        if direct is None:
            reasons.append("missing direct baseline")
        else:
            if direct.get("saturated"):
                reasons.append(
                    "direct saturated: " + "; ".join(direct.get("saturation_reasons") or ["yes"])
                )
            if (direct.get("error_rate") or 0) > 0.01:
                reasons.append(f"direct error rate {direct['error_rate'] * 100:.2f}% > 1%")
        if lw_p99 is None:
            reasons.append("missing lumewarp p99 overhead")
        elif lw_p99 >= 1.0:
            reasons.append(f"lumewarp p99 overhead {lw_p99:.3f} ms >= 1 ms")
        if ll_p99 is None:
            reasons.append("missing litellm p99 overhead")
        elif ll_p99 <= 0:
            reasons.append("litellm p99 overhead <= 0, ratio undefined")
        elif ratio is None:
            reasons.append("ratio undefined")
        elif ratio > 0.2:
            reasons.append(f"ratio lumewarp/litellm p99 overhead {ratio:.3f} > 0.2")
        reasons = list(dict.fromkeys(reasons))

        out[str(rate)] = {
            "lumewarp_p99_overhead_ms": lw_p99,
            "litellm_p99_overhead_ms": ll_p99,
            "ratio_lumewarp_over_litellm": ratio,
            "lumewarp_saturated": bool(lw and lw.get("saturated")),
            "lumewarp_error_rate": (lw or {}).get("error_rate"),
            "direct_saturated": bool(direct and direct.get("saturated")),
            "judged_at_1000_rps": rate == 1000,
            "pass": not reasons,
            "reasons": reasons,
        }
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_range(median: Any, low: Any, high: Any, digits: int = 2) -> str:
    if median is None:
        return "–"
    if low is None or high is None or round(float(low), digits) == round(float(high), digits):
        return _fmt(median, digits)
    return f"{_fmt(median, digits)} [{_fmt(low, digits)}–{_fmt(high, digits)}]"


def _flag_text(scenario: dict[str, Any]) -> str:
    if not scenario.get("saturated"):
        return ""
    return "⚠ " + "; ".join(scenario.get("saturation_reasons") or ["saturated"])


def _cmp_text(scenario: dict[str, Any], target: str) -> str:
    if target == "direct":
        return "baseline"
    return "yes" if scenario.get("overhead_comparable") else "no"


def _mem_cell(cpu: dict[str, Any], mem_kind: str | None) -> str:
    value = _fmt(cpu.get("max_mem_mb"), 1)
    if mem_kind and mem_kind != "pss":
        return f"{value} ({mem_kind})"
    return value


def render_summary(results: dict[str, Any]) -> str:
    meta = results.get("meta") or {}
    machine = meta.get("machine") or {}
    versions = meta.get("versions") or {}
    config = meta.get("config") or {}
    git = meta.get("git") or {}
    auth = meta.get("auth") or "off"
    lines: list[str] = []
    add = lines.append

    add("# Lumewarp bench summary")
    add("")
    add(f"- run: `{meta.get('run_id', '?')}` ({meta.get('profile', '?')} profile)")
    add(
        f"- machine: {machine.get('nproc', '?')} × {machine.get('cpu_model', '?')} "
        f"({machine.get('kernel', '?')})"
    )
    add(f"- lumewarp: `{git.get('describe') or git.get('sha') or '?'}`")
    add(f"- litellm: `{versions.get('litellm', '?')}` (pinned)")
    add(f"- oha: `{versions.get('oha', '?')}`")
    add(
        f"- mock: latency {config.get('mock_latency_ms', '?')} ms, "
        f"{config.get('mock_stream_chunks', '?')} stream chunks × "
        f"{config.get('mock_chunk_interval_ms', '?')} ms"
    )
    add(f"- repetitions: {config.get('repetitions', 1)} (medians shown)")
    if auth == "on":
        add("- client auth: on (`BENCH_AUTH=1`, one per-run bearer key on both gateways)")
    else:
        add("- client auth: none on both gateways (bench config)")
    add("")
    add("**Overhead** = latency through the gateway − latency direct to the mock upstream,")
    add("measured at the same offered load. Negative values are run-to-run noise.")
    add("")
    add("**Overhead is only comparable where `cmp` is `yes`**, i.e. both direct and")
    add("the target achieved ≥ 90% of the offered rate with ≤ 1% errors. Rows with")
    add("`cmp = no` are informational only. With repetitions > 1, values are medians")
    add("and overhead p99 is shown as `median [min–max]`.")
    add("")

    add("## Non-stream (`POST /v1/chat/completions`, keep-alive, fixed rate)")
    add("")
    add(
        "| rate | target | p50 ms | p90 ms | p99 ms | rps | err % | cmp | "
        "ovh p50 ms | ovh p99 ms | cpu avg/max % | PSS max MiB | flags |"
    )
    add("|---:|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")
    for rate, group in results.get("nonstream", {}).items():
        for target in TARGETS:
            s = group.get(target)
            if not s:
                continue
            lat = s.get("latency_ms") or {}
            ovh = s.get("overhead_ms") or {}
            cpu = s.get("cpu") or {}
            add(
                f"| {rate} | {target} | {_fmt(lat.get('p50'))} | {_fmt(lat.get('p90'))} | "
                f"{_fmt(lat.get('p99'))} | {_fmt(s.get('rps'), 0)} | "
                f"{_fmt((s.get('error_rate') or 0) * 100, 2)} | "
                f"{_cmp_text(s, target)} | {_fmt(ovh.get('p50'))} | "
                f"{_fmt_range(ovh.get('p99'), s.get('overhead_p99_min'), s.get('overhead_p99_max'))} | "
                f"{_fmt(cpu.get('avg_pct'), 1)}/{_fmt(cpu.get('max_pct'), 1)} | "
                f"{_mem_cell(cpu, s.get('mem_kind'))} | {_flag_text(s)} |"
            )
    add("")

    add("## Stream (`POST /v1/chat/completions`, SSE, fixed concurrency)")
    add("")
    add(
        "| conc | target | rps | ttft p50 ms | ttft p99 ms | total p50 ms | total p99 ms | "
        "err % | ttft ovh p50 | ttft ovh p99 | cpu avg/max % | PSS max MiB | flags |"
    )
    add("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for concurrency, group in results.get("stream", {}).items():
        for target in TARGETS:
            s = group.get(target)
            if not s:
                continue
            ttft = s.get("ttft_ms") or {}
            total = s.get("total_ms") or {}
            ovh = s.get("overhead_ms") or {}
            cpu = s.get("cpu") or {}
            add(
                f"| {concurrency} | {target} | {_fmt(s.get('rps'), 0)} | {_fmt(ttft.get('p50'))} | "
                f"{_fmt(ttft.get('p99'))} | {_fmt(total.get('p50'))} | {_fmt(total.get('p99'))} | "
                f"{_fmt((s.get('error_rate') or 0) * 100, 2)} | {_fmt(ovh.get('ttft_p50'))} | "
                f"{_fmt_range(ovh.get('ttft_p99'), s.get('overhead_ttft_p99_min'), s.get('overhead_ttft_p99_max'))} | "
                f"{_fmt(cpu.get('avg_pct'), 1)}/{_fmt(cpu.get('max_pct'), 1)} | "
                f"{_mem_cell(cpu, s.get('mem_kind'))} | {_flag_text(s)} |"
            )
    add("")
    add("Stream `rps` uses successful requests only (`ok_rps`) when sse-bench provides it.")
    add("")

    if results.get("embeddings"):
        add(f"## Embeddings (`POST /v1/embeddings`, at least {config.get('embed_bytes', '?')} bytes, `model` after `data`)")
        add("")
        add("| rate | target | p50 ms | p99 ms | rps | err % | cmp | ovh p50 ms | ovh p99 ms | cpu avg/max % | PSS max MiB | flags |")
        add("|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")
        for rate, group in results["embeddings"].items():
            for target in TARGETS:
                s = group.get(target)
                if not s:
                    continue
                lat = s.get("latency_ms") or {}
                ovh = s.get("overhead_ms") or {}
                cpu = s.get("cpu") or {}
                add(
                    f"| {rate} | {target} | {_fmt(lat.get('p50'))} | {_fmt(lat.get('p99'))} | "
                    f"{_fmt(s.get('rps'), 0)} | {_fmt((s.get('error_rate') or 0) * 100, 2)} | "
                    f"{_cmp_text(s, target)} | {_fmt(ovh.get('p50'))} | "
                    f"{_fmt_range(ovh.get('p99'), s.get('overhead_p99_min'), s.get('overhead_p99_max'))} | "
                    f"{_fmt(cpu.get('avg_pct'), 1)}/{_fmt(cpu.get('max_pct'), 1)} | "
                    f"{_mem_cell(cpu, s.get('mem_kind'))} | {_flag_text(s)} |"
                )
        add("")

    if results.get("rewrite"):
        add("## Response rewrite (`lumewarp-rewrite` against the fast path)")
        add("")
        add("`lumewarp-rewrite` calls the same pool under a name that differs from")
        add("`upstream_model`, so every response goes through the scanner. Delta p99 is")
        add("`lumewarp-rewrite` minus `lumewarp` per repetition, `median [min–max]`.")
        add("")
        add("| scenario | rate / conc | fast ovh p99 ms | rewrite ovh p99 ms | delta p99 ms | cmp |")
        add("|---|---:|---:|---:|---:|---|")
        for row in results["rewrite"]:
            add(
                f"| {row['scenario']} | {row['load']} | {_fmt(row.get('fast_overhead_p99_ms'))} | "
                f"{_fmt(row.get('rewrite_overhead_p99_ms'))} | "
                f"{_fmt_range(row.get('delta_p99_ms'), row.get('delta_p99_min_ms'), row.get('delta_p99_max_ms'))} | "
                f"{'yes' if row.get('comparable') else 'no'} |"
            )
        add("")

    add("## A6 gate (overhead p99, non-stream)")
    add("")
    add("A6 is judged at **1000 RPS**; other rates are informational. A row passes only")
    add("if the p99-overhead thresholds hold, Lumewarp and direct both held the offered")
    add("rate without saturation, and Lumewarp's error rate is ≤ 1%.")
    add("")
    add("| rate | role | lumewarp ovh p99 ms | litellm ovh p99 ms | ratio lw/ll | pass | reasons |")
    add("|---:|---|---:|---:|---:|---|---|")
    for rate, a6 in results.get("a6", {}).items():
        role = "gate" if a6.get("judged_at_1000_rps") else "info"
        reasons = "; ".join(a6.get("reasons") or []) or "–"
        add(
            f"| {rate} | {role} | {_fmt(a6.get('lumewarp_p99_overhead_ms'))} | "
            f"{_fmt(a6.get('litellm_p99_overhead_ms'))} | "
            f"{_fmt(a6.get('ratio_lumewarp_over_litellm'), 3)} | "
            f"{_fmt(a6.get('pass'))} | {reasons} |"
        )
    add("")

    flags = results.get("flags") or []
    add("## Saturation flags")
    add("")
    if flags:
        for flag in flags:
            add(f"- `{flag['scenario']}`: {'; '.join(flag['reasons'])}")
        add("")
        add("A flag means the target could not hold the offered load; it does not abort the run.")
    else:
        add("None: every target held the offered load with < 1% errors.")
    add("")

    add("## Machine")
    add("")
    add("```")
    add(f"nproc        {machine.get('nproc', '?')}")
    add(f"cpu          {machine.get('cpu_model', '?')}")
    add(f"kernel       {machine.get('kernel', '?')}")
    add(f"os           {machine.get('os', '?')}")
    add(f"memory       {machine.get('mem_total_mb', '?')} MiB")
    add(f"lumewarp git {git.get('describe') or git.get('sha') or '?'}")
    add(f"litellm      {versions.get('litellm', '?')}")
    add(f"oha          {versions.get('oha', '?')}")
    add(f"python       {versions.get('python', '?')}")
    add(f"repetitions  {config.get('repetitions', 1)}")
    add("```")
    add("")
    add(
        f"Raw oha / sse-bench JSON and per-scenario `/proc` samples are in this "
        f"directory (`{meta.get('run_id', '?')}/`)."
    )
    add("")
    return "\n".join(lines)


def write_outputs(run_dir: Path, results: dict[str, Any]) -> tuple[Path, Path]:
    results_path = run_dir / "results.json"
    summary_path = run_dir / "summary.md"
    results_path.write_text(json.dumps(results, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    summary_path.write_text(render_summary(results), encoding="utf-8")
    return results_path, summary_path


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _oha_doc(
    p50: float,
    p90: float,
    p99: float,
    rps: float,
    statuses: dict[str, int],
    errors: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "latencyPercentiles": {"p50": p50 / 1000, "p90": p90 / 1000, "p99": p99 / 1000},
        "metrics": {
            "requests_per_sec": rps,
            "latency_ms": {"mean": p50, "max": p99, "p50": p50, "p99": p99},
        },
        "statusCodeDistribution": statuses,
        "errorDistribution": errors or {},
    }


def _sse_doc(
    ttft_p50: float,
    ttft_p99: float,
    total_p50: float,
    total_p99: float,
    rps: float | None,
    requests: int,
    errors: int,
    ok_rps: float | None = None,
) -> dict[str, Any]:
    doc = {
        "requests": requests,
        "errors": errors,
        "ttft_ms": {"p50": ttft_p50, "p90": ttft_p50, "p99": ttft_p99, "max": ttft_p99},
        "total_ms": {"p50": total_p50, "p90": total_p50, "p99": total_p99, "max": total_p99},
    }
    if rps is not None:
        doc["rps"] = rps
    if ok_rps is not None:
        doc["ok_rps"] = ok_rps
    return doc


def _write_cpu(path: Path, cpu_values: list[float], pss_kb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("elapsed_s,cpu_pct,pss_kb,nprocs\n")
        for i, cpu in enumerate(cpu_values):
            fh.write(f"{i + 1}.0,{cpu},{pss_kb},1\n")


def _write_case(
    run_dir: Path,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Write raw files + manifest entries; returns the manifest."""
    manifest: list[dict[str, Any]] = []
    for case in cases:
        raw_path = run_dir / case["raw"]
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(case["doc"]), encoding="utf-8")
        _write_cpu(run_dir / case["cpu"], case["cpu_values"], case["pss_kb"])
        entry: dict[str, Any] = {
            "kind": case["kind"],
            "target": case["target"],
            "repetition": case["repetition"],
            "duration_secs": case.get("duration_secs", 15),
            "raw": case["raw"],
            "cpu": case["cpu"],
        }
        if case["kind"] == "nonstream":
            entry["rate"] = case["key"]
            entry["connections"] = case.get("connections", 64)
        else:
            entry["concurrency"] = case["key"]
        manifest.append(entry)
    (run_dir / "scenarios.jsonl").write_text(
        "\n".join(json.dumps(e) for e in manifest) + "\n", encoding="utf-8"
    )
    return manifest


SELFTEST_META: dict[str, Any] = {
    "run_id": "selftest",
    "profile": "quick",
    "machine": {
        "nproc": 4,
        "cpu_model": "Synthetic CPU @ 1GHz",
        "kernel": "0.0-selftest",
        "os": "selftest",
        "mem_total_mb": 1024,
    },
    "versions": {"litellm": "1.106.2", "oha": "oha 1.16.0", "python": "3.12.0"},
    "git": {"sha": "deadbee", "describe": "deadbee-dirty"},
    "config": {
        "mock_latency_ms": 50,
        "mock_stream_chunks": 20,
        "mock_chunk_interval_ms": 10,
        "repetitions": 3,
    },
}


def _selftest_main_case() -> int:
    """Three repetitions + a 404-free scenario set; checks medians/min/max."""
    assertions = 0

    def check(condition: bool, label: str) -> None:
        nonlocal assertions
        assert condition, label
        assertions += 1

    with tempfile.TemporaryDirectory(prefix="lumewarp-bench-selftest-") as tmp:
        run_dir = Path(tmp)
        cases: list[dict[str, Any]] = []
        # Non-stream: direct is stable; Lumewarp p50/p99 vary slightly across
        # reps; LiteLLM is saturated in every rep (880 rps + 200x500).
        for rep in (1, 2, 3):
            cases.append(
                {
                    "kind": "nonstream",
                    "target": "direct",
                    "key": 1000,
                    "repetition": rep,
                    "raw": f"nonstream/direct/rate-1000.rep{rep}.json",
                    "cpu": f"nonstream/direct/rate-1000.rep{rep}.cpu.csv",
                    "doc": _oha_doc(
                        50.0, 50.5, 51.0, 1000.0 if rep != 2 else 999.0,
                        {"200": 15000}, {"aborted due to deadline": 3},
                    ),
                    "cpu_values": [10.0, 12.0, 13.0],
                    "pss_kb": 40 * 1024,
                }
            )
            lw_p50 = {1: 51.0, 2: 51.4, 3: 51.6}[rep]
            lw_p99 = {1: 51.7, 2: 51.9, 3: 52.1}[rep]
            cases.append(
                {
                    "kind": "nonstream",
                    "target": "lumewarp",
                    "key": 1000,
                    "repetition": rep,
                    "raw": f"nonstream/lumewarp/rate-1000.rep{rep}.json",
                    "cpu": f"nonstream/lumewarp/rate-1000.rep{rep}.cpu.csv",
                    "doc": _oha_doc(lw_p50, 51.2, lw_p99, 1000.0, {"200": 15000}),
                    "cpu_values": {1: [30.0, 55.0], 2: [32.0, 60.0], 3: [30.0, 50.0]}[rep],
                    "pss_kb": 30 * 1024,
                }
            )
            cases.append(
                {
                    "kind": "nonstream",
                    "target": "litellm",
                    "key": 1000,
                    "repetition": rep,
                    "raw": f"nonstream/litellm/rate-1000.rep{rep}.json",
                    "cpu": f"nonstream/litellm/rate-1000.rep{rep}.cpu.csv",
                    "doc": _oha_doc(
                        55.0, 60.0, 120.0, 880.0,
                        {"200": 13200, "500": 200}, {"aborted due to deadline": 12},
                    ),
                    "cpu_values": [400.0, 900.0],
                    "pss_kb": 900 * 1024,
                }
            )
            # Stream: direct/lumewarp succeed; litellm has 1.5% errors. `ok_rps`
            # is present for direct/lumewarp to exercise the preference.
            cases.append(
                {
                    "kind": "stream",
                    "target": "direct",
                    "key": 100,
                    "repetition": rep,
                    "raw": f"stream/direct/c100.rep{rep}.json",
                    "cpu": f"stream/direct/c100.rep{rep}.cpu.csv",
                    "doc": _sse_doc(51.0, 53.0, 200.0, 210.0, 480.0, 7500, 0, ok_rps=505.0),
                    "cpu_values": [10.0, 20.0],
                    "pss_kb": 40 * 1024,
                }
            )
            cases.append(
                {
                    "kind": "stream",
                    "target": "lumewarp",
                    "key": 100,
                    "repetition": rep,
                    "raw": f"stream/lumewarp/c100.rep{rep}.json",
                    "cpu": f"stream/lumewarp/c100.rep{rep}.cpu.csv",
                    "doc": _sse_doc(52.0, 56.0, 201.0, 220.0, 400.0, 7400, 0, ok_rps=495.0),
                    "cpu_values": [25.0, 40.0],
                    "pss_kb": 30 * 1024,
                }
            )
            cases.append(
                {
                    "kind": "stream",
                    "target": "litellm",
                    "key": 100,
                    "repetition": rep,
                    "raw": f"stream/litellm/c100.rep{rep}.json",
                    "cpu": f"stream/litellm/c100.rep{rep}.cpu.csv",
                    "doc": _sse_doc(60.0, 90.0, 210.0, 400.0, 400.0, 6000, 90),
                    "cpu_values": [200.0, 500.0],
                    "pss_kb": 800 * 1024,
                }
            )

        manifest = _write_case(run_dir, cases)
        results = build_results(run_dir, SELFTEST_META, manifest)

        direct = results["nonstream"]["1000"]["direct"]
        lumewarp = results["nonstream"]["1000"]["lumewarp"]
        litellm = results["nonstream"]["1000"]["litellm"]

        check(lumewarp["repetitions"] == 3, "3 repetitions aggregated")
        check(direct["latency_ms"]["p50"] == 50.0, "direct p50 parsed from seconds")
        check(lumewarp["latency_ms"]["p50"] == 51.4, "median lumewarp p50 across reps")
        check(lumewarp["latency_ms"]["p99"] == 51.9, "median lumewarp p99 across reps")
        check(lumewarp["overhead_ms"]["p50"] == 1.4, "median p50 overhead")
        check(lumewarp["overhead_ms"]["p99"] == 0.9, "median p99 overhead")
        check(lumewarp["overhead_p99_min"] == 0.7, "p99 overhead min")
        check(lumewarp["overhead_p99_max"] == 1.1, "p99 overhead max")
        check(lumewarp["saturated"] is False, "lumewarp not saturated")
        check(lumewarp["saturated_repetitions"] == 0, "no saturated lumewarp reps")
        check(lumewarp["overhead_comparable"] is True, "lumewarp row comparable")
        # 200x500 out of 13400 responses + 12 deadline aborts: deadline aborts
        # are reported but excluded from the error rate.
        check(litellm["error_rate"] == round(200 / 13400, 6), "litellm error rate excludes deadline aborts")
        check(litellm["deadline_aborts"] == 12, "deadline aborts reported")
        check(litellm["saturated"] is True, "litellm saturated by low rps and errors")
        check(litellm["saturated_repetitions"] == 3, "all litellm reps saturated")
        check(len(litellm["saturation_reasons"]) == 2, "two aggregated saturation reasons")
        check(litellm["overhead_comparable"] is False, "saturated litellm row not comparable")
        check(lumewarp["cpu"]["avg_pct"] == 42.5, "median cpu average from csv")
        check(lumewarp["cpu"]["max_pct"] == 55.0, "median cpu max from csv")
        check(lumewarp["cpu"]["max_mem_mb"] == 30.0, "median PSS max in MiB")
        check(lumewarp["mem_kind"] == "pss", "memory labelled PSS")
        check(results["a6"]["1000"]["pass"] is True, "A6 passes in synthetic data")
        check(
            abs(results["a6"]["1000"]["ratio_lumewarp_over_litellm"] - 0.9 / 69.0) < 1e-3,
            "A6 ratio",
        )
        check(results["a6"]["1000"]["judged_at_1000_rps"] is True, "A6 judged at 1k")
        check(results["a6"]["1000"]["reasons"] == [], "A6 pass has no reasons")

        lw_stream = results["stream"]["100"]["lumewarp"]
        check(lw_stream["rps"] == 495.0, "stream rps prefers ok_rps")
        check(lw_stream["cpu"]["avg_pct"] == 32.5, "stream cpu average")
        check(results["stream"]["100"]["direct"]["rps"] == 505.0, "direct stream ok_rps")
        check(results["stream"]["100"]["litellm"]["saturated"] is True, "stream error saturation flag")
        check(lw_stream["overhead_ms"]["ttft_p99"] == 3.0, "stream ttft overhead")
        check(lw_stream["overhead_comparable"] is True, "stream row comparable")
        check(
            [f["scenario"] for f in results["flags"]]
            == ["nonstream/1000/litellm", "stream/100/litellm"],
            "flag list",
        )

        results_path, summary_path = write_outputs(run_dir, results)
        check(results_path.is_file() and summary_path.is_file(), "outputs written")
        summary = summary_path.read_text(encoding="utf-8")
        for needle in (
            "| 1000 | lumewarp | 51.40 |",
            "0.90 [0.70–1.10]",
            "PSS max MiB",
            "`nonstream/1000/litellm`",
            "3/3 repetitions: error rate 1.49%",
            "Synthetic CPU @ 1GHz",
            "litellm: `1.106.2`",
            "| 1000 | gate |",
        ):
            check(needle in summary, f"summary contains {needle!r}")
    return assertions


def _selftest_a6_404() -> int:
    """Lumewarp returning 404 for every request must fail A6."""
    assertions = 0

    def check(condition: bool, label: str) -> None:
        nonlocal assertions
        assert condition, label
        assertions += 1

    with tempfile.TemporaryDirectory(prefix="lumewarp-bench-selftest-404-") as tmp:
        run_dir = Path(tmp)
        cases = [
            {
                "kind": "nonstream",
                "target": "direct",
                "key": 1000,
                "repetition": 1,
                "raw": "nonstream/direct/rate-1000.rep1.json",
                "cpu": "nonstream/direct/rate-1000.rep1.cpu.csv",
                "doc": _oha_doc(50.0, 50.5, 51.0, 1000.0, {"200": 15000}),
                "cpu_values": [10.0, 12.0],
                "pss_kb": 40 * 1024,
            },
            {
                "kind": "nonstream",
                "target": "lumewarp",
                "key": 1000,
                "repetition": 1,
                "raw": "nonstream/lumewarp/rate-1000.rep1.json",
                "cpu": "nonstream/lumewarp/rate-1000.rep1.cpu.csv",
                # Every request is a fast 404: low latency, negative-ish
                # overhead, 100% error rate. Must not pass A6.
                "doc": _oha_doc(50.5, 50.6, 50.7, 1000.0, {"404": 15000}),
                "cpu_values": [5.0, 6.0],
                "pss_kb": 10 * 1024,
            },
            {
                "kind": "nonstream",
                "target": "litellm",
                "key": 1000,
                "repetition": 1,
                "raw": "nonstream/litellm/rate-1000.rep1.json",
                "cpu": "nonstream/litellm/rate-1000.rep1.cpu.csv",
                "doc": _oha_doc(55.0, 60.0, 120.0, 1000.0, {"200": 15000}),
                "cpu_values": [400.0, 900.0],
                "pss_kb": 900 * 1024,
            },
        ]
        manifest = _write_case(run_dir, cases)
        results = build_results(run_dir, {**SELFTEST_META, "config": {**SELFTEST_META["config"], "repetitions": 1}}, manifest)
        lw = results["nonstream"]["1000"]["lumewarp"]
        a6 = results["a6"]["1000"]
        check(lw["error_rate"] == 1.0, "404 error rate is 100%")
        check(lw["overhead_ms"]["p99"] < 1.0, "404 overhead p99 above threshold would pass old gate")
        check(lw["saturated"] is True, "404 lumewarp is saturated")
        check(lw["overhead_comparable"] is False, "404 row not comparable")
        check(a6["pass"] is False, "A6 fails on all-404 lumewarp")
        check(any("error rate" in reason for reason in a6["reasons"]), "A6 reason mentions error rate")
        check(a6["lumewarp_error_rate"] == 1.0, "A6 records lumewarp error rate")
    return assertions


def _selftest_rewrite() -> int:
    assertions = 0

    def check(condition: bool, label: str) -> None:
        nonlocal assertions
        if not condition:
            raise AssertionError(label)
        assertions += 1

    with tempfile.TemporaryDirectory(prefix="lumewarp-bench-selftest-rewrite-") as tmp:
        run_dir = Path(tmp)
        manifest: list[dict[str, Any]] = []
        latency = {
            ("nonstream", "direct"): 50.0,
            ("nonstream", "lumewarp"): 50.2,
            ("nonstream", "lumewarp-rewrite"): 50.5,
            ("embeddings", "direct"): 60.0,
            ("embeddings", "lumewarp"): 62.0,
            ("embeddings", "lumewarp-rewrite"): 66.0,
        }
        for (kind, target), p99 in latency.items():
            rate = 1000 if kind == "nonstream" else 50
            for rep in (1, 2):
                raw = f"{kind}/{target}/rate-{rate}.rep{rep}.json"
                cpu = f"{kind}/{target}/rate-{rate}.rep{rep}.cpu.csv"
                (run_dir / raw).parent.mkdir(parents=True, exist_ok=True)
                (run_dir / raw).write_text(
                    json.dumps(_oha_doc(p99 - 1, p99 - 0.5, p99 + rep / 10, rate, {"200": rate * 15})),
                    encoding="utf-8",
                )
                _write_cpu(run_dir / cpu, [10.0, 20.0], 1024)
                manifest.append(
                    {
                        "kind": kind,
                        "target": target,
                        "rate": rate,
                        "repetition": rep,
                        "connections": 64,
                        "duration_secs": 15,
                        "raw": raw,
                        "cpu": cpu,
                    }
                )
        results = build_results(run_dir, {"config": {"embed_bytes": 3145728}}, manifest)
        rows = {row["scenario"]: row for row in results["rewrite"]}
        check(set(rows) == {"non-stream", "embeddings"}, "one row per scenario with both targets")
        check(rows["non-stream"]["delta_p99_ms"] == 0.3, "paired delta p99")
        check(rows["embeddings"]["delta_p99_ms"] == 4.0, "embeddings delta p99")
        check(rows["embeddings"]["fast_overhead_p99_ms"] == 2.0, "fast path overhead")
        check(rows["embeddings"]["rewrite_overhead_p99_ms"] == 6.0, "rewrite overhead")
        check(results["a6"]["1000"]["lumewarp_p99_overhead_ms"] == 0.2, "A6 stays on the fast path")
        summary = render_summary(results)
        check("## Response rewrite" in summary, "rewrite section rendered")
        check("## Embeddings" in summary, "embeddings section rendered")
        check("3145728 bytes" in summary, "embeddings size shown")
        empty = build_results(run_dir, {}, [e for e in manifest if e["target"] != "lumewarp-rewrite"])
        check(empty["rewrite"] == [], "no rewrite target, no rows")
    return assertions


def run_selftest() -> int:
    assertions = _selftest_main_case() + _selftest_a6_404() + _selftest_rewrite()
    print(f"selftest: OK ({assertions} assertions)")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Aggregate one Lumewarp bench run")
    parser.add_argument("--run-dir", help="directory written by bench/scripts/run.sh")
    parser.add_argument("--selftest", action="store_true", help="run built-in synthetic checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.run_dir:
        parser.error("--run-dir is required (or use --selftest)")

    run_dir = Path(args.run_dir)
    try:
        meta = _load_json(run_dir / "meta.json")
        manifest = load_manifest(run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"aggregate: {exc}", file=sys.stderr)
        return 1

    results = build_results(run_dir, meta, manifest)
    results_path, summary_path = write_outputs(run_dir, results)
    print(f"wrote {results_path}")
    print(f"wrote {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

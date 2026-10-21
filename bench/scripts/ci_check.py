#!/usr/bin/env python3
"""CI gate for one Lumewarp-vs-LiteLLM bench run.

Reads one run's results.json (--results-dir pointing at a run directory, or at
the results root to pick the newest run; or --results-json) and fails on:

  * run.sh did not exit successfully (--run-outcome)
  * any Lumewarp scenario with > 1% errors, or marked saturated
  * Lumewarp non-stream overhead p99 > 5 ms (when Lumewarp is not saturated)
  * Lumewarp stream TTFT overhead p99 > 10 ms (when Lumewarp is not saturated)
  * at the A6 rate (1000 RPS): Lumewarp non-stream overhead p99 is worse than
    LiteLLM's

The *absolute* A6 thresholds (p99 < 1 ms and ratio <= 20%) are only printed as
`::warning::` annotations: GitHub-hosted runners are noisy and A6 is judged on
an idle machine. The 5 ms / 10 ms CI ceilings are deliberately loose hard fails.

On runners with <= 4 CPUs, saturation and >1% error failures for non-stream
rates above 1000 RPS are downgraded to warnings, so a manual `--full` dispatch
can run without being dominated by 5k/10k capacity limits on a small runner.

Stdlib only; run with `python3 -I ci_check.py --results-dir bench/results`.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

FAIL_ERROR_RATE = 0.01
A6_RATE = "1000"
A6_ABSOLUTE_P99_MS = 1.0
A6_RATIO = 0.2
CI_NONSTREAM_P99_CEILING_MS = 5.0
CI_STREAM_TTFT_P99_CEILING_MS = 10.0
SMALL_RUNNER_CPUS = 4
SMALL_RUNNER_MAX_HARD_RATE = 1000


def _default_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _fmt(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "–"


def _sorted_load_keys(mapping: dict[str, Any]) -> list[str]:
    return sorted(
        mapping, key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k))
    )


def _lumewarp_scenarios(
    results: dict[str, Any],
) -> list[tuple[str, int | None, str, dict[str, Any]]]:
    """All Lumewarp scenarios: (kind, non-stream rate, label, scenario)."""
    out: list[tuple[str, int | None, str, dict[str, Any]]] = []
    nonstream = results.get("nonstream") or {}
    for key in _sorted_load_keys(nonstream):
        scenario = (nonstream.get(key) or {}).get("lumewarp")
        if isinstance(scenario, dict):
            rate = int(key) if str(key).isdigit() else None
            out.append(("nonstream", rate, f"non-stream {key} RPS", scenario))
    stream = results.get("stream") or {}
    for key in _sorted_load_keys(stream):
        scenario = (stream.get(key) or {}).get("lumewarp")
        if isinstance(scenario, dict):
            out.append(("stream", None, f"stream concurrency {key}", scenario))
    return out


def check(
    results: dict[str, Any],
    run_outcome: str = "success",
    cpus: int | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (failures, warnings, report lines) for one results document."""
    failures: list[str] = []
    warnings: list[str] = []
    cpus = cpus if cpus is not None else _default_cpus()
    defer_high_rates = cpus <= SMALL_RUNNER_CPUS
    lines: list[str] = [
        f"run.sh outcome: {run_outcome}",
        f"runner CPUs: {cpus}",
    ]
    if defer_high_rates:
        lines.append(
            f"small runner: Lumewarp saturation/errors at non-stream rates > "
            f"{SMALL_RUNNER_MAX_HARD_RATE} RPS are warnings"
        )

    if run_outcome != "success":
        failures.append(f"run.sh did not succeed (outcome: {run_outcome})")

    scenarios = _lumewarp_scenarios(results)
    if not scenarios:
        failures.append("no Lumewarp scenarios found in results.json")
    for kind, rate, label, scenario in scenarios:
        error_rate = scenario.get("error_rate")
        rps = scenario.get("rps")
        saturated = bool(scenario.get("saturated"))
        overhead = scenario.get("overhead_ms") or {}
        if kind == "nonstream":
            overhead_p99 = overhead.get("p99")
            overhead_label = "overhead p99"
            ceiling = CI_NONSTREAM_P99_CEILING_MS
        else:
            overhead_p99 = overhead.get("ttft_p99")
            overhead_label = "TTFT overhead p99"
            ceiling = CI_STREAM_TTFT_P99_CEILING_MS
        deferred = bool(
            defer_high_rates
            and kind == "nonstream"
            and rate is not None
            and rate > SMALL_RUNNER_MAX_HARD_RATE
        )

        if error_rate is None:
            failures.append(f"{label}: missing Lumewarp error_rate")
        elif error_rate > FAIL_ERROR_RATE:
            message = f"{label}: Lumewarp error rate {error_rate * 100:.2f}% > 1%"
            if deferred:
                warnings.append(
                    f"{message} (rate > {SMALL_RUNNER_MAX_HARD_RATE} on a "
                    f"<= {SMALL_RUNNER_CPUS} CPU runner; warning only)"
                )
            else:
                failures.append(message)
        if saturated:
            reasons = scenario.get("saturation_reasons") or []
            suffix = f" ({'; '.join(str(r) for r in reasons)})" if reasons else ""
            message = f"{label}: Lumewarp saturated{suffix}"
            if deferred:
                warnings.append(
                    f"{message} (rate > {SMALL_RUNNER_MAX_HARD_RATE} on a "
                    f"<= {SMALL_RUNNER_CPUS} CPU runner; warning only)"
                )
            else:
                failures.append(message)

        # Loose absolute CI ceiling; skipped for saturated scenarios (those
        # already fail, except for deferred high rates on small runners).
        if not saturated:
            if overhead_p99 is None:
                warnings.append(
                    f"{label}: missing Lumewarp {overhead_label}; absolute CI ceiling not checked"
                )
            elif float(overhead_p99) > ceiling:
                failures.append(
                    f"{label}: Lumewarp {overhead_label} {float(overhead_p99):.3f} ms "
                    f"> {ceiling:g} ms absolute CI ceiling"
                )

        lines.append(
            f"- {label}: rps={_fmt(rps, 1)}, error_rate="
            f"{_fmt(error_rate * 100 if error_rate is not None else None, 2)}%, "
            f"saturated={str(saturated).lower()}, "
            f"{overhead_label}={_fmt(overhead_p99)} ms"
        )

    a6 = results.get("a6") or {}
    gate: tuple[str, dict[str, Any]] | None = None
    for rate, row in sorted(a6.items()):
        if not isinstance(row, dict):
            continue
        if row.get("judged_at_1000_rps") or str(rate) == A6_RATE:
            gate = (str(rate), row)
            break
    if gate is None:
        failures.append(f"A6 row for {A6_RATE} RPS missing from results.json")
    else:
        gate_rate, row = gate
        lw = row.get("lumewarp_p99_overhead_ms")
        ll = row.get("litellm_p99_overhead_ms")
        lines.append(
            f"A6 rate {gate_rate}: Lumewarp p99 overhead {_fmt(lw)} ms vs "
            f"LiteLLM {_fmt(ll)} ms"
        )
        if lw is None or ll is None:
            failures.append(f"A6 {gate_rate}: missing p99 overhead values")
        else:
            lw = float(lw)
            ll = float(ll)
            if lw > ll:
                failures.append(
                    f"A6 {gate_rate}: Lumewarp p99 overhead {lw:.3f} ms is worse "
                    f"than LiteLLM's {ll:.3f} ms"
                )
            if lw >= A6_ABSOLUTE_P99_MS:
                warnings.append(
                    f"A6 {gate_rate}: Lumewarp p99 overhead {lw:.3f} ms >= "
                    f"{A6_ABSOLUTE_P99_MS:g} ms (absolute A6 threshold; not a CI failure)"
                )
            if ll > 0:
                ratio = lw / ll
                if ratio > A6_RATIO:
                    warnings.append(
                        f"A6 {gate_rate}: Lumewarp/LiteLLM p99 overhead ratio "
                        f"{ratio:.3f} > {A6_RATIO:g} (absolute A6 threshold; not a CI failure)"
                    )
            elif lw > 0:
                warnings.append(
                    f"A6 {gate_rate}: LiteLLM p99 overhead is {ll:.3f} ms; ratio not meaningful"
                )

        # Other A6 complaints (e.g. direct saturated) are warnings in CI; the
        # hard failures for Lumewarp errors/saturation are handled above.
        for reason in row.get("reasons") or []:
            reason = str(reason)
            if ">= 1 ms" in reason or "> 0.2" in reason:
                continue
            # Lumewarp errors/saturation are hard failures above; other A6
            # complaints (direct saturated, missing values, ...) are warnings.
            if "lumewarp" in reason and ("error rate" in reason or "saturated" in reason):
                continue
            warnings.append(f"A6 {gate_rate}: {reason}")

    return failures, warnings, lines


def emit(results_path: Path, failures: list[str], warnings: list[str], lines: list[str]) -> int:
    print(f"results: {results_path}")
    for line in lines:
        print(line)
    for warning in warnings:
        print(f"::warning::{warning}")
    for failure in failures:
        print(f"::error::{failure}")
    if failures:
        print(f"ci_check: FAIL ({len(failures)} failure(s), {len(warnings)} warning(s))")
        return 1
    print(f"ci_check: PASS ({len(warnings)} warning(s))")
    return 0


def resolve_results(results_dir: Path) -> Path | None:
    """Accept a run dir (contains results.json) or a results root (newest run)."""
    direct = results_dir / "results.json"
    if direct.is_file():
        return direct
    paths = list(results_dir.glob("*/results.json"))
    if not paths:
        return None
    return max(paths, key=lambda p: (p.parent.name, p.stat().st_mtime))


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _base_results() -> dict[str, Any]:
    return {
        "nonstream": {
            "1000": {
                "direct": {"rps": 1000.0, "error_rate": 0.0, "saturated": False},
                "lumewarp": {
                    "rps": 1000.0,
                    "error_rate": 0.0,
                    "saturated": False,
                    "saturation_reasons": [],
                    "overhead_ms": {"p50": 0.2, "p99": 0.5},
                },
                "litellm": {
                    "rps": 1000.0,
                    "error_rate": 0.0,
                    "saturated": False,
                    "overhead_ms": {"p50": 5.0, "p99": 5.0},
                },
            }
        },
        "stream": {
            "100": {
                "lumewarp": {
                    "rps": 370.0,
                    "error_rate": 0.0,
                    "saturated": False,
                    "overhead_ms": {"ttft_p50": 0.2, "ttft_p99": 0.5},
                },
            }
        },
        "a6": {
            "1000": {
                "judged_at_1000_rps": True,
                "lumewarp_p99_overhead_ms": 0.5,
                "litellm_p99_overhead_ms": 5.0,
                "ratio_lumewarp_over_litellm": 0.1,
                "pass": True,
                "reasons": [],
            }
        },
    }


def _add_high_rate(results: dict[str, Any], rate: int = 5000) -> dict[str, Any]:
    results["nonstream"][str(rate)] = {
        "lumewarp": {
            "rps": 3000.0,
            "error_rate": 0.0,
            "saturated": True,
            "saturation_reasons": [f"achieved 3000 rps < 90% of {rate}"],
            "overhead_ms": {"p50": 1.0, "p99": 20.0},
        }
    }
    return results


def run_selftest() -> int:
    assertions = 0

    def check_case(
        label: str,
        results: dict[str, Any],
        *,
        outcome: str = "success",
        cpus: int = 8,
        expect_fail: bool,
        expect_message: str | None = None,
        expect_warning: str | None = None,
    ) -> None:
        nonlocal assertions
        failures, warnings, _lines = check(results, outcome, cpus)
        if expect_fail:
            assert failures, f"{label}: expected failure, got none"
        else:
            assert not failures, f"{label}: unexpected failures: {failures}"
        if expect_message is not None:
            assert any(expect_message in f for f in failures), (
                f"{label}: no failure containing {expect_message!r}: {failures}"
            )
        if expect_warning is not None:
            assert any(expect_warning in w for w in warnings), (
                f"{label}: no warning containing {expect_warning!r}: {warnings}"
            )
        assertions += 1

    check_case("healthy run", _base_results(), expect_fail=False)

    check_case(
        "run.sh failed",
        _base_results(),
        outcome="failure",
        expect_fail=True,
        expect_message="run.sh did not succeed",
    )

    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"]["error_rate"] = 0.02
    check_case(
        "lumewarp 2% errors",
        results,
        expect_fail=True,
        expect_message="error rate 2.00% > 1%",
    )

    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"]["saturated"] = True
    results["nonstream"]["1000"]["lumewarp"]["saturation_reasons"] = [
        "achieved 500 rps < 90% of 1000"
    ]
    check_case(
        "lumewarp saturated",
        results,
        expect_fail=True,
        expect_message="Lumewarp saturated",
    )

    results = _base_results()
    results["stream"]["100"]["lumewarp"]["error_rate"] = 0.02
    check_case(
        "stream lumewarp errors",
        results,
        expect_fail=True,
        expect_message="stream concurrency 100: Lumewarp error rate 2.00% > 1%",
    )

    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"]["overhead_ms"]["p99"] = 5.0
    results["a6"]["1000"]["lumewarp_p99_overhead_ms"] = 5.0
    results["a6"]["1000"]["litellm_p99_overhead_ms"] = 4.0
    check_case(
        "lumewarp p99 worse than litellm",
        results,
        expect_fail=True,
        expect_message="is worse than LiteLLM's",
    )

    results = _base_results()
    results["a6"]["1000"]["lumewarp_p99_overhead_ms"] = 2.0
    results["a6"]["1000"]["litellm_p99_overhead_ms"] = 50.0
    results["nonstream"]["1000"]["lumewarp"]["overhead_ms"]["p99"] = 2.0
    check_case(
        "absolute 1ms threshold only warns",
        results,
        expect_fail=False,
        expect_warning=">= 1 ms",
    )

    results = _base_results()
    results["a6"]["1000"]["lumewarp_p99_overhead_ms"] = 0.5
    results["a6"]["1000"]["litellm_p99_overhead_ms"] = 2.0
    check_case(
        "absolute 20% ratio only warns",
        results,
        expect_fail=False,
        expect_warning="ratio 0.250 > 0.2",
    )

    results = _base_results()
    del results["a6"]["1000"]
    check_case(
        "missing A6 row",
        results,
        expect_fail=True,
        expect_message="A6 row for 1000 RPS missing",
    )

    results = _base_results()
    results["a6"]["1000"]["lumewarp_p99_overhead_ms"] = None
    check_case(
        "missing A6 overhead value",
        results,
        expect_fail=True,
        expect_message="missing p99 overhead values",
    )

    results = _base_results()
    results["nonstream"] = {}
    results["stream"] = {}
    check_case(
        "no lumewarp scenarios",
        results,
        expect_fail=True,
        expect_message="no Lumewarp scenarios",
    )

    results = _base_results()
    results["a6"]["1000"]["reasons"] = ["direct saturated: achieved 700 rps < 90% of 1000"]
    check_case(
        "direct saturation warns only",
        results,
        expect_fail=False,
        expect_warning="direct saturated",
    )

    # R2 #8: a loose absolute ceiling, so a 1000x regression fails even when
    # LiteLLM is (always) saturated and its overhead is gigantic.
    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"]["overhead_ms"]["p99"] = 50.0
    results["a6"]["1000"]["lumewarp_p99_overhead_ms"] = 50.0
    results["a6"]["1000"]["litellm_p99_overhead_ms"] = 4000.0
    check_case(
        "lw=50ms vs ll=4000ms must fail",
        results,
        expect_fail=True,
        expect_message="absolute CI ceiling",
    )

    results = _base_results()
    results["stream"]["100"]["lumewarp"]["overhead_ms"]["ttft_p99"] = 11.0
    check_case(
        "stream TTFT overhead 11ms fails",
        results,
        expect_fail=True,
        expect_message="TTFT overhead p99 11.000 ms > 10 ms",
    )

    results = _base_results()
    results["stream"]["100"]["lumewarp"]["overhead_ms"]["ttft_p99"] = 10.0
    check_case("stream TTFT overhead 10ms passes", results, expect_fail=False)

    # Saturated scenarios are exempt from the absolute ceiling (saturation is
    # already the failure).
    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"].update(
        {
            "saturated": True,
            "saturation_reasons": ["achieved 500 rps < 90% of 1000"],
            "overhead_ms": {"p50": 1.0, "p99": 50.0},
        }
    )
    failures, _warnings, _lines = check(results, "success", 8)
    assert any("saturated" in f for f in failures), failures
    assert not any("absolute CI ceiling" in f for f in failures), failures
    assertions += 1

    # Small runners: >1000 RPS saturation is a warning (full dispatch), but
    # 1000 RPS remains a hard failure.
    check_case(
        "5000 RPS saturation deferred on 4 CPUs",
        _add_high_rate(_base_results()),
        cpus=4,
        expect_fail=False,
        expect_warning="warning only",
    )
    check_case(
        "5000 RPS saturation fails on 8 CPUs",
        _add_high_rate(_base_results()),
        cpus=8,
        expect_fail=True,
        expect_message="Lumewarp saturated",
    )
    results = _add_high_rate(_base_results())
    results["nonstream"]["5000"]["lumewarp"]["error_rate"] = 0.02
    check_case(
        "5000 RPS errors deferred on 4 CPUs",
        results,
        cpus=4,
        expect_fail=False,
        expect_warning="warning only",
    )
    results = _base_results()
    results["nonstream"]["1000"]["lumewarp"]["saturated"] = True
    results["nonstream"]["1000"]["lumewarp"]["saturation_reasons"] = ["achieved 500 rps"]
    check_case(
        "1000 RPS saturation still fails on 4 CPUs",
        results,
        cpus=4,
        expect_fail=True,
        expect_message="Lumewarp saturated",
    )

    # A6 may use a non-"1000" key when judged_at_1000_rps is present.
    results = copy.deepcopy(_base_results())
    results["a6"] = {
        "1k": {
            "judged_at_1000_rps": True,
            "lumewarp_p99_overhead_ms": 0.5,
            "litellm_p99_overhead_ms": 5.0,
            "reasons": [],
        }
    }
    check_case("A6 row found by marker", results, expect_fail=False)

    print(f"selftest: OK ({assertions} assertions)")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        default="bench/results",
        help="a run directory (contains results.json) or the results root (newest run)",
    )
    parser.add_argument("--results-json", help="explicit results.json (overrides --results-dir)")
    parser.add_argument(
        "--run-outcome",
        default="success",
        help="outcome of the run.sh step (GitHub steps.<id>.outcome); default success",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=None,
        help="CPU count for the small-runner deferral (default: detected)",
    )
    parser.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.results_json:
        results_path = Path(args.results_json)
    else:
        results_path = resolve_results(Path(args.results_dir))
    if results_path is None or not results_path.is_file():
        print(f"::error::no results.json found under {args.results_dir}")
        print("ci_check: FAIL (missing results; did run.sh fail before aggregating?)")
        return 1

    try:
        with results_path.open(encoding="utf-8") as fh:
            results = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::cannot read {results_path}: {exc}")
        print("ci_check: FAIL (unreadable results.json)")
        return 1
    if not isinstance(results, dict):
        print(f"::error::{results_path} does not contain a JSON object")
        print("ci_check: FAIL (invalid results.json)")
        return 1

    failures, warnings, lines = check(results, args.run_outcome, args.cpus)
    return emit(results_path, failures, warnings, lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

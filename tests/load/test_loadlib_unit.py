"""Analysis helpers without processes, binaries, or live database connections."""

import json
import uuid
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

import loadlib
import pytest
from loadlib import (
    LoadRun,
    accounting,
    cost,
    cost_check,
    hard_errors,
    load_window,
    pct,
    summarize,
)


def record(status=200, *, key="key", stream=False, **extra):
    return {"k": key, "st": status, "s": stream, "t": 1.0, "d": 0.01, **extra}


@pytest.mark.parametrize(
    ("model", "prompt", "completion", "expected"),
    [("mock-a", 10, 3, 16000), ("mock-b", 10, 3, 42000), ("mock-a", 0, 0, 0)],
)
def test_unit_cost(model, prompt, completion, expected):
    assert cost(model, prompt, completion) == expected


def test_unit_cost_rounds_half_up(monkeypatch):
    monkeypatch.setitem(loadlib.PRICES, "fractional", (499999, 1))
    assert cost("fractional", 1, 0) == 0
    assert cost("fractional", 1, 1) == 1


@pytest.mark.parametrize(
    ("values", "percentile", "expected"),
    [
        ([], 50, None),
        ([3], 99, 3),
        ([4, 1, 3, 2], 50, 2),
        ([4, 1, 3, 2], 90, 4),
        ([2, 1], -10, 1),
        ([2, 1], 110, 2),
    ],
)
def test_unit_pct(values, percentile, expected):
    original = list(values)
    assert pct(values, percentile) == expected
    assert values == original


def test_unit_summarize():
    records = [
        record(t=1, d=0.01),
        record(t=3, d=0.03, stream=True),
        record(probe=True, d=9),
        record(429),
        record(0),
    ]
    summary = summarize(records, 10)
    assert summary["requests"] == 5
    assert summary["statuses"] == {"0": 1, "200": 3, "429": 1}
    assert summary["rps_ok"] == 1
    assert summary["latency_ms"] == {"n": 1, "p50": 10, "p90": 10, "p99": 10, "max": 10}
    assert summary["stream_latency_ms"]["p99"] == 30


def test_unit_summarize_empty_and_single():
    assert summarize([], 10)["latency_ms"] == {"n": 0}
    assert summarize([], 10)["rps_ok"] == 0
    assert summarize([record()], 10)["rps_ok"] == 0.1
    assert summarize([record()], 0)["rps_ok"] is None


def test_unit_hard_errors():
    allowed = [record(st) for st in (200, 401, 403, 429)]
    bad = [record(st) for st in (0, 201, 400, 404, 500, 503)]
    malformed = record(p="bad body")
    assert hard_errors(allowed + bad + [malformed]) == bad + [malformed]
    # Inclusive restart boundaries forgive only connection errors, never bad bodies/5xx.
    records = [record(0, t=t) for t in (0, 1, 2, 3)]
    assert hard_errors(records + [malformed, bad[-1]], (1, 2)) == [
        records[0],
        records[3],
        malformed,
        bad[-1],
    ]


@pytest.mark.parametrize(
    ("elapsed", "warning_offset", "duration", "stream_duration", "status", "expected"),
    [
        (5, None, 0.01, 0.02, 200, True),
        (9, None, 0.01, 0.02, 200, False),
        (9, 4, 0.01, 0.02, 200, True),
        (9, 4, 0.5, 0.02, 200, False),
        (9, 4, 0.1, 0.02, 200, False),
        (9, 4, 0.01, 0.5, 200, False),
        (9, 4, 0.01, 0.25, 200, False),
        (9, 4, 0.01, 0.02, 500, False),
        (9, 1, 0.01, 0.02, 200, False),
        (9, 9.5, 0.01, 0.02, 200, False),
        (10.01, 4, 0.01, 0.02, 200, False),
        (10, 4, 0.01, 0.02, 200, True),
        (8, 4, 0.099, 0.249, 200, True),
        (8, None, 0.01, 0.02, 200, False),
    ],
)
def test_unit_load_window(
    monkeypatch, elapsed, warning_offset, duration, stream_duration, status, expected
):
    monkeypatch.setattr(loadlib, "SAT_P99_S", 0.1)
    monkeypatch.setattr(loadlib, "SAT_STREAM_P99_S", 0.25)
    started = datetime(2026, 10, 24, 12).timestamp()
    log = ""
    if warning_offset is not None:
        stamp = datetime.fromtimestamp(started + warning_offset).strftime("%Y-%m-%d %H:%M:%S,%f")[
            :-3
        ]
        log = f"[{stamp}] host/WARNING/locust.runners: CPU usage above 90%!"
    run = SimpleNamespace(started=started, seconds=10, proc=SimpleNamespace(text=lambda: log))
    records = [
        record(status, t=started + 3, d=duration),
        record(status, t=started + 4, d=stream_duration, stream=True),
    ]
    result = load_window(run, elapsed, 2, records, step_started=started + 2)
    assert result["ok"] is expected
    assert result["diagnostic"] == ("generator saturation" if expected and elapsed >= 8 else None)
    assert result["late"] is (elapsed >= 8)
    assert result["generator_cpu_warning"] is (
        warning_offset is not None and 2 <= warning_offset <= elapsed
    )
    before_step = warning_offset is not None and warning_offset < 2
    assert result["generator_cpu_warning_before_step"] is before_step
    assert result["generator_cpu_warning_before_step_at"] == (
        started + warning_offset if before_step else None
    )


@pytest.mark.parametrize(
    ("offsets", "in_step"), [([1], False), ([4, 1, 0.5], True), ([0.5, 1], False)]
)
def test_unit_load_window_earlier_warning_is_diagnostic(monkeypatch, offsets, in_step):
    monkeypatch.setattr(loadlib, "SAT_P99_S", 0.1)
    monkeypatch.setattr(loadlib, "SAT_STREAM_P99_S", 0.25)
    started = datetime(2026, 10, 24, 12).timestamp()
    lines = []
    for offset in offsets:
        stamp = datetime.fromtimestamp(started + offset).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        lines.append(f"[{stamp}] host/WARNING/locust.runners: CPU usage above 90%!")
    run = SimpleNamespace(
        started=started, seconds=10, proc=SimpleNamespace(text=lambda: "\n".join(lines))
    )
    records = [record(t=started + 3), record(t=started + 4, stream=True)]
    result = load_window(run, 9, 2, records, step_started=started + 2)
    assert result["generator_cpu_warning_before_step"] is True
    assert result["generator_cpu_warning_before_step_at"] == started + 1
    assert result["generator_cpu_warning"] is in_step
    assert result["ok"] is in_step
    assert result["diagnostic"] == ("generator saturation" if in_step else None)


@pytest.mark.parametrize(
    "warning",
    [
        "CPU usage above 90%!",
        "[invalid timestamp] host/WARNING/locust.runners: CPU usage above 90%!",
        "[2026-10-24 12:00:04,000] host/INFO/locust.runners: CPU usage above 90%!",
    ],
)
def test_unit_load_window_requires_timestamped_warning(warning):
    started = datetime(2026, 10, 24, 12).timestamp()
    run = SimpleNamespace(started=started, seconds=10, proc=SimpleNamespace(text=lambda: warning))
    records = [record(t=started + 3), record(t=started + 4, stream=True)]
    result = load_window(run, 9, 2, records, step_started=started + 2)
    assert result["ok"] is False
    assert result["generator_cpu_warning"] is False


@pytest.mark.parametrize("stream", [False, True])
def test_unit_load_window_requires_both_request_types(stream):
    started = datetime(2026, 10, 24, 12).timestamp()
    warning = "[2026-10-24 12:00:04,000] host/WARNING/locust.runners: CPU usage above 90%!"
    run = SimpleNamespace(started=started, seconds=10, proc=SimpleNamespace(text=lambda: warning))
    result = load_window(
        run, 9, 2, [record(t=started + 3, stream=stream)], step_started=started + 2
    )
    assert result["ok"] is False


def test_unit_load_window_uses_step_latency(monkeypatch):
    monkeypatch.setattr(loadlib, "SAT_P99_S", 0.1)
    monkeypatch.setattr(loadlib, "SAT_STREAM_P99_S", 0.25)
    started = datetime(2026, 10, 24, 12).timestamp()
    warning = "[2026-10-24 12:00:04,000] host/WARNING/locust.runners: CPU usage above 90%!"
    run = SimpleNamespace(started=started, seconds=10, proc=SimpleNamespace(text=lambda: warning))
    records = [record(t=started + 1)] * 1000 + [
        record(t=started + 3, d=0.5),
        record(t=started + 4, stream=True),
    ]
    result = load_window(run, 9, 2, records, step_started=started + 2)
    assert result["gateway_p99_s"] == 0.5
    assert result["ok"] is False


def test_unit_accounting(monkeypatch):
    monkeypatch.setattr(
        loadlib,
        "usage_counts",
        lambda _: Counter(
            {
                ("key", 200, False): 1,
                ("key", 200, True): 1,
                ("key", 429, False): 2,
                ("key", 403, False): 1,
            }
        ),
    )
    monkeypatch.setattr(loadlib, "pg", lambda *_: [{"n": 0}])
    records = [
        record(),
        record(stream=True),
        record(429),
        record(429, stream=True),
        record(401),
        record(403),
        record(0),
    ]
    result = accounting(SimpleNamespace(dsn="unused"), records)
    assert result == {
        "client_200": 2,
        "db_200": 2,
        "client_429": 2,
        "db_429": 2,
        "db_rows": 5,
        "duplicate_request_ids": 0,
        "mismatches": [],
    }


def test_unit_accounting_detects_missing_extra_and_duplicate(monkeypatch):
    monkeypatch.setattr(
        loadlib,
        "usage_counts",
        lambda _: Counter(
            {
                ("key", 200, True): 1,
                ("key", 401, False): 2,
                ("extra", 429, False): 1,
            }
        ),
    )
    monkeypatch.setattr(loadlib, "pg", lambda *_: [{"n": 1}])
    result = accounting(SimpleNamespace(dsn="unused"), [record(), record(401)])
    assert result["duplicate_request_ids"] == 1
    assert {
        (r["key"], r["status"], r["stream"], r["client"], r["db"]) for r in result["mismatches"]
    } == {
        ("key", 200, False, 1, 0),
        ("key", 200, True, 0, 1),
        ("key", 401, None, 1, 2),
        ("extra", 429, None, 0, 1),
    }


@pytest.mark.parametrize("broken", [False, True])
def test_unit_cost_check(monkeypatch, broken):
    rows = [
        {
            "team": "00000000-0000-0000-0000-000000000001",
            "owner_kind": "user",
            "owner": "user-1",
            "model": "mock-a",
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost_nano_usd": 16000,
            "cost_known": not broken,
        },
        {
            "team": "00000000-0000-0000-0000-000000000001",
            "owner_kind": "user",
            "owner": "user-1",
            "model": "mock-b",
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost_nano_usd": 42001 if broken else 42000,
            "cost_known": True,
        },
    ]
    team = rows[0]["team"]
    member = str(uuid.uuid5(uuid.UUID(team), "user-1"))
    totals = [
        {"scope_type": "team", "id": team, "c": 58000},
        {"scope_type": "member", "id": member, "c": 58000},
    ]
    if broken:
        rows.append(
            {
                "team": "team-2",
                "owner_kind": "service_account",
                "owner": "sa-2",
                "model": "mock-a",
                "prompt_tokens": 0,
                "completion_tokens": 1,
                "cost_nano_usd": 2000,
                "cost_known": True,
            }
        )
        totals.append({"scope_type": "service_account", "id": "orphan", "c": 1})
    answers = iter([rows, totals])
    monkeypatch.setattr(loadlib, "pg", lambda *_: next(answers))
    result = cost_check(SimpleNamespace(dsn="unused"))
    assert result["rows_with_wrong_cost"] == (3 if broken else 0)
    assert result["total_nano_usd"] == (60000 if broken else 58000)
    assert {(r["scope_type"], r["scope_id"]) for r in result["scopes_mismatched"]} == (
        {
            ("team", team),
            ("member", member),
            ("team", "team-2"),
            ("service_account", "sa-2"),
            ("service_account", "orphan"),
        }
        if broken
        else set()
    )


def test_unit_cost_check_empty(monkeypatch):
    monkeypatch.setattr(loadlib, "pg", lambda *_: [])
    assert cost_check(SimpleNamespace(dsn="unused")) == {
        "scopes": 0,
        "rows_with_wrong_cost": 0,
        "scopes_mismatched": [],
        "total_nano_usd": 0,
    }


def test_unit_cost_check_member_scopes_are_team_specific(monkeypatch):
    user = str(uuid.UUID(int=3))
    teams = [str(uuid.UUID(int=i)) for i in (1, 2)]
    rows = [
        {
            "team": team,
            "owner_kind": "user",
            "owner": user,
            "model": "mock-a",
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost_nano_usd": 16000,
            "cost_known": True,
        }
        for team in teams
    ]
    totals = [
        {"scope_type": kind, "id": scope_id, "c": 16000}
        for team in teams
        for kind, scope_id in (
            ("team", team),
            ("member", str(uuid.uuid5(uuid.UUID(team), user))),
        )
    ]
    answers = iter([rows, totals])
    monkeypatch.setattr(loadlib, "pg", lambda *_: next(answers))
    result = cost_check(SimpleNamespace(dsn="unused"))
    assert result["scopes"] == 4
    assert result["total_nano_usd"] == 32000
    assert result["rows_with_wrong_cost"] == 0
    assert result["scopes_mismatched"] == []


@pytest.mark.parametrize("count", [1, 2])
def test_unit_load_run_checks_final_count_not_periodic_csv(tmp_path, monkeypatch, count):
    monkeypatch.setattr(loadlib, "RESULTS", tmp_path / "results")
    run_dir = tmp_path / "locust-steady"
    run_dir.mkdir()
    (run_dir / "requests-1.jsonl").write_text(json.dumps(record()) + "\n")
    (run_dir / "stats_stats.csv").write_text("Name,Request Count\nAggregated,0\n")
    (run_dir / "final-stats.json").write_text(json.dumps({"requests": count}))
    log = tmp_path / "locust.log"
    log.write_text("")
    run = LoadRun.__new__(LoadRun)
    run.dir = run_dir
    run.seconds = 1
    run.proc = SimpleNamespace(
        popen=SimpleNamespace(wait=lambda _: None, returncode=0),
        log=log,
        text=lambda: "",
    )
    if count == 1:
        assert run.wait() == [record()]
    else:
        with pytest.raises(AssertionError, match="locust counted 2 requests, records have 1"):
            run.wait()


def test_load_window_reads_wall_clock_log_lines_through_the_run_offset():
    wall = datetime(2026, 10, 24, 12).timestamp()
    offset = wall - 1000.0
    stamp = datetime.fromtimestamp(wall + 3).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    warning = f"[{stamp}] host/WARNING/root: CPU usage above 90%! Load generator"
    run = SimpleNamespace(
        started=1000.0, seconds=10, wall_offset=offset, proc=SimpleNamespace(text=lambda: warning)
    )
    records = [record(t=1003.0), record(t=1004.0, stream=True)]
    result = load_window(run, 9, 2, records, step_started=1002.0)
    assert result["generator_cpu_warning"] is True
    assert result["ok"] is True


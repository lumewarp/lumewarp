"""Analysis helpers without processes, binaries, or live database connections."""

import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import loadlib
import pytest
from loadlib import (
    ClockSampler,
    ClockStepped,
    LoadRun,
    accounting,
    budget_flips,
    cost,
    cost_check,
    hard_errors,
    load_window,
    pct,
    retry_unmeasurable,
    summarize,
)
from loadpacing import CpuWarningRecorder, Pacer


def record(status=200, *, key="key", stream=False, **extra):
    return {"k": key, "st": status, "s": stream, "t": 1.0, "d": 0.01, **extra}


def load_run(started, warnings):
    return SimpleNamespace(started=started, seconds=10, cpu_warnings=lambda: warnings)


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
    started = 1000.0
    warnings = [] if warning_offset is None else [started + warning_offset]
    run = load_run(started, warnings)
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
    started = 1000.0
    run = load_run(started, sorted(started + offset for offset in offsets))
    records = [record(t=started + 3), record(t=started + 4, stream=True)]
    result = load_window(run, 9, 2, records, step_started=started + 2)
    assert result["generator_cpu_warning_before_step"] is True
    assert result["generator_cpu_warning_before_step_at"] == started + 1
    assert result["generator_cpu_warning"] is in_step
    assert result["ok"] is in_step
    assert result["diagnostic"] == ("generator saturation" if in_step else None)


def test_unit_load_window_needs_a_recorded_warning_not_a_log_line():
    started = 1000.0
    run = load_run(started, [])
    stamp = datetime.fromtimestamp(started + 4).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    run.proc = SimpleNamespace(
        text=lambda: f"[{stamp}] host/WARNING/locust.runners: CPU usage above 90%!"
    )
    records = [record(t=started + 3), record(t=started + 4, stream=True)]
    result = load_window(run, 9, 2, records, step_started=started + 2)
    assert result["ok"] is False
    assert result["generator_cpu_warning"] is False


@pytest.mark.parametrize("stream", [False, True])
def test_unit_load_window_requires_both_request_types(stream):
    started = 1000.0
    run = load_run(started, [started + 4])
    result = load_window(
        run, 9, 2, [record(t=started + 3, stream=stream)], step_started=started + 2
    )
    assert result["ok"] is False


def test_unit_load_window_uses_step_latency(monkeypatch):
    monkeypatch.setattr(loadlib, "SAT_P99_S", 0.1)
    monkeypatch.setattr(loadlib, "SAT_STREAM_P99_S", 0.25)
    started = 1000.0
    run = load_run(started, [started + 4])
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


class FakeClocks:
    def __init__(self, mono=1000.0, wall=5000.0):
        self.now = mono
        self.wall_now = wall

    def mono(self):
        return self.now

    def wall(self):
        return self.wall_now

    def advance(self, seconds):
        self.now += seconds
        self.wall_now += seconds

    def step(self, seconds):
        self.wall_now += seconds


def recorder_warns(tmp_path, clock, message="CPU usage above 90%! This may constrain"):
    logger = logging.Logger("locust.runners")
    logger.addHandler(CpuWarningRecorder(str(tmp_path), clock))
    logger.warning(message)


def run_reading(tmp_path):
    run = LoadRun.__new__(LoadRun)
    run.dir = tmp_path
    run.seconds = 10
    run.started = 1000.0
    run.proc = SimpleNamespace(text=lambda: "")
    return run


@pytest.mark.parametrize("step", [24.9, -24.9])
def test_unit_warning_before_the_step_stays_there_when_the_wall_clock_moves(tmp_path, step):
    """The warning came 20 s before the step. A wall clock read after a +24.9 s step would put it
    4.9 s inside; here the wall clock is not consulted, so neither direction moves it."""
    clocks = FakeClocks()
    clocks.advance(5)
    recorder_warns(tmp_path, clocks.mono)
    clocks.step(step)
    run = run_reading(tmp_path)
    records = [record(t=1026.0), record(t=1027.0, stream=True)]
    result = load_window(run, 9, 2, records, step_started=1025.0)
    assert result["generator_cpu_warning"] is False
    assert result["generator_cpu_warning_before_step_at"] == 1005.0
    assert result["ok"] is False


@pytest.mark.parametrize("step", [24.9, -24.9])
def test_unit_warning_inside_the_step_counts_when_the_wall_clock_moves(tmp_path, step):
    clocks = FakeClocks()
    clocks.step(step)
    clocks.advance(4)
    recorder_warns(tmp_path, clocks.mono)
    run = run_reading(tmp_path)
    records = [record(t=1003.0), record(t=1004.0, stream=True)]
    result = load_window(run, 9, 2, records, step_started=1002.0)
    assert result["generator_cpu_warning"] is True
    assert result["ok"] is True


def test_unit_recorder_takes_the_monotonic_clock_not_the_record_time(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1.0)
    recorder_warns(tmp_path, lambda: 123.5)
    assert run_reading(tmp_path).cpu_warnings() == [123.5]


def test_unit_recorder_ignores_other_warnings(tmp_path):
    recorder_warns(tmp_path, lambda: 1.0, "Worker 1 exceeded cpu threshold")
    assert run_reading(tmp_path).cpu_warnings() == []


class TestPacer:
    def pacer(self):
        self.clocks = FakeClocks()
        return Pacer(0.1, self.clocks.mono)

    @pytest.mark.parametrize(
        ("first_task", "wait"), [(0.01, 0.09), (0.15, 0.0), (0.1, 0.0), (0.0, 0.1)]
    )
    def test_first_task_counts_from_construction(self, first_task, wait):
        pacer = self.pacer()
        self.clocks.advance(first_task)
        assert pacer.next() == pytest.approx(wait)

    def test_normal_task_after_a_wait(self):
        pacer = self.pacer()
        self.clocks.advance(0.01)
        self.clocks.advance(pacer.next())
        self.clocks.advance(0.03)
        assert pacer.next() == pytest.approx(0.07)

    def test_overrun_never_waits_negative(self):
        pacer = self.pacer()
        self.clocks.advance(0.01)
        self.clocks.advance(pacer.next())
        self.clocks.advance(0.25)
        assert pacer.next() == 0.0
        self.clocks.advance(0.02)
        assert pacer.next() == pytest.approx(0.08)

    @pytest.mark.parametrize("step", [24.9, -24.9])
    def test_wall_clock_step_changes_nothing(self, step):
        pacer = self.pacer()
        self.clocks.advance(0.01)
        self.clocks.advance(pacer.next())
        self.clocks.step(step)
        self.clocks.advance(0.02)
        assert pacer.next() == pytest.approx(0.08)


def sampled(clocks, steps=(), seconds=10, every=0.5):
    """A sampler that sampled every `every` s for `seconds`, stepping the wall clock as told."""
    sampler = ClockSampler(mono=clocks.mono, wall=clocks.wall)
    steps = dict(steps)
    for i in range(int(seconds / every) + 1):
        if i * every in steps:
            clocks.step(steps[i * every])
        sampler.sample()
        clocks.advance(every)
    return sampler


def test_unit_clock_sampler_converts_on_a_stable_clock():
    sampler = sampled(FakeClocks())  # offset 4000
    assert sampler.to_monotonic(5003.25, 1001.0, 1009.0) == 1003.25


@pytest.mark.parametrize("step", [24.9, -24.9, 0.06, -0.06])
def test_unit_clock_sampler_refuses_a_step_inside_the_window(step):
    sampler = sampled(FakeClocks(), [(4.0, step)])
    with pytest.raises(ClockStepped, match="moved by"):
        sampler.to_monotonic(5003.25, 1001.0, 1009.0)


def test_unit_clock_sampler_refuses_a_step_and_its_return_inside_the_window():
    sampler = sampled(FakeClocks(), [(3.0, 24.9), (6.0, -24.9)])
    with pytest.raises(ClockStepped, match="moved by"):
        sampler.to_monotonic(5003.25, 1001.0, 1009.0)


def test_unit_clock_sampler_allows_a_step_outside_the_window():
    sampler = sampled(FakeClocks(), [(9.5, 24.9)])
    assert sampler.to_monotonic(5003.25, 1001.0, 1009.0) == 1003.25


def test_unit_clock_sampler_tolerates_slew():
    sampler = sampled(FakeClocks(), [(4.0, 0.04)])
    assert sampler.to_monotonic(5003.25, 1001.0, 1009.0) == 1003.25


@pytest.mark.parametrize(("since", "until"), [(999.0, 1009.0), (1001.0, 1011.0)])
def test_unit_clock_sampler_refuses_a_window_it_did_not_cover(since, until):
    sampler = sampled(FakeClocks())
    with pytest.raises(ClockStepped, match="no clock samples"):
        sampler.to_monotonic(5003.25, since, until)


def test_unit_clock_sampler_refuses_a_gap_a_step_could_hide_in():
    clocks = FakeClocks()
    sampler = ClockSampler(mono=clocks.mono, wall=clocks.wall, max_gap=1.0)
    sampler.sample()
    clocks.advance(1.5)
    sampler.sample()
    with pytest.raises(ClockStepped, match="no clock sample for"):
        sampler.to_monotonic(5000.5, 1000.0, 1001.5)


def test_unit_clock_sampler_drops_a_preempted_pair():
    readings = iter([10.0, 10.5, 20.0, 20.001])
    sampler = ClockSampler(mono=lambda: next(readings), wall=lambda: 100.0)
    sampler.sample()
    sampler.sample()
    assert sampler.samples == [(20.001, 100.0 - 20.001)]


def exceeded_datetime(wall):
    return datetime.fromtimestamp(wall, UTC)


def test_unit_budget_flip_counts_from_exceeded_at_not_from_commit():
    """exceeded_at at X, committed (so visible) at X+3, first 429 at X+5.01: 5.01 s, over the
    bound. Measuring from when the row became visible would read 2.01 s."""
    sampler = sampled(FakeClocks(), seconds=10)
    x = 1002.0
    records = [record(429, t=x + 5.01), record(200, t=x + 1)]
    exceeded, flips = budget_flips(
        records, {"key"}, exceeded_datetime(x + 4000), sampler, since=1000.0
    )
    assert exceeded == pytest.approx(x)
    assert flips["key"] == pytest.approx(5.01)
    assert flips["key"] >= 5.0


def test_unit_budget_flip_is_void_when_the_clock_stepped():
    sampler = sampled(FakeClocks(), [(4.0, 24.9)])
    records = [record(429, t=1005.0)]
    with pytest.raises(ClockStepped):
        budget_flips(records, {"key"}, exceeded_datetime(5002.0), sampler, since=1000.0)


def test_unit_budget_flip_checks_stay_failures():
    sampler = sampled(FakeClocks())
    with pytest.raises(AssertionError, match="never got 429"):
        budget_flips([record(200, t=1001.0)], {"key"}, exceeded_datetime(5001.0), sampler, 1000.0)
    records = [record(429, t=1002.0), record(200, t=1003.0)]
    with pytest.raises(AssertionError, match="accepted after it flipped"):
        budget_flips(records, {"key"}, exceeded_datetime(5001.0), sampler, 1000.0)


def test_unit_retry_stops_at_the_first_measured_attempt():
    seen = []

    def scenario(attempt):
        seen.append(attempt)
        if attempt == 1:
            raise ClockStepped("moved")

    retry_unmeasurable(scenario, "budget")
    assert seen == [1, 2]


def test_unit_retry_never_passes_an_unmeasured_scenario():
    seen = []

    def scenario(attempt):
        seen.append(attempt)
        raise ClockStepped("moved")

    with pytest.raises(AssertionError, match="wall clock stepped in each of 3 attempts"):
        retry_unmeasurable(scenario, "budget")
    assert seen == [1, 2, 3]


def test_unit_retry_does_not_retry_a_real_failure():
    seen = []

    def scenario(attempt):
        seen.append(attempt)
        raise AssertionError("flip over the bound")

    with pytest.raises(AssertionError, match="flip over the bound"):
        retry_unmeasurable(scenario, "budget")
    assert seen == [1]

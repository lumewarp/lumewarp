"""Control-plane-mode e2e under load: Postgres + control plane + gateway + mock upstream as
real processes (tests/e2e/stack.py), driven by headless Locust while the orchestrator
churns config, restarts processes and checks every request against Postgres.

Each scenario runs on a fresh stack so its usage accounting is exact. Every request a
client made (Locust or the orchestrator's probes) is recorded; usage rows must match them
per key, status and stream flag.
"""

import time
from collections import Counter, defaultdict

import pytest
from loadlib import (
    Fleet,
    LoadRun,
    Probe,
    Records,
    accounting,
    cost_check,
    dist,
    env_float,
    env_int,
    gateway_counter,
    hard_errors,
    load_window,
    settle_usage,
    stream_resets,
    summarize,
    wait_acked,
    write_report,
)
from stack import SCOPE, pg, wait_for

STEADY_SECS = env_int("LOAD_STEADY_SECS", 150)
CHURN_SECS = env_int("LOAD_CHURN_SECS", 110)
# Long enough for the outage (15 s in + CP_OUTAGE_SECS) plus USAGE_DRAIN_BOUND.
CP_SECS = env_int("LOAD_CP_SECS", 90)
CP_OUTAGE_SECS = env_float("LOAD_CP_OUTAGE_SECS", 20)
BUDGET_SECS = env_int("LOAD_BUDGET_SECS", 50)
GW_SECS = env_int("LOAD_GW_SECS", 50)
# Config versions published back to back (a PATCH every BURST_GAP seconds).
BURST = env_int("LOAD_BURST", 40)
BURST_GAP = env_float("LOAD_BURST_GAP", 0.05)
REVOKE_BOUND = env_float("LOAD_REVOKE_BOUND", 2.0)
NEW_KEY_BOUND = env_float("LOAD_NEW_KEY_BOUND", 2.0)
BUDGET_BOUND = env_float("LOAD_BUDGET_BOUND", 5.0)
# The gateway's usage retry backoff caps at 30 s (gateway/.../usage.rs RETRY_MAX), so the
# first report after the CP is back can come up to 30 s later; then the backlog drains.
USAGE_DRAIN_BOUND = env_float("LOAD_USAGE_DRAIN_BOUND", 30 + 20)
# ~16_000 nano-USD per mock request at mock-a prices.
BUDGET = env_int("LOAD_BUDGET_NANO_USD", 10_000_000)
DROPPED = "lumewarp_usage_events_dropped_total"


def ok200(status, _code) -> bool:
    return status == 200


def rejected401(status, code) -> bool:
    return status == 401 and code == "invalid_api_key"


def finish(stack, run: LoadRun, extra: Records | None = None) -> list[dict]:
    records = run.wait()
    if extra is not None:
        records += extra.items
    settle_usage(stack, sum(1 for r in records if r["st"] == 200))
    return records


def assert_accounting(report: dict) -> None:
    acc = report["usage"]
    assert acc["duplicate_request_ids"] == 0, acc
    assert not acc["mismatches"], f"usage rows != client requests: {acc['mismatches'][:20]}"
    costs = report["cost"]
    assert costs["rows_with_wrong_cost"] == 0, costs
    assert not costs["scopes_mismatched"], costs


def test_steady_mixed_load(stack):
    fleet = Fleet(stack)
    gw = stack.start_gateway()
    wait_acked(stack)
    run = LoadRun(stack, gw, "steady", fleet.keys, STEADY_SECS)
    run.start()
    records = finish(stack, run)

    report = {
        "load": summarize(records, STEADY_SECS),
        "keys": len(fleet.keys),
        "teams": len(fleet.teams),
        "owners": len(fleet.owners),
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": gateway_counter(gw, DROPPED),
        "config_stream_resets": stream_resets(gw),
    }
    write_report("1-steady", report)
    assert len(fleet.keys) >= 50
    assert hard_errors(records) == []
    assert set(report["load"]["statuses"]) == {"200"}, report["load"]["statuses"]
    assert report["load"]["stream_latency_ms"]["n"] > 0
    assert_accounting(report)
    assert report["usage_dropped"] == 0
    assert report["config_stream_resets"] == 0, "config stream dropped under steady CP"


def test_churn_under_load(stack):
    fleet = Fleet(stack)
    gw = stack.start_gateway()
    wait_acked(stack)
    probes = Records()
    probe = Probe(gw, probes)
    rl_index = 1
    rl_keys = fleet.owner_keys(rl_index)
    rl_ids = {k["id"] for k in rl_keys}
    # One key per owner (except the rate-limited one) is revoked under load.
    revoked = [fleet.owner_keys(i)[0] for i in range(len(fleet.owners)) if i != rl_index][:6]
    burst_key = fleet.keys[-1]  # PATCHed many times; kept out of the pool
    pool = [k for k in fleet.keys if k is not burst_key]
    run = LoadRun(stack, gw, "churn", pool, CHURN_SECS)
    run.start()
    report: dict = {}
    timeline: dict = {}
    try:
        run.sleep_until(8)
        step_started = time.time()
        # --- revoke keys under load: measured per key from the API call to the first 401.
        revoke_latency = []
        for key in revoked:
            t0 = time.time()
            stack.ok("POST", f"/keys/{key['id']}/revoke")
            seen = probe.until(key, rejected401, 5)
            key["rejected_at"] = seen
            revoke_latency.append(None if seen is None else seen - t0)
            time.sleep(0.5)

        # --- create keys under load: accepted within a bound, then added to the pool.
        new_latency = []
        others = [i for i in range(len(fleet.owners)) if i != rl_index]
        for i in range(6):
            owner_id = fleet.owners[others[i % len(others)]]["id"]
            t0 = time.time()
            key = fleet.create_key(owner_id)
            fleet.keys.append(key)
            seen = probe.until(key, ok200, 5)
            new_latency.append(None if seen is None else seen - t0)
            pool.append(key)
            run.set_keys(pool)

        # --- models and weights: a+b (1:1) -> b only (a weight 0) -> a only.
        a = fleet.models["mock-a"]
        timeline["before_b"] = stack.current_version()
        b = fleet.create_model("mock-b")
        stack.ok("PATCH", f"/routers/{fleet.router_id}", json={"model_ids": [a, b]})
        timeline["ab"] = wait_acked(stack)
        time.sleep(6)
        timeline["ab_end"] = stack.current_version()
        stack.ok("PATCH", f"/models/{a}", json={"weight": 0})
        timeline["b_only"] = wait_acked(stack)
        time.sleep(6)
        timeline["b_only_end"] = stack.current_version()
        stack.ok("PATCH", f"/models/{a}", json={"weight": 1})
        stack.ok("PATCH", f"/routers/{fleet.router_id}", json={"model_ids": [a]})
        r = stack.api("DELETE", f"/models/{b}")
        assert r.status_code == 204, r.text
        timeline["a_only"] = wait_acked(stack)
        time.sleep(4)

        # --- lower an owner rate limit: 429 + Retry-After only for its keys, never 5xx.
        rl_owner = fleet.owners[rl_index]["policy_path"]
        t_rl_api = time.time()
        stack.ok("PATCH", rl_owner, json={"rpm_limit": 60})
        wait_acked(stack)
        t_rl = time.time()
        time.sleep(8)
        t_rl_off_api = time.time()
        stack.ok("PATCH", rl_owner, json={"rpm_limit": None})
        wait_acked(stack)
        t_rl_off = time.time()
        assert probe.until(rl_keys[0], ok200, 3) is not None, "rate limit cleared but still 429"

        # --- many versions quickly.
        burst_start_version = stack.current_version()
        t0 = time.time()
        for i in range(BURST):
            stack.ok(
                "PATCH",
                next(o["policy_path"] for o in fleet.owners if o["id"] == burst_key["owner_id"]),
                json={"rpm_limit": 100_000 + i},
            )
            time.sleep(BURST_GAP)
        api_secs = time.time() - t0
        latest = wait_acked(stack, 30)
        converge_secs = time.time() - t0
        wait_for(lambda: gw.metric("lumewarp_config_version") == latest, 5, "gateway on the latest")
        burst = {
            "patches": BURST,
            "gap_s": BURST_GAP,
            "api_secs": round(api_secs, 2),
            "versions_published": latest - burst_start_version,
            "acked_latest_after_secs": round(converge_secs, 2),
        }
        assert burst["versions_published"] >= BURST // 4, f"burst was coalesced: {burst}"
        step_elapsed = run.elapsed()
    except BaseException:
        run.proc.kill()
        raise
    finally:
        probe.close()

    records = finish(stack, run, probes)
    versions = pg(
        stack.dsn,
        "SELECT version, source_revision, created_at FROM config_versions WHERE scope = $1"
        " ORDER BY version",
        SCOPE,
    )
    status = stack.gateway_status()

    # Traffic shift, by the config version each request was served under.
    by_version = pg(
        stack.dsn,
        "SELECT config_version AS v, model AS d, count(*) AS n FROM usage_events"
        " WHERE status = 200 GROUP BY 1, 2",
    )

    def share(lo: int, hi: int | None) -> Counter:
        c = Counter()
        for row in by_version:
            if row["v"] >= lo and (hi is None or row["v"] <= hi):
                c[row["d"]] += row["n"]
        return c

    phases = {
        "a_before": share(0, timeline["before_b"]),
        "a_and_b": share(timeline["ab"], timeline["ab_end"]),
        "b_only": share(timeline["b_only"], timeline["b_only_end"]),
        "a_only_after": share(timeline["a_only"], None),
    }
    locust_records = [r for r in records if not r.get("probe")]
    rl_429 = [r for r in records if r["st"] == 429]
    report = {
        "load": summarize(records, CHURN_SECS),
        "load_window": load_window(run, step_elapsed, 2, records, step_started=step_started),
        "revoke_latency_s": dist([x for x in revoke_latency if x is not None], 1, 3),
        "revoke_latency_raw": [None if x is None else round(x, 3) for x in revoke_latency],
        "new_key_latency_s": dist([x for x in new_latency if x is not None], 1, 3),
        "new_key_latency_raw": [None if x is None else round(x, 3) for x in new_latency],
        "traffic_by_phase": {k: dict(v) for k, v in phases.items()},
        "rate_limit": {
            "requests_429": len(rl_429),
            "rate_limited_owner_requests_while_limited": sum(
                1 for r in locust_records if r["k"] in rl_ids and t_rl <= r["t"] <= t_rl_off_api
            ),
            "publish_to_ack_s": round(t_rl - t_rl_api, 3),
            "clear_to_ack_s": round(t_rl_off - t_rl_off_api, 3),
        },
        "version_burst": burst,
        "versions_total": len(versions),
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": gateway_counter(gw, DROPPED),
        "config_stream_resets": stream_resets(gw),
    }
    write_report("2-churn", report)

    assert report["load_window"]["ok"], report["load_window"]
    assert hard_errors(records) == [], hard_errors(records)[:10]
    # Revokes.
    assert None not in revoke_latency, (
        f"a revoked key was still accepted after 5 s: {revoke_latency}"
    )
    assert max(revoke_latency) < REVOKE_BOUND, revoke_latency
    for key in revoked:
        late = [
            r
            for r in records
            if r["k"] == key["id"] and r["st"] == 200 and r["t"] > key["rejected_at"]
        ]
        assert not late, f"revoked key {key['id']} accepted after it was rejected: {late[:3]}"
    # New keys.
    assert None not in new_latency, f"a new key was not accepted within 5 s: {new_latency}"
    assert max(new_latency) < NEW_KEY_BOUND, new_latency
    # Models.
    assert set(phases["a_before"]) == {"mock-a"}, phases
    ab = phases["a_and_b"]
    assert ab["mock-a"] > 0 and ab["mock-b"] > 0, phases
    assert 0.3 < ab["mock-b"] / (ab["mock-a"] + ab["mock-b"]) < 0.7, phases
    assert set(phases["b_only"]) == {"mock-b"}, phases
    assert set(phases["a_only_after"]) == {"mock-a"}, phases
    # Rate limit: only the limited owner, only while limited, always with Retry-After.
    while_limited = [
        r for r in locust_records if r["k"] in rl_ids and t_rl <= r["t"] <= t_rl_off_api
    ]
    assert any(r["st"] == 429 for r in while_limited), "no 429 while the owner was limited"
    for r in rl_429:
        assert r["k"] in rl_ids, f"429 on a key outside the limited owner: {r}"
        assert r.get("c") == "rate_limit_exceeded", r
        assert t_rl_api <= r["t"] <= t_rl_off, f"429 outside the limited window: {r}"
        if not r.get("probe"):
            assert r.get("ra") and int(r["ra"]) >= 1, f"429 without Retry-After: {r}"
    # Versions: strictly increasing, gateway on the latest, nothing quarantined.
    revisions = [v["source_revision"] for v in versions]
    # compiler/publish.py:_publish_locked skips latest[1] >= revision under the scope
    # advisory lock. This scenario only compiles mutations (no rollback publications).
    assert revisions == sorted(set(revisions)), "source_revision not strictly increasing"
    created = [v["created_at"] for v in versions]
    assert created == sorted(created), "config_versions created_at not monotonic"
    assert status["acked_version"] == versions[-1]["version"], status
    assert status["quarantined_version"] is None and status["last_nack_code"] is None, status
    assert_accounting(report)
    assert report["usage_dropped"] == 0
    assert report["config_stream_resets"] == 0, "config stream dropped under steady CP"


def test_control_plane_restart_under_load(stack):
    fleet = Fleet(stack)
    gw = stack.start_gateway()
    wait_acked(stack)
    run = LoadRun(stack, gw, "cp-restart", fleet.keys, CP_SECS)
    run.start()
    try:
        run.sleep_until(15)
        failed_before = gateway_counter(gw, "lumewarp_usage_events_failed_total")
        t_stop = time.time()
        stack.stop_cp()
        wait_for(
            lambda: gw.metric("lumewarp_config_stream_connected") == 0, 10, "stream disconnected"
        )
        peak_queue = 0.0
        while run.elapsed() < 15 + CP_OUTAGE_SECS:
            peak_queue = max(peak_queue, gateway_counter(gw, "lumewarp_usage_queue_depth"))
            time.sleep(0.5)
        t_start = time.time()
        failed_during = gateway_counter(gw, "lumewarp_usage_events_failed_total") - failed_before
        stack.start_cp()
        t_cp_ready = time.time()
        wait_acked(stack, 30)
        t_acked = time.time()
        wait_for(
            lambda: gateway_counter(gw, "lumewarp_usage_queue_depth") < 1000,
            USAGE_DRAIN_BOUND,
            "usage queue drained after the CP came back",
        )
        t_drained = time.time()
        step_elapsed = run.elapsed()
    except BaseException:
        run.proc.kill()
        raise

    records = finish(stack, run)
    during = [r for r in records if t_stop <= r["t"] <= t_cp_ready]
    report = {
        "load": summarize(records, CP_SECS),
        "load_window": load_window(run, step_elapsed, 2, records, step_started=t_stop),
        "outage_s": round(t_start - t_stop, 2),
        "cp_boot_s": round(t_cp_ready - t_start, 2),
        "reconnect_ack_after_cp_ready_s": round(t_acked - t_cp_ready, 2),
        "queue_drained_after_cp_ready_s": round(t_drained - t_cp_ready, 2),
        "requests_during_outage": len(during),
        "ok_during_outage": sum(1 for r in during if r["st"] == 200),
        "peak_usage_queue_depth": peak_queue,
        "usage_report_failed_events": gateway_counter(gw, "lumewarp_usage_events_failed_total"),
        "usage_report_failed_events_during_outage": failed_during,
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": gateway_counter(gw, DROPPED),
        # Reconnect attempts while the CP is down count too; reported, not asserted.
        "config_stream_resets": stream_resets(gw),
    }
    write_report("3-cp-restart", report)
    assert report["load_window"]["ok"], report["load_window"]
    assert hard_errors(records) == [], hard_errors(records)[:10]
    assert set(report["load"]["statuses"]) == {"200"}, report["load"]["statuses"]
    # usage.rs:Reporter::send records usage_batch(0, n) on retry, then samples queue.len()
    # (lines 909-911). Either signal proves outage buffering, including an in-flight batch.
    assert report["ok_during_outage"] > 0, report
    assert peak_queue > 0 or failed_during > 0, report
    assert report["usage_dropped"] == 0, "queue overflowed; counts cannot match"
    assert_accounting(report)


@pytest.mark.parametrize("scope", ["service_account", "member", "team"])
def test_budget_under_load(stack, scope):
    owner_index = 1 if scope == "member" else 0
    fleet = Fleet(
        stack,
        budgets={owner_index: BUDGET} if scope != "team" else None,
        team_budgets={0: BUDGET} if scope == "team" else None,
    )
    gw = stack.start_gateway()
    wait_acked(stack)
    owner = fleet.owners[owner_index]
    scope_id = fleet.teams[0]["id"] if scope == "team" else owner["scope_id"]
    budget_keys = {
        k["id"]
        for k in fleet.keys
        if (k["team_id"] == scope_id if scope == "team" else k["owner_id"] == owner["id"])
    }
    run = LoadRun(stack, gw, f"budget-{scope}", fleet.keys, BUDGET_SECS)
    run.start()

    def exceeded():
        rows = pg(
            stack.dsn,
            "SELECT exceeded_at FROM usage_totals WHERE scope_type = $1"
            " AND scope_id = $2::uuid AND exceeded_at IS NOT NULL",
            scope,
            scope_id,
        )
        return rows[0]["exceeded_at"] if rows else None

    try:
        step_started = time.time()
        exceeded_at = wait_for(exceeded, BUDGET_SECS - 15, "owner budget exceeded", interval=0.2)
        t_exceeded = exceeded_at.timestamp()
        step_elapsed = run.elapsed()
    except BaseException:
        run.proc.kill()
        raise
    records = finish(stack, run)

    flips = {}
    for key in budget_keys:
        mine = sorted((r for r in records if r["k"] == key), key=lambda r: r["t"])
        first = next((r for r in mine if r["st"] == 429), None)
        assert first is not None, f"budget key {key} never got 429"
        flips[key] = first["t"] - t_exceeded
        late = [r for r in mine if r["st"] == 200 and r["t"] > first["t"] + 0.1]
        assert not late, f"budget key {key} accepted after it flipped: {late[:3]}"
    spent = pg(
        stack.dsn,
        "SELECT cost_nano_usd FROM usage_totals WHERE scope_type = $1 AND scope_id = $2::uuid",
        scope,
        scope_id,
    )[0]["cost_nano_usd"]
    after = [r for r in records if r["k"] in budget_keys and r["st"] == 200 and r["t"] > t_exceeded]
    report = {
        "load": summarize(records, BUDGET_SECS),
        "load_window": load_window(run, step_elapsed, 10, records, step_started=step_started),
        "budget_nano_usd": BUDGET,
        "budget_scope": scope,
        "budget_scope_id": scope_id,
        "spent_nano_usd": spent,
        "overshoot_nano_usd": spent - BUDGET,
        "overshoot_pct": round(100 * (spent - BUDGET) / BUDGET, 1),
        "ok_requests_after_exceeded": len(after),
        "flip_after_exceeded_s": dist(list(flips.values()), 1, 3),
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": gateway_counter(gw, DROPPED),
        "config_stream_resets": stream_resets(gw),
    }
    write_report(f"4-budget-{scope}", report)
    assert report["load_window"]["ok"], report["load_window"]
    assert hard_errors(records) == [], hard_errors(records)[:10]
    for r in records:
        if r["st"] == 429:
            assert r["k"] in budget_keys and r.get("c") == "insufficient_quota", r
        else:
            assert r["st"] == 200, r
    assert max(flips.values()) < BUDGET_BOUND, flips
    assert spent >= BUDGET
    assert_accounting(report)
    assert report["usage_dropped"] == 0
    assert report["config_stream_resets"] == 0, "config stream dropped under steady CP"


def test_rate_limit_under_load(stack):
    fleet = Fleet(stack)
    gw = stack.start_gateway()
    wait_acked(stack)
    probes = Records()
    probe = Probe(gw, probes)
    limited = fleet.owners[:2]
    limited_ids = {k["id"] for i in range(2) for k in fleet.owner_keys(i)}
    run = LoadRun(stack, gw, "rate-limit", fleet.keys, env_int("LOAD_RATE_LIMIT_SECS", 20))
    try:
        for owner in limited:
            stack.ok("PATCH", owner["policy_path"], json={"rpm_limit": 1})
        wait_acked(stack)
        # Different keys share the owner's limiter, for both member and SA scopes.
        for i in range(2):
            keys = fleet.owner_keys(i)
            assert probe.chat(keys[0]) == (200, None)
            assert probe.chat(keys[1]) == (429, "rate_limit_exceeded")
        run.start()
        records = run.wait()
        for owner in limited:
            stack.ok("PATCH", owner["policy_path"], json={"rpm_limit": None})
        wait_acked(stack)
        for i in range(2):
            assert probe.chat(fleet.owner_keys(i)[1]) == (200, None)
    except BaseException:
        run.proc.kill()
        raise
    finally:
        probe.close()
    records += probes.items
    settle_usage(stack, sum(r["st"] == 200 for r in records))
    report = {
        "load": summarize(records, run.seconds),
        "limited_owner_kinds": [o["owner_kind"] for o in limited],
        "limited_requests": {
            owner["owner_kind"]: sum(
                r["st"] == 429 for r in records if r["k"] in {k["id"] for k in fleet.owner_keys(i)}
            )
            for i, owner in enumerate(limited)
        },
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": gateway_counter(gw, DROPPED),
        "config_stream_resets": stream_resets(gw),
    }
    write_report("6-rate-limit", report)
    assert hard_errors(records) == []
    for i in range(2):
        ids = {k["id"] for k in fleet.owner_keys(i)}
        assert any(r["st"] == 429 and r["k"] in ids for r in records if not r.get("probe"))
    for r in records:
        if r["st"] == 429:
            assert r["k"] in limited_ids and r.get("c") == "rate_limit_exceeded", r
            if not r.get("probe"):
                assert r.get("ra") and int(r["ra"]) >= 1, r
        else:
            assert r["st"] == 200, r
    assert_accounting(report)
    assert report["usage_dropped"] == 0
    assert report["config_stream_resets"] == 0


def test_gateway_restart_under_load(stack):
    fleet = Fleet(stack)
    gw = stack.start_gateway()
    version = wait_acked(stack)
    run = LoadRun(stack, gw, "gw-restart", fleet.keys, GW_SECS)
    run.start()
    try:
        run.sleep_until(20)
        dropped_before = gateway_counter(gw, DROPPED)
        reported_before = gateway_counter(gw, "lumewarp_usage_events_reported_total")
        log_before = gw.proc.text().count("booting from cached snapshot")
        t_stop = time.time()
        gw.proc.stop()  # SIGTERM: graceful drain + usage flush
        t_stopped = time.time()
        gw.proc.start()  # same argv, same ports, same state dir
        gw.wait_listening()
        t_listening = time.time()
        boot_version = gw.metric("lumewarp_config_version")
        gw.wait_ready(5)
        t_ready = time.time()
        acked = wait_acked(stack, 20)
        t_acked = time.time()
        step_elapsed = run.elapsed()
    except BaseException:
        run.proc.kill()
        raise
    records = finish(stack, run)

    window = (t_stop - 0.5, t_ready + 1.0)
    conn_errors = [r for r in records if r["st"] == 0]
    by_status = defaultdict(int)
    for r in records:
        if window[0] <= r["t"] <= window[1]:
            by_status[str(r["st"])] += 1
    report = {
        "load": summarize(records, GW_SECS),
        "load_window": load_window(run, step_elapsed, 5, records, step_started=t_stop),
        "drain_s": round(t_stopped - t_stop, 2),
        "down_s": round(t_listening - t_stopped, 2),
        "ready_after_start_s": round(t_ready - t_stopped, 2),
        "reconnect_ack_s": round(t_acked - t_ready, 2),
        "boot_version": boot_version,
        "version_before": version,
        "connection_errors": len(conn_errors),
        "statuses_in_restart_window": dict(by_status),
        "usage_reported_before_stop": reported_before,
        "usage": accounting(stack, records),
        "cost": cost_check(stack),
        "usage_dropped": dropped_before + gateway_counter(gw, DROPPED),
        "config_stream_resets": stream_resets(gw),
    }
    write_report("5-gw-restart", report)
    assert report["load_window"]["ok"], report["load_window"]
    assert gw.proc.text().count("booting from cached snapshot") == log_before + 1, "not from disk"
    assert boot_version is not None and boot_version >= version, report
    assert acked >= version
    assert hard_errors(records, allow_conn=window) == [], hard_errors(records, window)[:10]
    assert {r["st"] for r in records} <= {200, 0}, Counter(r["st"] for r in records)
    assert report["usage_dropped"] == 0
    assert_accounting(report)
    assert report["config_stream_resets"] == 0, "config stream dropped under steady CP"

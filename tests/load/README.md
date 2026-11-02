# Control-plane-mode e2e under load

The P1 stack as real processes (Postgres database, control plane, release gateway in
control-plane mode, mock upstream; harness from `tests/e2e/stack.py`) driven by headless
Locust (`locust==2.32.4` via `uvx`, `FastHttpUser`, several processes) while the tests
churn config and restart processes. Each scenario gets a fresh stack, so usage accounting is
exact: every request a client made (Locust or the orchestrator's probes) is recorded, and
the usage rows in Postgres must match them per key — 200s per key and stream flag, 429s per
key — with no duplicates, and per-team and owner cost must equal tokens × model price in both
`usage_events` and `usage_totals`.

    scripts/load-e2e.sh                 # build, start dev Postgres if needed, run all
    LOAD_REPEAT=2 scripts/load-e2e.sh   # two consecutive full runs
    scripts/load-e2e.sh -k churn        # one scenario
    LOAD_STEADY_SECS=10 LOAD_USERS=4 LOAD_USER_RPS=5 LOAD_PROCESSES=1 \
      LOAD_RESULTS_DIR=/tmp/lumewarp-load-smoke scripts/load-e2e.sh -k steady

The last command is a short mixed-load smoke check against the local mock only. It keeps
the full 60-key fleet and the same usage, owner cost, and stream assertions.

Pure helper tests need no binaries, Postgres, or Locust (CI runs this in the
control-plane job):

    PYTHONDONTWRITEBYTECODE=1 \
      E2E_GATEWAY_BIN=/nonexistent E2E_MOCK_BIN=/nonexistent \
      uv run --frozen --project control-plane pytest -c tests/load/pytest.ini \
      --rootdir tests/load tests/load/test_loadlib_unit.py -q -p no:cacheprovider

Elapsed-time guard rails remain failures unless a Locust CPU saturation warning, recorded with
its monotonic time when logged (`cpu-warnings-<pid>.jsonl`, not read back from the log's wall clock
stamps), falls within the orchestration step, with successful non-stream p99 below `LOAD_SAT_P99_S`
and stream p99 below `LOAD_SAT_STREAM_P99_S` during that step, and no hard errors.
Both request types must be observed. Steps ending after the total load duration always fail.
Qualifying overruns are recorded in `load_window` as diagnostics; revoke/new-key/budget
latency bounds, traffic assertions, and exact accounting still apply. The CP outage must
show a positive queue depth or an increase in usage report retry events during the outage.

| scenario | what it asserts |
| --- | --- |
| `test_steady_mixed_load` | 60 keys over 6 teams × 2 owners (one service account and one member using a PAT) × 5 keys, chat + stream mix: zero 5xx, zero connection errors, well-formed bodies/SSE (no leaked usage-only chunk), exact usage + cost accounting at team and owner scopes, no dropped events, config stream stays up. |
| `test_churn_under_load` | Under load: revoke keys (each rejected < 2 s, measured per key, never accepted after the first 401), create keys (accepted < 2 s), add model `mock-b` (1:1 split), weight `mock-a` to 0 (all `mock-b`), remove `mock-b` (all `mock-a`) — checked by the config version each request was served under; lower an owner rpm limit (429 `rate_limit_exceeded` + `Retry-After` only for that owner's keys and window, never 5xx), clear it; publish 40 versions back to back through owner policy changes (gateway ACKs the latest, `source_revision` strictly increasing, nothing quarantined). |
| `test_control_plane_restart_under_load` | CP stopped for 20 s mid-run: every request still 200, usage queued meanwhile is delivered after restart (exact counts, drop counter 0), gateway reconnects and ACKs. |
| `test_budget_under_load` | Separate service-account, member and team cases with a 0.01 USD budget: every affected key flips to 429 `insufficient_quota` < 5 s after the budget was exceeded at ingestion and is never accepted after its flip; keys outside the scope remain unaffected; overshoot recorded. |
| `test_rate_limit_under_load` | A service account and member each share an RPM limiter across their keys: 429 `rate_limit_exceeded` with `Retry-After`, unrelated owners unaffected, clearing both limits restores service, exact usage and cost accounting. |
| `test_gateway_restart_under_load` | Gateway SIGTERM + restart on the same ports: boots from the on-disk snapshot, reconnects, connection errors only inside the restart window, never 5xx, no lost or duplicated usage. |

Elapsed times, including the `t` of each request record, are `CLOCK_MONOTONIC`, and Locust is
paced on it (`loadpacing.Pacer`) rather than by its own `constant_throughput`, which reads
`time.time()`.

The one wall clock value is `exceeded_at`, set by the control plane before its transaction commits.
The budget scenario converts it with wall-minus-monotonic samples taken around the measurement, so
the 5 s flip bound still runs from the threshold evaluation. If the samples show the offset moving
inside the window the flip is unmeasured: the scenario runs again on a fresh stack, at most twice,
then fails saying the clock stepped. It never passes on an unmeasured flip.

What was shown about clock steps: with `time.time()` in the Locust processes alone shimmed to jump
by 24.9 s and back (`sitecustomize` on `PYTHONPATH`), Locust's `constant_throughput` stalled each
user for about 25 s on each step back: about 70 RPS against the 400 target, the budget never reached
within the scenario, and one revoke measured as 25 s. That is a reproduced mechanism, nothing more.
It does not show that the four load failures seen on `main` had this cause: their assertion text
was not kept and no real clock step was observed in those runs, and the control-plane restart
failure was not reproduced. A real step across the control plane, Postgres and the gateway has not
been tested.

Reports: `tests/load/results/<UTC timestamp>/` (`summary.md`, one JSON per scenario with RPS,
latency p50/p90/p99/max, revoke and new-key latency distributions, budget overshoot,
restart timings; Locust CSVs and log per scenario). Final request counts come from Locust's
quit event after users and workers drain, not the last periodic CSV snapshot.
Failing tests print every process log.

Environment (all optional):

| variable | default | meaning |
| --- | --- | --- |
| `E2E_PG_URL` | dev Postgres `127.0.0.1:55432` | server DSN; tests create/drop `lumewarp_e2e_*` databases |
| `LOAD_USERS` / `LOAD_USER_RPS` | 40 / 10 | Locust users and requests per second per user (~400 RPS) |
| `LOAD_PROCESSES` | 4 | Locust processes |
| `LOAD_STREAM_SHARE` | 0.3 | share of streaming requests |
| `LOAD_SAT_P99_S`, `LOAD_SAT_STREAM_P99_S` | 0.1, 0.25 | strict non-stream / stream p99 ceilings for generator saturation classification |
| `LOAD_STEADY_SECS`, `LOAD_CHURN_SECS`, `LOAD_CP_SECS`, `LOAD_BUDGET_SECS`, `LOAD_GW_SECS` | 150, 110, 90, 50, 50 | scenario run times |
| `LOAD_CP_OUTAGE_SECS` | 20 | control-plane downtime |
| `LOAD_REVOKE_BOUND`, `LOAD_NEW_KEY_BOUND`, `LOAD_BUDGET_BOUND` | 2, 2, 5 | latency bounds (s) |
| `LOAD_USAGE_DRAIN_BOUND` | 50 | after a CP restart, seconds for the gateway usage queue to drain (its retry backoff caps at 30 s) |
| `LOAD_BUDGET_NANO_USD` | 10000000 | tiny owner budget |
| `LOAD_RATE_LIMIT_SECS` | 20 | mixed-owner rate-limit scenario run time |
| `LOAD_BURST`, `LOAD_BURST_GAP` | 40, 0.05 | version burst size and spacing (s) |
| `LOAD_REPEAT`, `LOAD_RESULTS_DIR` | 1, timestamped | consecutive runs, report directory |
| `LOAD_CP_WRAPPER` | – | run the control plane as `python <script> serve` (profiler, coverage, local patch) |
| `LOAD_LOCUST` | `uvx --from locust==2.32.4 locust` | Locust command |

The script waits while a `bench/scripts/run*.sh` benchmark is running (both would skew).
Ports are ephemeral; 9000, 5432, 8080 and 8081 are never used.

Budget and rate-limit smoke checks (mock upstream only; all 60 keys retained):

    LOAD_USERS=8 LOAD_USER_RPS=10 LOAD_PROCESSES=1 \
      LOAD_BUDGET_SECS=30 LOAD_BUDGET_NANO_USD=100000 LOAD_RATE_LIMIT_SECS=10 \
      LOAD_RESULTS_DIR=/tmp/lumewarp-owner-smoke \
      scripts/load-e2e.sh -k 'budget or rate_limit'

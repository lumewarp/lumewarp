#!/usr/bin/env bash
# Control-plane-mode e2e under load (tests/load): Postgres + control plane + gateway
# (release) + mock upstream as real processes, driven by headless Locust while the tests
# churn config, restart the control plane / gateway and exhaust a budget. Every request is
# checked against the usage rows in Postgres.
#
#   scripts/load-e2e.sh [pytest args...]      e.g. -k churn
#
# Environment (defaults in tests/load/README.md):
#   E2E_PG_URL        server DSN; tests create/drop their own lumewarp_e2e_* databases.
#                     Default: dev Postgres 127.0.0.1:55432 (scripts/dev-db.sh up if down).
#   LOAD_REPEAT       consecutive full runs (default 1); stops at the first failure.
#   LOAD_RESULTS_DIR  report directory (default tests/load/results/<UTC timestamp>).
#   LOAD_USERS, LOAD_USER_RPS, LOAD_PROCESSES, LOAD_STREAM_SHARE, LOAD_*_SECS, LOAD_*_BOUND.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
export E2E_PG_URL="${E2E_PG_URL:-postgresql://lumewarp:lumewarp@127.0.0.1:55432/lumewarp}"
export LOAD_LOCUST="${LOAD_LOCUST:-uvx --from locust==2.32.4 locust}"
repeat="${LOAD_REPEAT:-1}"
results_base="${LOAD_RESULTS_DIR:-$root/tests/load/results/$(date -u +%Y%m%dT%H%M%SZ)}"

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/lumewarp-load-run.XXXXXX")"
export E2E_RUN_DIR="$run_dir"
touch "$run_dir/pids" "$run_dir/dbs"

# Same pg_dump scan as scripts/e2e-p1.sh: each disposable database is scanned in
# tests/e2e/stack.py::teardown before it is dropped, and the finalizer below covers
# leftovers, databases matching this run's names that no test registered, and fails the
# script on a hit. The run id in the database names is what makes that discovery possible.
run_id="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d '[:space:]')"
export E2E_RUN_ID="$run_id"
echo "$run_id" >"$run_dir/run-id"
export E2E_SECRET_LOG="$run_dir/secrets.jsonl"
export E2E_SCAN_REPORT="$run_dir/secret-scans.jsonl"
: >"$E2E_SECRET_LOG"
: >"$E2E_SCAN_REPORT"

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [[ -n "${pytest_pid:-}" ]] && kill -0 "$pytest_pid" 2>/dev/null; then
    # Interrupted: let pytest's fixtures tear down (SIGINT), then fall through to the reaper.
    kill -INT -- "-$pytest_pid" 2>/dev/null || true
    for _ in $(seq 1 150); do kill -0 "$pytest_pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "-$pytest_pid" 2>/dev/null || true
  fi
  # pytest removes its own processes and databases; this reaps whatever a hard kill left
  # (gateways, control plane, mock upstream, Locust and its worker processes).
  while read -r pid; do
    [[ -n "$pid" ]] && kill -KILL -- "-$pid" 2>/dev/null || true
  done <"$run_dir/pids"
  # After the tests and before the reaper drops anything: no plaintext credential may
  # have reached any database this run created. A hit, a missing scan result or a scan
  # error fails the script. The only opt-out is E2E_SKIP_SECRET_SCAN=1.
  if [[ "${E2E_SKIP_SECRET_SCAN:-0}" != 1 ]]; then
    if ! (cd "$root" && uv run --frozen --project control-plane python \
        tests/e2e/secret_scan.py finalize --run-dir "$run_dir" --pg-url "$E2E_PG_URL"); then
      echo "error: plaintext credentials found in the load e2e databases" >&2
      status=1
    fi
  fi
  if [[ -s "$run_dir/dbs" ]]; then
    (cd "$root/control-plane" && uv run --frozen python - "$E2E_PG_URL" "$run_dir/dbs" <<'PY' || true
import asyncio, sys
import asyncpg

async def main(dsn, path):
    conn = await asyncpg.connect(dsn)
    try:
        for name in open(path).read().split():
            if name.startswith("lumewarp_e2e_"):
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
PY
    )
  fi
  rm -rf "$run_dir"
  exit "$status"
}
trap cleanup EXIT
trap "exit 130" INT
trap "exit 143" TERM

if [[ "$E2E_PG_URL" == *"127.0.0.1:55432"* ]] && ! (exec 3<>/dev/tcp/127.0.0.1/55432) 2>/dev/null; then
  "$root/scripts/dev-db.sh" up
fi

echo "== build gateway (release) and mock upstream"
(cd "$root/gateway" && cargo build --release -p lumewarp-gateway)
(cd "$root/bench/mock-upstream" && cargo build --release --bin mock-upstream)
export E2E_GATEWAY_BIN="${E2E_GATEWAY_BIN:-$root/gateway/target/release/lumewarp}"
export E2E_MOCK_BIN="${E2E_MOCK_BIN:-$root/bench/mock-upstream/target/release/mock-upstream}"

echo "== locust"
$LOAD_LOCUST --version

# A concurrent benchmark (bench/scripts/run*.sh) skews both results: wait for it.
waited=0
while pgrep -f 'bench/scripts/run(-locust)?\.sh' >/dev/null; do
  [[ $waited == 0 ]] && echo "== waiting for a running bench/scripts benchmark to finish"
  waited=1
  sleep 10
done

cd "$root"
for i in $(seq 1 "$repeat"); do
  results="$results_base"
  [[ "$repeat" -gt 1 ]] && results="$results_base/run-$i"
  mkdir -p "$results"
  export LOAD_RESULTS_DIR="$results"
  echo "== load e2e run $i/$repeat (report: $results/summary.md)"
  # Own process group (setsid) so an interrupt can stop pytest and let it clean up first.
  setsid uv run --frozen --project control-plane pytest -c tests/load/pytest.ini \
    --rootdir tests/load tests/load/test_load.py -q --durations=0 -p no:cacheprovider "$@" &
  pytest_pid=$!
  wait "$pytest_pid"
  pytest_pid=
done

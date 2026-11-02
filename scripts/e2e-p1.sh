#!/usr/bin/env bash
# P1 end-to-end exit gate: Postgres + control plane + gateway + mock upstream as real
# processes (tests/e2e). Builds the gateway and the mock upstream, then runs pytest in the
# control-plane's uv environment.
#
#   scripts/e2e-p1.sh [pytest args...]
#
# Environment:
#   E2E_PG_URL   server DSN (postgresql://user:pass@host:port/db); the tests create and drop
#                their own lumewarp_e2e_* databases there. Default: the dev Postgres on
#                127.0.0.1:55432, started with scripts/dev-db.sh up if it is not reachable.
#   E2E_PROFILE  gateway build profile: debug (default) or release.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
profile="${E2E_PROFILE:-debug}"
export E2E_PG_URL="${E2E_PG_URL:-postgresql://lumewarp:lumewarp@127.0.0.1:55432/lumewarp}"

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/lumewarp-e2e-run.XXXXXX")"
export E2E_RUN_DIR="$run_dir"
touch "$run_dir/pids" "$run_dir/dbs"

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [[ -n "${pytest_pid:-}" ]] && kill -0 "$pytest_pid" 2>/dev/null; then
    # Interrupted: let pytest's fixtures tear down (SIGINT), then fall through to the reaper.
    kill -INT -- "-$pytest_pid" 2>/dev/null || true
    for _ in $(seq 1 100); do kill -0 "$pytest_pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "-$pytest_pid" 2>/dev/null || true
  fi
  # pytest removes its own processes and databases; this reaps whatever a hard kill left.
  while read -r pid; do
    [[ -n "$pid" ]] && kill -KILL -- "-$pid" 2>/dev/null || true
  done <"$run_dir/pids"
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

echo "== build gateway with the reference strategy crate"
# Before the plain build, which then leaves the usual binary at the usual path.
(cd "$root/gateway" && cargo build -p lumewarp-gateway --features strategy-priority-failover)
cp "$root/gateway/target/debug/lumewarp" "$run_dir/lumewarp-with-strategy"
export E2E_STRATEGY_GATEWAY_BIN="$run_dir/lumewarp-with-strategy"

echo "== build gateway ($profile) and mock upstream"
if [[ "$profile" == release ]]; then
  (cd "$root/gateway" && cargo build --release -p lumewarp-gateway)
else
  (cd "$root/gateway" && cargo build -p lumewarp-gateway)
fi
(cd "$root/bench/mock-upstream" && cargo build --release --bin mock-upstream)
export E2E_GATEWAY_BIN="${E2E_GATEWAY_BIN:-$root/gateway/target/$profile/lumewarp}"
export E2E_MOCK_BIN="${E2E_MOCK_BIN:-$root/bench/mock-upstream/target/release/mock-upstream}"

echo "== e2e"
cd "$root"
# Own process group (setsid) so an interrupt can stop pytest and let it clean up first.
setsid uv run --frozen --project control-plane pytest -c tests/e2e/pytest.ini \
  --rootdir tests/e2e tests/e2e -q --durations=0 "$@" &
pytest_pid=$!
wait "$pytest_pid"

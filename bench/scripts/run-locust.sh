#!/usr/bin/env bash
#
# End-to-end Locust load suite on the same benchmark setup as run.sh.
#
# Starts the same mock upstream, Lumewarp and LiteLLM (same configs, same
# BENCH_AUTH handling), then runs one headless, multi-process Locust run per
# target with a mixed OpenAI-compatible workload (chat, chat SSE with and
# without include_usage, completions, embeddings, models, plus a small share of
# negative requests on the gateways). Every response is checked; see
# bench/locust/locustfile.py.
#
# Results go to bench/results/<RUN_ID>/locust/ (summary.md + results.json).
#
# Usage: bench/scripts/run-locust.sh [--quick|--full] [--no-build] [-h|--help]
#
# Exit codes: 0 for a completed run (failures/saturation are recorded),
#             2 for infrastructure failure.
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$BENCH_DIR/.." && pwd)"

log() { printf '[locust] %s\n' "$*" >&2; }
die() {
  printf '[locust] ERROR: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Locust load/e2e suite: direct mock vs Lumewarp vs LiteLLM, mixed workload.

Usage: bench/scripts/run-locust.sh [--quick|--full] [--no-build] [-h|--help]

  --quick     step load 100 -> 500 users (24 s per step, ~2 min per target)
  --full      constant-rate stages 300/1000/5000/10000 RPS (5 s warmup + 30 s
              each) plus a soak stage (1000 RPS, 300 s), per target
  --no-build  do not run `cargo build --release` for mock/gateway

Each user runs USER_RPS tasks per second (locust constant_throughput), so the
offered rate is users x USER_RPS. Scenarios repeat REPEAT times (quick 1,
full 3) with the target order rotated per repetition, like run.sh.

Environment overrides (defaults):
  MOCK_PORT=19100 LUMEWARP_PORT=4100 LITELLM_PORT=4101
                              (9000, 5432, 8080 and 8081 are refused)
                              Live service ports are also refused; the gateway
                              admin listener uses an ephemeral local port.
  MOCK_LATENCY_MS=50 MOCK_STREAM_CHUNKS=20 MOCK_CHUNK_INTERVAL_MS=10
  MOCK_HEADERS_FIRST=0        as in run.sh
  BENCH_AUTH=0                1: generate LOCUST_KEYS keys; Lumewarp gets all of
                              their SHA-256s, LiteLLM the first one as
                              master_key (no DB, so no virtual keys)
  LOCUST_KEYS=8               number of client keys with BENCH_AUTH=1
  LOCUST_VERSION=2.32.4       pinned, run through uvx (no global install)
  GEVENTHTTPCLIENT_VERSION=2.6.1
  LOCUST_PROCESSES            load generator processes (quick 8, full 12)
  LOCUST_MASTER_PORT          master<->worker port on 127.0.0.1 (default: a free
                              port picked before startup and reused across runs,
                              avoiding locust's default 5557)
  USER_RPS=2                  tasks per second per user
  LOCUST_WEIGHTS              e.g. "chat=55,stream=25,completions=5,embeddings=10,models=5,negative=2"
  STREAM_USAGE_SHARE=0.5      share of streams asking for include_usage
  LW_ACCEPT_MODELS=mock       comma-separated accepted response models
  LW_MAX_TOKENS=16            maximum requested output tokens (model stays mock)
  QUICK_STEPS="100 200 300 400 500"  QUICK_STEP_SECS=24
  RATES="300 1000 5000 10000" STAGE_SECS=30 WARMUP_SECS=5   (full)
  SOAK_RATE=1000 SOAK_SECS=300 (full; SOAK_SECS=0 disables the soak)
  REPEAT=<profile>            repetitions (quick 1, full 3)
  GEN_CPU_LIMIT_PCT=85        flag a run when the generator's average CPU per
                              process is above this
  LITELLM_VERSION=1.106.2 LITELLM_WORKERS=4
  SETTLE_SECS=10 HEALTH_TIMEOUT_SECS=180
  RESULTS_DIR=bench/results RUN_ID=<UTC timestamp>
  MOCK_BIN / LUMEWARP_BIN     override binary paths
EOF
}

PROFILE=quick
BUILD=1
while (($# > 0)); do
  case "$1" in
    --quick) PROFILE=quick ;;
    --full) PROFILE=full ;;
    --no-build) BUILD=0 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
# Knobs
# --------------------------------------------------------------------------- #

MOCK_PORT="${MOCK_PORT:-19100}"
LUMEWARP_PORT="${LUMEWARP_PORT:-4100}"
LITELLM_PORT="${LITELLM_PORT:-4101}"
MOCK_LATENCY_MS="${MOCK_LATENCY_MS:-50}"
MOCK_STREAM_CHUNKS="${MOCK_STREAM_CHUNKS:-20}"
MOCK_CHUNK_INTERVAL_MS="${MOCK_CHUNK_INTERVAL_MS:-10}"
MOCK_HEADERS_FIRST="${MOCK_HEADERS_FIRST:-0}"
BENCH_AUTH="${BENCH_AUTH:-0}"
LOCUST_KEYS="${LOCUST_KEYS:-8}"
LOCUST_VERSION="${LOCUST_VERSION:-2.32.4}"
GEVENTHTTPCLIENT_VERSION="${GEVENTHTTPCLIENT_VERSION:-2.6.1}"
LITELLM_VERSION="${LITELLM_VERSION:-1.106.2}"
LITELLM_WORKERS="${LITELLM_WORKERS:-4}"
HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-180}"
SETTLE_SECS="${SETTLE_SECS:-10}"
RESULTS_DIR="${RESULTS_DIR:-$BENCH_DIR/results}"
USER_RPS="${USER_RPS:-2}"
LOCUST_WEIGHTS="${LOCUST_WEIGHTS:-}"
STREAM_USAGE_SHARE="${STREAM_USAGE_SHARE:-0.5}"
LW_MODEL=mock
LW_ACCEPT_MODELS="${LW_ACCEPT_MODELS:-$LW_MODEL}"
LW_MAX_TOKENS="${LW_MAX_TOKENS:-16}"
GEN_CPU_LIMIT_PCT="${GEN_CPU_LIMIT_PCT:-85}"

MOCK_BIN="${MOCK_BIN:-$BENCH_DIR/mock-upstream/target/release/mock-upstream}"
LUMEWARP_BIN="${LUMEWARP_BIN:-$REPO_ROOT/gateway/target/release/lumewarp}"
LOCUSTFILE="$BENCH_DIR/locust/locustfile.py"

if [[ "$PROFILE" == "quick" ]]; then
  DEFAULT_REPEAT=1
  DEFAULT_PROCESSES=8
else
  DEFAULT_REPEAT=3
  DEFAULT_PROCESSES=12
fi
REPEAT="${REPEAT:-$DEFAULT_REPEAT}"
LOCUST_PROCESSES="${LOCUST_PROCESSES:-$DEFAULT_PROCESSES}"
read -r -a QUICK_STEP_LIST <<< "${QUICK_STEPS:-100 200 300 400 500}"
QUICK_STEP_SECS="${QUICK_STEP_SECS:-24}"
read -r -a RATE_LIST <<< "${RATES:-300 1000 5000 10000}"
STAGE_SECS="${STAGE_SECS:-30}"
WARMUP_SECS="${WARMUP_SECS:-5}"
SOAK_RATE="${SOAK_RATE:-1000}"
SOAK_SECS="${SOAK_SECS:-300}"

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_pos() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
for v in "${QUICK_STEP_LIST[@]}" "${RATE_LIST[@]}"; do
  is_pos "$v" || die "invalid step/rate: $v (positive integer required)"
done
for name in REPEAT LOCUST_PROCESSES QUICK_STEP_SECS STAGE_SECS SOAK_RATE LOCUST_KEYS LITELLM_WORKERS MOCK_STREAM_CHUNKS LW_MAX_TOKENS; do
  is_pos "${!name}" || die "$name must be a positive integer (got: ${!name})"
done
for name in WARMUP_SECS SOAK_SECS MOCK_LATENCY_MS GEN_CPU_LIMIT_PCT MOCK_CHUNK_INTERVAL_MS; do
  is_uint "${!name}" || die "$name must be a non-negative integer (got: ${!name})"
done
[[ "$USER_RPS" =~ ^[0-9]+([.][0-9]+)?$ ]] && [[ "$USER_RPS" != "0" ]] ||
  die "USER_RPS must be a positive number (got: $USER_RPS)"
python3 -I - "$STREAM_USAGE_SHARE" "$LOCUST_WEIGHTS" "$MOCK_STREAM_CHUNKS" "$MOCK_CHUNK_INTERVAL_MS" "$USER_RPS" <<'PY' ||
import math
import sys

try:
    share = float(sys.argv[1])
    if not 0 <= share <= 1:
        raise ValueError("STREAM_USAGE_SHARE must be between 0 and 1")
    weights = dict(chat=55, stream=25, completions=5, embeddings=10, models=5, negative=2)
    for entry in sys.argv[2].split(","):
        if not entry.strip():
            continue
        key, sep, raw = entry.partition("=")
        key = key.strip()
        if not sep or key not in weights:
            raise ValueError(f"LOCUST_WEIGHTS: invalid entry {entry!r} (known: {', '.join(weights)})")
        if int(raw) < 0:
            raise ValueError(f"LOCUST_WEIGHTS: negative weight for {key}")
        weights[key] = int(raw)
    if not any(value > 0 for key, value in weights.items() if key != "negative"):
        raise ValueError("LOCUST_WEIGHTS must enable at least one positive endpoint")
    if int(sys.argv[3]) > 2**32 - 1:
        raise ValueError("MOCK_STREAM_CHUNKS must fit in an unsigned 32-bit integer")
    if int(sys.argv[4]) > 2**64 - 1:
        raise ValueError("MOCK_CHUNK_INTERVAL_MS must fit in an unsigned 64-bit integer")
    if not math.isfinite(float(sys.argv[5])) or float(sys.argv[5]) <= 0:
        raise ValueError("USER_RPS must be a finite positive number")
except ValueError as exc:
    print(f"[locust] ERROR: {exc}", file=sys.stderr)
    sys.exit(2)
PY
  die "invalid workload configuration"
[[ "$MOCK_HEADERS_FIRST" == "0" || "$MOCK_HEADERS_FIRST" == "1" ]] ||
  die "MOCK_HEADERS_FIRST must be 0 or 1"
case "$BENCH_AUTH" in
  0) AUTH=0 AUTH_MODE=off ;;
  1) AUTH=1 AUTH_MODE=on ;;
  *) die "BENCH_AUTH must be 0 or 1 (got: $BENCH_AUTH)" ;;
esac
validate_port() {
  local port="$1"
  is_pos "$port" || die "invalid port: $port"
  if (( ${#port} > 5 )) || (( port > 65535 )); then
    die "invalid port: $port"
  fi
  case "$port" in
    9000 | 5432 | 8080 | 8081) die "port $port is reserved on bench machines (MinIO/Postgres/dev servers); pick another" ;;
    4000 | 9464 | 8091 | 50051 | 9100 | 55432) die "port $port belongs to a live service; pick another" ;;
  esac
}
for port in "$MOCK_PORT" "$LUMEWARP_PORT" "$LITELLM_PORT"; do
  validate_port "$port"
done
select_master_port() {
  local port="${LOCUST_MASTER_PORT:-}"
  if [[ -n "$port" ]]; then
    validate_port "$port"
    validate_port "$((port + 1))"
    printf '%s\n' "$port"
    return
  fi
  while true; do
    port="$(python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')" ||
      die "failed to pick a Locust master port"
    if (validate_port "$port" && validate_port "$((port + 1))") 2>/dev/null; then
      printf '%s\n' "$port"
      return
    fi
  done
}
LOCUST_MASTER_PORT="$(select_master_port)"

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid RUN_ID: $RUN_ID"

# users = rate / USER_RPS (at least 1).
users_for_rate() {
  python3 -I -c 'import math, sys; print(max(1, math.ceil(float(sys.argv[1]) / float(sys.argv[2]))))' "$1" "$USER_RPS"
}

# --------------------------------------------------------------------------- #
# Process-tree helpers (same approach as run.sh)
# --------------------------------------------------------------------------- #

tree_pids() {
  local root="$1"
  local -a queue=("$root") out=()
  local -A seen=()
  local pid child ppid stat_file raw
  while (( ${#queue[@]} > 0 )); do
    pid="${queue[0]}"
    queue=("${queue[@]:1}")
    [[ -n "${seen[$pid]:-}" ]] && continue
    seen["$pid"]=1
    [[ -d "/proc/$pid" ]] && out+=("$pid")
    for stat_file in /proc/[0-9]*/stat; do
      child="${stat_file#/proc/}"
      child="${child%/stat}"
      ppid=""
      if [[ -r "$stat_file" ]]; then
        raw="$(<"$stat_file")" 2>/dev/null || continue
        raw="${raw##*)}"
        # shellcheck disable=SC2086 # intentional word splitting of stat fields
        set -- $raw
        ppid="${2:-}"
      fi
      [[ -n "$ppid" && "$ppid" == "$pid" ]] && queue+=("$child")
    done
  done
  (( ${#out[@]} > 0 )) && printf '%s\n' "${out[@]}"
}

kill_tree() {
  local root="$1" sig="${2:-TERM}"
  local -a pids=()
  local i
  mapfile -t pids < <(tree_pids "$root")
  for (( i = ${#pids[@]} - 1; i >= 0; i-- )); do
    kill -s "$sig" "${pids[$i]}" 2>/dev/null || true
  done
}

# --------------------------------------------------------------------------- #
# Tooling and build
# --------------------------------------------------------------------------- #

for tool in curl python3 uvx; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done
[[ -f "$LOCUSTFILE" ]] || die "locustfile not found: $LOCUSTFILE"
if (( BUILD )); then
  command -v cargo >/dev/null 2>&1 || die "missing required tool: cargo (or pass --no-build)"
  log "building mock-upstream (release)"
  (cd "$BENCH_DIR/mock-upstream" && cargo build --release)
  log "building lumewarp (release)"
  (cd "$REPO_ROOT/gateway" && cargo build --release)
fi
[[ -x "$MOCK_BIN" ]] || die "mock-upstream binary not found: $MOCK_BIN"
[[ -x "$LUMEWARP_BIN" ]] || die "lumewarp binary not found: $LUMEWARP_BIN"

LOCUST_CMD=(uvx --from "locust==${LOCUST_VERSION}" --with "geventhttpclient==${GEVENTHTTPCLIENT_VERSION}" locust)
locust_version="$("${LOCUST_CMD[@]}" --version 2>/dev/null | head -n1)" ||
  die "failed to run locust ${LOCUST_VERSION} via uvx"
log "locust: $locust_version"
python3 -I "$LOCUSTFILE" --selftest >/dev/null || die "locustfile selftest failed"

fd_limit="$(ulimit -n)"
if (( fd_limit < 4096 )); then
  log "warning: open-file limit is ${fd_limit}; high user counts may run out of sockets"
fi

# --------------------------------------------------------------------------- #
# Client keys (BENCH_AUTH=1)
# --------------------------------------------------------------------------- #

# LOCUST_KEYS random keys per run, never written to meta/logs. Lumewarp gets
# every key's SHA-256 in server.api_keys and locust users spread over all of
# them. LiteLLM without a DB only knows general_settings.master_key (virtual
# keys need Postgres), so it gets the first key and its users all send that.
CLIENT_KEYS=()
KEY_HASHES=()
if (( AUTH )); then
  while read -r key hash; do
    [[ "$key" =~ ^[0-9a-f]{64}$ && "$hash" =~ ^[0-9a-f]{64}$ ]] || die "failed to generate bench keys"
    CLIENT_KEYS+=("$key")
    KEY_HASHES+=("$hash")
  done < <(
    python3 -I -c 'import hashlib, secrets, sys
for _ in range(int(sys.argv[1])):
    key = secrets.token_hex(32)
    print(key, hashlib.sha256(key.encode()).hexdigest())' "$LOCUST_KEYS"
  )
  (( ${#CLIENT_KEYS[@]} == LOCUST_KEYS )) || die "failed to generate ${LOCUST_KEYS} bench keys"
  log "client auth: on (${LOCUST_KEYS} per-run keys on lumewarp, key 1 as LiteLLM master_key; direct stays open)"
else
  log "client auth: off"
fi

# --------------------------------------------------------------------------- #
# Run directory
# --------------------------------------------------------------------------- #

mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd -- "$RESULTS_DIR" && pwd)"
RUN_DIR="$RESULTS_DIR/$RUN_ID"
# Share the directory with a run.sh run of the same RUN_ID (CI), but never
# overwrite an earlier locust run.
if [[ -e "$RUN_DIR/locust" ]]; then
  suffix=1
  while [[ -e "$RESULTS_DIR/${RUN_ID}-${suffix}/locust" ]]; do
    suffix=$((suffix + 1))
  done
  RUN_DIR="$RESULTS_DIR/${RUN_ID}-${suffix}"
fi
RUN_ID="$(basename "$RUN_DIR")"
OUT_DIR="$RUN_DIR/locust"
mkdir -p "$OUT_DIR/logs" "$OUT_DIR/configs"
: > "$OUT_DIR/scenarios.jsonl"
log "output: $OUT_DIR"
log "profile: $PROFILE, auth: $AUTH_MODE, repetitions: $REPEAT, generator processes: $LOCUST_PROCESSES"

# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #

SERVER_PIDS=()
SAMPLER_PIDS=()
LOAD_PID=""
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

stop_samplers() {
  local pid
  for pid in "${SAMPLER_PIDS[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${SAMPLER_PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  SAMPLER_PIDS=()
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$LOAD_PID" ]] && kill -0 "$LOAD_PID" 2>/dev/null; then
    kill_tree "$LOAD_PID" TERM
  fi
  stop_samplers
  local pid round alive
  for pid in "${SERVER_PIDS[@]}"; do
    kill_tree "$pid" TERM
  done
  for round in 1 2 3 4 5; do
    alive=0
    for pid in "${SERVER_PIDS[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    (( alive == 0 )) && break
    sleep 0.2
  done
  if [[ -n "$LOAD_PID" ]] && kill -0 "$LOAD_PID" 2>/dev/null; then
    kill_tree "$LOAD_PID" KILL
  fi
  for pid in "${SERVER_PIDS[@]}"; do
    kill_tree "$pid" KILL
  done
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
# Start servers (same configs and templating as run.sh)
# --------------------------------------------------------------------------- #

require_port_free() {
  if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
    die "port $1 is already in use; free it or override MOCK_PORT/LUMEWARP_PORT/LITELLM_PORT"
  fi
}

wait_for_http() {
  local name="$1" url="$2" timeout="$3" pid="$4" logfile="$5"
  shift 5
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 40 "$logfile" >&2 2>/dev/null || true
      die "$name exited before becoming healthy (see $logfile)"
    fi
    if curl -fsS -m 2 -o /dev/null "$@" "$url" 2>/dev/null; then
      log "$name healthy at $url"
      return 0
    fi
    sleep 1
  done
  tail -n 60 "$logfile" >&2 2>/dev/null || true
  die "$name not healthy after ${timeout}s at $url (see $logfile)"
}

require_port_free "$MOCK_PORT"
require_port_free "$LUMEWARP_PORT"
require_port_free "$LITELLM_PORT"

MOCK_EXTRA_ARGS=()
[[ "$MOCK_HEADERS_FIRST" == "1" ]] && MOCK_EXTRA_ARGS+=(--headers-first)
log "starting mock upstream on 127.0.0.1:$MOCK_PORT (latency ${MOCK_LATENCY_MS}ms, ${MOCK_STREAM_CHUNKS} chunks x ${MOCK_CHUNK_INTERVAL_MS}ms)"
"$MOCK_BIN" \
  --listen "127.0.0.1:$MOCK_PORT" \
  --latency-ms "$MOCK_LATENCY_MS" \
  --stream-chunks "$MOCK_STREAM_CHUNKS" \
  --chunk-interval-ms "$MOCK_CHUNK_INTERVAL_MS" \
  "${MOCK_EXTRA_ARGS[@]}" \
  >"$OUT_DIR/logs/mock.log" 2>&1 &
MOCK_PID=$!
SERVER_PIDS+=("$MOCK_PID")
wait_for_http "mock upstream" "http://127.0.0.1:$MOCK_PORT/health" "$HEALTH_TIMEOUT_SECS" "$MOCK_PID" "$OUT_DIR/logs/mock.log"

LUMEWARP_CONFIG="$OUT_DIR/configs/lumewarp.yaml"
LUMEWARP_SED=(
  -e "s|127\.0\.0\.1:9000|127.0.0.1:${MOCK_PORT}|g"
  -e "s|127\.0\.0\.1:4000|127.0.0.1:${LUMEWARP_PORT}|g"
  -e "/^  listen:/a\\  admin_listen: 127.0.0.1:0"
)
if (( AUTH )); then
  hash_list="$(printf '"%s", ' "${KEY_HASHES[@]}")"
  hash_list="${hash_list%, }"
  LUMEWARP_SED+=(-e "s|api_keys: \[\] # __BENCH_API_KEYS__|api_keys: [${hash_list}] # __BENCH_API_KEYS__|")
fi
sed "${LUMEWARP_SED[@]}" "$BENCH_DIR/lumewarp/config.yaml" >"$LUMEWARP_CONFIG"
grep -q "127.0.0.1:${MOCK_PORT}/v1" "$LUMEWARP_CONFIG" || die "failed to template mock port into $LUMEWARP_CONFIG"
grep -q "listen: 127.0.0.1:${LUMEWARP_PORT}" "$LUMEWARP_CONFIG" || die "failed to template lumewarp port into $LUMEWARP_CONFIG"
if (( AUTH )); then
  for hash in "${KEY_HASHES[@]}"; do
    grep -q "\"${hash}\"" "$LUMEWARP_CONFIG" || die "failed to template server.api_keys into $LUMEWARP_CONFIG"
  done
else
  grep -q "^  api_keys: \[\]" "$LUMEWARP_CONFIG" || die "server.api_keys should stay empty without BENCH_AUTH=1"
fi

log "starting lumewarp on 127.0.0.1:$LUMEWARP_PORT"
"$LUMEWARP_BIN" --config "$LUMEWARP_CONFIG" >"$OUT_DIR/logs/lumewarp.log" 2>&1 &
LUMEWARP_PID=$!
SERVER_PIDS+=("$LUMEWARP_PID")
wait_for_http "lumewarp" "http://127.0.0.1:$LUMEWARP_PORT/healthz" "$HEALTH_TIMEOUT_SECS" "$LUMEWARP_PID" "$OUT_DIR/logs/lumewarp.log"

LITELLM_CONFIG="$OUT_DIR/configs/litellm.yaml"
LITELLM_SED=(-e "s|127\.0\.0\.1:9000/v1|127.0.0.1:${MOCK_PORT}/v1|g")
if (( AUTH )); then
  LITELLM_SED+=(
    -e "s|^  # master_key: __BENCH_MASTER_KEY__$|  master_key: \"${CLIENT_KEYS[0]}\"|"
    -e "s|^  dangerously_permit_weak_or_unset_master_key: true$|  # BENCH_AUTH=1: master_key is set, so the weak-key escape hatch is removed.|"
  )
fi
sed "${LITELLM_SED[@]}" "$BENCH_DIR/litellm/config.yaml" >"$LITELLM_CONFIG"
grep -q "127.0.0.1:${MOCK_PORT}/v1" "$LITELLM_CONFIG" || die "failed to template mock port into $LITELLM_CONFIG"
if (( AUTH )); then
  grep -qx "  master_key: \"${CLIENT_KEYS[0]}\"" "$LITELLM_CONFIG" || die "failed to template master_key into $LITELLM_CONFIG"
  ! grep -q "^  dangerously_permit_weak_or_unset_master_key:" "$LITELLM_CONFIG" ||
    die "BENCH_AUTH=1 must remove dangerously_permit_weak_or_unset_master_key from $LITELLM_CONFIG"
else
  ! grep -q "^  master_key:" "$LITELLM_CONFIG" || die "master_key must stay unset without BENCH_AUTH=1"
fi

log "starting LiteLLM ${LITELLM_VERSION} on 127.0.0.1:$LITELLM_PORT with ${LITELLM_WORKERS} workers"
env \
  LITELLM_LOG=ERROR \
  LITELLM_DONT_SHOW_FEEDBACK_BOX=True \
  LITELLM_LOCAL_MODEL_COST_MAP=True \
  DISABLE_SCHEMA_UPDATE=True \
  uvx --from "litellm[proxy]==${LITELLM_VERSION}" litellm \
  --config "$LITELLM_CONFIG" \
  --host 127.0.0.1 \
  --port "$LITELLM_PORT" \
  --num_workers "$LITELLM_WORKERS" \
  >"$OUT_DIR/logs/litellm.log" 2>&1 &
LITELLM_PID=$!
SERVER_PIDS+=("$LITELLM_PID")
LITELLM_HEALTH_ARGS=()
(( AUTH )) && LITELLM_HEALTH_ARGS+=(-H "Authorization: Bearer ${CLIENT_KEYS[0]}")
wait_for_http "litellm" "http://127.0.0.1:$LITELLM_PORT/health" "$HEALTH_TIMEOUT_SECS" "$LITELLM_PID" "$OUT_DIR/logs/litellm.log" "${LITELLM_HEALTH_ARGS[@]}"

log "settling for ${SETTLE_SECS}s"
sleep "$SETTLE_SECS"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

base_url() {
  case "$1" in
    direct) printf 'http://127.0.0.1:%s' "$MOCK_PORT" ;;
    lumewarp) printf 'http://127.0.0.1:%s' "$LUMEWARP_PORT" ;;
    litellm) printf 'http://127.0.0.1:%s' "$LITELLM_PORT" ;;
  esac
}

server_pid() {
  case "$1" in
    direct) printf '%s' "$MOCK_PID" ;;
    lumewarp) printf '%s' "$LUMEWARP_PID" ;;
    litellm) printf '%s' "$LITELLM_PID" ;;
  esac
}

ensure_alive() {
  local target="$1" pid logfile
  pid="$(server_pid "$target")"
  logfile="$OUT_DIR/logs/$([[ "$target" == direct ]] && echo mock || echo "$target").log"
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 40 "$logfile" >&2 2>/dev/null || true
    die "target $target exited during the run (see $logfile)"
  fi
}

start_sampler() {
  local pid="$1" out="$2" sampler
  python3 -I "$SCRIPT_DIR/proc_sampler.py" --pid "$pid" --interval 1 --out "$out" \
    >>"$OUT_DIR/logs/sampler.log" 2>&1 &
  sampler=$!
  sleep 0.2
  if kill -0 "$sampler" 2>/dev/null; then
    SAMPLER_PIDS+=("$sampler")
  else
    log "warning: /proc sampler failed for pid $pid; $(basename "$out") will be missing"
  fi
}

# Keys a target's users send: all keys on lumewarp, LiteLLM's master key on
# litellm, none on the open direct baseline.
keys_for() {
  (( AUTH )) || return 0
  case "$1" in
    lumewarp) local IFS=,; printf '%s' "${CLIENT_KEYS[*]}" ;;
    litellm) printf '%s' "${CLIENT_KEYS[0]}" ;;
  esac
}

append_manifest() {
  printf '%s\n' "$1" >>"$OUT_DIR/scenarios.jsonl"
}

# run_locust <target> <scenario> <rep> <stages> <measure_after> <measured_secs> <offered_rps>
run_locust() {
  local target="$1" scenario="$2" rep="$3" stages="$4" measure_after="$5" measured="$6" offered="$7"
  local rel="$scenario/$target.rep$rep"
  local dir="$OUT_DIR/$rel" status=0 negative=0 bad_key=0
  mkdir -p "$dir/samples"
  [[ "$target" != "direct" ]] && negative=1
  (( AUTH )) && [[ "$target" != "direct" ]] && bad_key=1

  ensure_alive "$target"
  log "  $scenario: $target (stages ${stages}, rep ${rep}/${REPEAT})"
  start_sampler "$(server_pid "$target")" "$dir/server.cpu.csv"
  env \
    LW_TARGET="$target" \
    LW_MODEL="$LW_MODEL" \
    LW_ACCEPT_MODELS="$LW_ACCEPT_MODELS" \
    LW_MAX_TOKENS="$LW_MAX_TOKENS" \
    LW_API_KEYS="$(keys_for "$target")" \
    LW_WEIGHTS="$LOCUST_WEIGHTS" \
    LW_STREAM_USAGE_SHARE="$STREAM_USAGE_SHARE" \
    LW_NEGATIVE="$negative" \
    LW_NEGATIVE_BAD_KEY="$bad_key" \
    LW_USER_RPS="$USER_RPS" \
    LW_STAGES="$stages" \
    LW_MEASURE_AFTER_SECS="$measure_after" \
    LW_SAMPLES_DIR="$dir/samples" \
    LW_DUMP_DIR="$dir/samples" \
    "${LOCUST_CMD[@]}" \
    -f "$LOCUSTFILE" \
    --headless \
    --host "$(base_url "$target")" \
    --processes "$LOCUST_PROCESSES" \
    --master-bind-host 127.0.0.1 \
    --master-bind-port "$LOCUST_MASTER_PORT" \
    --master-host 127.0.0.1 \
    --master-port "$LOCUST_MASTER_PORT" \
    --csv "$dir/locust" \
    --csv-full-history \
    --html "$dir/report.html" \
    --stop-timeout 15 \
    --exit-code-on-error 0 \
    --only-summary \
    --loglevel WARNING \
    >"$dir/locust.log" 2>&1 &
  LOAD_PID=$!
  # The generator tree is sampled too: its CPU decides whether the run is
  # generator-bound.
  start_sampler "$LOAD_PID" "$dir/generator.cpu.csv"
  wait "$LOAD_PID" || status=$?
  LOAD_PID=""
  stop_samplers
  if (( status != 0 )) || [[ ! -s "$dir/locust_stats.csv" ]]; then
    tail -n 40 "$dir/locust.log" >&2 || true
    die "locust failed (exit $status): $scenario $target rep $rep (see $dir/locust.log)"
  fi
  # The master can exit before its workers finish writing their dumps (a
  # saturated target leaves requests in flight), so wait for one per process.
  local dumps=0 deadline=$((SECONDS + 30))
  while :; do
    dumps="$({ compgen -G "$dir/samples/samples-*.json" || true; } | wc -l)"
    (( dumps >= LOCUST_PROCESSES || SECONDS >= deadline )) && break
    sleep 0.2
  done
  (( dumps > 0 )) ||
    die "locust wrote no sample dumps: $scenario $target rep $rep (see $dir/locust.log)"
  (( dumps >= LOCUST_PROCESSES )) ||
    die "locust wrote $dumps of $LOCUST_PROCESSES sample dumps: $scenario $target rep $rep (see $dir/locust.log)"
  ensure_alive "$target"

  append_manifest "{\"target\":\"${target}\",\"scenario\":\"${scenario}\",\"repetition\":${rep},\"stages\":\"${stages}\",\"measure_after_secs\":${measure_after},\"measured_secs\":${measured},\"offered_rps\":${offered},\"dir\":\"${rel}\"}"
  sleep 2
}

# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

# Each entry: name|stages|measure_after|measured_secs|offered_rps
SCENARIOS=()
if [[ "$PROFILE" == "quick" ]]; then
  stages="" total_users_secs=0 total_secs=0
  for users in "${QUICK_STEP_LIST[@]}"; do
    stages+="${users}:${QUICK_STEP_SECS},"
    total_users_secs=$((total_users_secs + users * QUICK_STEP_SECS))
    total_secs=$((total_secs + QUICK_STEP_SECS))
  done
  offered="$(python3 -I -c 'import sys; print(round(float(sys.argv[1]) / float(sys.argv[2]) * float(sys.argv[3]), 1))' "$total_users_secs" "$total_secs" "$USER_RPS")"
  SCENARIOS+=("step|${stages%,}|0|${total_secs}|${offered}")
else
  for rate in "${RATE_LIST[@]}"; do
    users="$(users_for_rate "$rate")"
    SCENARIOS+=("rate-${rate}|${users}:$((WARMUP_SECS + STAGE_SECS))|${WARMUP_SECS}|${STAGE_SECS}|${rate}")
  done
  if (( SOAK_SECS > 0 )); then
    users="$(users_for_rate "$SOAK_RATE")"
    SCENARIOS+=("soak|${users}:$((WARMUP_SECS + SOAK_SECS))|${WARMUP_SECS}|${SOAK_SECS}|${SOAK_RATE}")
  fi
fi

TARGETS=(direct lumewarp litellm)
rotated_targets() {
  local offset="$1" i
  for (( i = 0; i < ${#TARGETS[@]}; i++ )); do
    printf '%s\n' "${TARGETS[$(((offset + i) % ${#TARGETS[@]}))]}"
  done
}

for (( rep = 1; rep <= REPEAT; rep++ )); do
  mapfile -t target_order < <(rotated_targets $((rep - 1)))
  log "repetition ${rep}/${REPEAT} (target order: ${target_order[*]})"
  for entry in "${SCENARIOS[@]}"; do
    IFS='|' read -r name stages measure_after measured offered <<< "$entry"
    for target in "${target_order[@]}"; do
      run_locust "$target" "$name" "$rep" "$stages" "$measure_after" "$measured" "$offered"
    done
  done
done

# --------------------------------------------------------------------------- #
# Metadata and aggregation
# --------------------------------------------------------------------------- #

python3 -I - "$OUT_DIR/meta.json" "$LW_ACCEPT_MODELS" "${QUICK_STEP_LIST[*]}" "$STREAM_USAGE_SHARE" "$LOCUST_WEIGHTS" <<EOF
import json, os, platform, subprocess, sys
def run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15, cwd="$REPO_ROOT").stdout.strip()
    except OSError:
        return ""
cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "unknown")
meta = {
    "run_id": "$RUN_ID",
    "profile": "$PROFILE",
    "auth": "$AUTH_MODE",
    "client_keys": $( (( AUTH )) && echo "$LOCUST_KEYS" || echo 0 ),
    "started_at": "$STARTED_AT",
    "machine": {"nproc": len(os.sched_getaffinity(0)), "cpu_model": cpu, "kernel": platform.release()},
    "git": {"describe": run("git", "describe", "--always", "--dirty")},
    "versions": {"locust": "$locust_version", "litellm": "$LITELLM_VERSION",
                 "geventhttpclient": "$GEVENTHTTPCLIENT_VERSION"},
    "config": {
        "ports": {"mock": $MOCK_PORT, "lumewarp": $LUMEWARP_PORT, "litellm": $LITELLM_PORT},
        "mock_latency_ms": $MOCK_LATENCY_MS, "mock_stream_chunks": $MOCK_STREAM_CHUNKS,
        "mock_chunk_interval_ms": $MOCK_CHUNK_INTERVAL_MS, "mock_headers_first": $MOCK_HEADERS_FIRST,
        "litellm_workers": $LITELLM_WORKERS, "locust_processes": $LOCUST_PROCESSES,
        "model": "$LW_MODEL", "accept_models": sys.argv[2], "max_tokens": $LW_MAX_TOKENS,
        "step_offered": [int(users) * float("$USER_RPS") for users in sys.argv[3].split()] if "$PROFILE" == "quick" else [],
        "user_rps": $USER_RPS, "weights": sys.argv[5], "stream_usage_share": float(sys.argv[4]),
        "repetitions": $REPEAT, "gen_cpu_limit_pct": $GEN_CPU_LIMIT_PCT,
    },
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(meta, fh, indent=2)
    fh.write("\n")
EOF

log "aggregating"
python3 -I "$SCRIPT_DIR/aggregate_locust.py" --run-dir "$OUT_DIR"
log "done: $OUT_DIR/summary.md"

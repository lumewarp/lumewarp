#!/usr/bin/env bash
#
# Benchmark Lumewarp against LiteLLM on this machine.
#
# For each target (mock upstream directly, Lumewarp, LiteLLM) the script runs
# the same offered load: fixed-rate non-streaming chat completions with oha and
# concurrent SSE clients with sse-bench, while sampling CPU/PSS from /proc.
# Results go to bench/results/<UTC timestamp>/ (results.json + summary.md).
#
# Overhead = latency through a gateway - latency direct to the mock upstream at
# the same offered load. Saturation is recorded and flagged, never fatal.
#
# Usage: bench/scripts/run.sh [--quick|--full] [--no-build] [-h|--help]
#
# Exit codes: 0 for a completed run (even with saturation flags),
#             2 for infrastructure failure (missing tool/binary, port in use,
#             server failed to start or died mid-run).
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd -- "$BENCH_DIR/.." && pwd)"

log() { printf '[bench] %s\n' "$*" >&2; }
die() {
  printf '[bench] ERROR: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Benchmark Lumewarp vs LiteLLM vs the mock upstream directly.

Usage: bench/scripts/run.sh [--quick|--full] [--no-build] [-h|--help]

  --quick     1k RPS non-stream (15 s) + stream at concurrency 100 (default)
  --full      300/1000/5000/10000 RPS non-stream (30 s + 5 s warmup) +
              stream at concurrency 100 and 1000 (30 s each)
  --no-build  do not run `cargo build --release` for mock/gateway

Each scenario is repeated REPEAT times (1 for --quick, 3 for --full), with the
per-repetition target order rotated; results report medians.

Environment overrides (defaults):
  MOCK_PORT=9000              Lumewarp-facing mock upstream port
  LUMEWARP_PORT=4000          Lumewarp listen port
  LITELLM_PORT=4001           LiteLLM listen port
  MOCK_LATENCY_MS=50          mock upstream fixed latency
  MOCK_STREAM_CHUNKS=20       mock SSE chunks per response
  MOCK_CHUNK_INTERVAL_MS=10   delay between mock SSE chunks
  MOCK_HEADERS_FIRST=0        pass --headers-first to the mock (SSE headers
                              before the prefill delay, like vLLM/OpenAI)
  BENCH_AUTH=0                client auth: 1 generates one random key per run,
                              configures it on both gateways and sends it as a
                              bearer (the direct mock target stays open)
  BENCH_OTEL=0                1 exports Lumewarp's traces to a local OTLP sink on
                              OTLP_SINK_PORT (default 4318), at 100% sampling with
                              a queue sized not to drop; report it separately
  BENCH_REWRITE=0             1 adds the target lumewarp-rewrite: the same gateway
                              called as mock-renamed, so every response goes through
                              the model scanner; reported against the lumewarp target
                              (the fast path) in the "Response rewrite" section
  BENCH_EMBED_BYTES=0         >0 adds non-stream POST /v1/embeddings with a response of
                              at least this many bytes (model after data), on direct
                              and the lumewarp targets only
  EMBED_RATES="50"            offered RPS of the embeddings scenario
  BENCH_LITELLM=1             0 skips LiteLLM (no A6 ratio; for rewrite-only runs)
  LITELLM_VERSION=1.106.2     litellm[proxy] pin (must match bench/litellm/config.yaml)
  LITELLM_WORKERS=$(nproc)    LiteLLM --num_workers
  DURATION_SECS               measured duration per scenario (profile default)
  WARMUP_SECS                 non-stream warmup per scenario (full: 5, quick: 0)
  RATES=<profile default>     offered non-stream RPS (full: 300 1000 5000 10000)
  STREAM_CONCURRENCIES="100"  stream concurrency levels
  REPEAT=<profile>            repetitions per scenario (quick 1, full 3)
  SETTLE_SECS=10              idle time after all servers are healthy
  HEALTH_TIMEOUT_SECS=180     per-server health check timeout
  RESULTS_DIR=bench/results   output root
  RUN_ID=<UTC timestamp>      output directory name
  MOCK_BIN / SSE_BENCH_BIN / LUMEWARP_BIN   override binary paths
EOF
}

PROFILE=quick
BUILD=1
while (($# > 0)); do
  case "$1" in
    --quick)
      PROFILE=quick
      ;;
    --full)
      PROFILE=full
      ;;
    --no-build)
      BUILD=0
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (try --help)"
      ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
# Knobs
# --------------------------------------------------------------------------- #

MOCK_PORT="${MOCK_PORT:-9000}"
LUMEWARP_PORT="${LUMEWARP_PORT:-4000}"
LITELLM_PORT="${LITELLM_PORT:-4001}"
MOCK_LATENCY_MS="${MOCK_LATENCY_MS:-50}"
MOCK_STREAM_CHUNKS="${MOCK_STREAM_CHUNKS:-20}"
MOCK_CHUNK_INTERVAL_MS="${MOCK_CHUNK_INTERVAL_MS:-10}"
MOCK_HEADERS_FIRST="${MOCK_HEADERS_FIRST:-0}"
BENCH_AUTH="${BENCH_AUTH:-0}"
BENCH_OTEL="${BENCH_OTEL:-0}"
OTLP_SINK_PORT="${OTLP_SINK_PORT:-4318}"
BENCH_REWRITE="${BENCH_REWRITE:-0}"
BENCH_EMBED_BYTES="${BENCH_EMBED_BYTES:-0}"
EMBED_RATES="${EMBED_RATES:-50}"
BENCH_LITELLM="${BENCH_LITELLM:-1}"
LITELLM_VERSION="${LITELLM_VERSION:-1.106.2}"
LITELLM_WORKERS="${LITELLM_WORKERS:-$(nproc)}"
HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-180}"
SETTLE_SECS="${SETTLE_SECS:-10}"
RESULTS_DIR="${RESULTS_DIR:-$BENCH_DIR/results}"

MOCK_BIN="${MOCK_BIN:-$BENCH_DIR/mock-upstream/target/release/mock-upstream}"
SSE_BENCH_BIN="${SSE_BENCH_BIN:-$BENCH_DIR/mock-upstream/target/release/sse-bench}"
LUMEWARP_BIN="${LUMEWARP_BIN:-$REPO_ROOT/gateway/target/release/lumewarp}"

if [[ "$PROFILE" == "quick" ]]; then
  DEFAULT_RATES="1000"
  DEFAULT_DURATION=15
  DEFAULT_WARMUP=0
  DEFAULT_REPEAT=1
  DEFAULT_STREAM_CONCURRENCIES="100"
else
  DEFAULT_RATES="300 1000 5000 10000"
  DEFAULT_DURATION=30
  DEFAULT_WARMUP=5
  DEFAULT_REPEAT=3
  DEFAULT_STREAM_CONCURRENCIES="100 1000"
fi
read -r -a RATE_LIST <<< "${RATES:-$DEFAULT_RATES}"
read -r -a STREAM_CONCURRENCY_LIST <<< "${STREAM_CONCURRENCIES:-$DEFAULT_STREAM_CONCURRENCIES}"
DURATION_SECS="${DURATION_SECS:-$DEFAULT_DURATION}"
WARMUP_SECS="${WARMUP_SECS:-$DEFAULT_WARMUP}"
REPEAT="${REPEAT:-$DEFAULT_REPEAT}"

(( ${#RATE_LIST[@]} > 0 )) || die "no non-stream rates configured"
(( ${#STREAM_CONCURRENCY_LIST[@]} > 0 )) || die "no stream concurrency levels configured"
for rate in "${RATE_LIST[@]}"; do
  [[ "$rate" =~ ^[1-9][0-9]*$ ]] || die "invalid non-stream rate: $rate (positive integer required)"
done
for concurrency in "${STREAM_CONCURRENCY_LIST[@]}"; do
  [[ "$concurrency" =~ ^[1-9][0-9]*$ ]] || die "invalid stream concurrency: $concurrency (positive integer required)"
done
(( DURATION_SECS > 0 )) || die "DURATION_SECS must be > 0"
(( MOCK_LATENCY_MS >= 0 )) || die "MOCK_LATENCY_MS must be >= 0"
[[ "$REPEAT" =~ ^[1-9][0-9]*$ ]] || die "REPEAT must be a positive integer"
[[ "$MOCK_HEADERS_FIRST" == "0" || "$MOCK_HEADERS_FIRST" == "1" ]] ||
  die "MOCK_HEADERS_FIRST must be 0 or 1"
case "$BENCH_AUTH" in
  0) AUTH=0 ;;
  1) AUTH=1 ;;
  *) die "BENCH_AUTH must be 0 or 1 (got: $BENCH_AUTH)" ;;
esac
case "$BENCH_OTEL" in
  0|1) ;;
  *) die "BENCH_OTEL must be 0 or 1 (got: $BENCH_OTEL)" ;;
esac
case "$BENCH_REWRITE" in
  0|1) ;;
  *) die "BENCH_REWRITE must be 0 or 1 (got: $BENCH_REWRITE)" ;;
esac
case "$BENCH_LITELLM" in
  0|1) ;;
  *) die "BENCH_LITELLM must be 0 or 1 (got: $BENCH_LITELLM)" ;;
esac
[[ "$BENCH_EMBED_BYTES" =~ ^[0-9]+$ ]] || die "BENCH_EMBED_BYTES must be a byte count"
read -r -a EMBED_RATE_LIST <<< "$EMBED_RATES"
for rate in "${EMBED_RATE_LIST[@]}"; do
  [[ "$rate" =~ ^[1-9][0-9]*$ ]] || die "invalid embeddings rate: $rate (positive integer required)"
done
AUTH_MODE="off"
BENCH_API_KEY=""
BENCH_API_KEY_SHA256=""
if (( AUTH )); then
  AUTH_MODE="on"
fi

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid RUN_ID: $RUN_ID"

REQUEST_BODY='{"model":"mock","messages":[{"role":"user","content":"benchmark"}],"max_tokens":16,"temperature":0}'
EMBED_BODY='{"model":"mock","input":"benchmark"}'
MODEL="mock"
# Routes to the same pool as `mock`; only the called name differs from upstream_model.
REWRITE_MODEL="mock-renamed"

# --------------------------------------------------------------------------- #
# Process-tree helpers (LiteLLM forks workers, uvx wraps the real server)
# --------------------------------------------------------------------------- #

tree_pids() {
  local root="$1"
  local -a queue=("$root") out=()
  local -A seen=()
  local pid child ppid stat_file
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
        local raw
        raw="$(<"$stat_file")" || continue
        # Drop the pid and the parenthesised comm, then fields are
        # "state ppid ...". cut on the *last* ')' (comm may contain spaces).
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

for tool in oha curl python3 uvx nproc; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done
if (( BUILD )); then
  command -v cargo >/dev/null 2>&1 || die "missing required tool: cargo (or pass --no-build)"
fi

if (( BUILD )); then
  log "building mock-upstream + sse-bench (release)"
  (cd "$BENCH_DIR/mock-upstream" && cargo build --release)
  log "building lumewarp (release)"
  (cd "$REPO_ROOT/gateway" && cargo build --release)
fi

[[ -x "$MOCK_BIN" ]] || die "mock-upstream binary not found: $MOCK_BIN"
[[ -x "$SSE_BENCH_BIN" ]] || die "sse-bench binary not found: $SSE_BENCH_BIN"
[[ -x "$LUMEWARP_BIN" ]] || die "lumewarp binary not found: $LUMEWARP_BIN"

# --------------------------------------------------------------------------- #
# Client auth (BENCH_AUTH=1)
# --------------------------------------------------------------------------- #

# One random key per run, never written to meta.json/logs. The gateway only
# gets its SHA-256 (server.api_keys); LiteLLM gets the raw key as master_key
# and the load generators send it as a bearer. The direct mock target is the
# unauthenticated baseline and never sends it.
if (( AUTH )); then
  read -r BENCH_API_KEY BENCH_API_KEY_SHA256 < <(
    python3 -I -c 'import hashlib, secrets; key = secrets.token_hex(32); print(key, hashlib.sha256(key.encode()).hexdigest())'
  )
  [[ "$BENCH_API_KEY" =~ ^[0-9a-f]{64}$ ]] || die "failed to generate a bench API key"
  [[ "$BENCH_API_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "failed to hash the bench API key"
  log "client auth: on (per-run bearer key, sha256 ${BENCH_API_KEY_SHA256:0:12}…; direct target stays open)"
else
  log "client auth: off"
fi

fd_limit="$(ulimit -n)"
if (( fd_limit < 4096 )); then
  log "warning: open-file limit is ${fd_limit}; high-rate scenarios may be capped by connections"
fi

log "oha: $(oha --version 2>/dev/null | head -n1)"

# --------------------------------------------------------------------------- #
# Run directory
# --------------------------------------------------------------------------- #

mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd -- "$RESULTS_DIR" && pwd)"
RUN_DIR="$RESULTS_DIR/$RUN_ID"
suffix=1
while [[ -e "$RUN_DIR" ]]; do
  RUN_DIR="$RESULTS_DIR/${RUN_ID}-${suffix}"
  suffix=$((suffix + 1))
done
RUN_ID="$(basename "$RUN_DIR")"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/configs" "$RUN_DIR/nonstream" "$RUN_DIR/stream"
: > "$RUN_DIR/scenarios.jsonl"

log "run directory: $RUN_DIR"
log "profile: $PROFILE, auth: $AUTH_MODE, rewrite: $BENCH_REWRITE, embeddings bytes: $BENCH_EMBED_BYTES, litellm: $BENCH_LITELLM, repetitions: $REPEAT"

# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #

SERVER_PIDS=()
SAMPLER_PID=""
LOAD_PID=""
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$LOAD_PID" ]] && kill -0 "$LOAD_PID" 2>/dev/null; then
    kill_tree "$LOAD_PID" TERM
  fi
  if [[ -n "$SAMPLER_PID" ]] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
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
# Start servers
# --------------------------------------------------------------------------- #

require_port_free() {
  local port="$1"
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    die "port $port is already in use; free it or override MOCK_PORT/LUMEWARP_PORT/LITELLM_PORT"
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
    # Extra args are curl options (e.g. an Authorization header when
    # BENCH_AUTH=1 makes LiteLLM's /health require the master key).
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
if (( BENCH_LITELLM )); then
  require_port_free "$LITELLM_PORT"
fi

# Mock upstream.
MOCK_EXTRA_ARGS=()
MOCK_MODE=""
if [[ "$MOCK_HEADERS_FIRST" == "1" ]]; then
  MOCK_EXTRA_ARGS+=(--headers-first)
  MOCK_MODE=", headers-first"
fi
if (( BENCH_EMBED_BYTES > 0 )); then
  MOCK_EXTRA_ARGS+=(--large-embedding-bytes "$BENCH_EMBED_BYTES")
  MOCK_MODE+=", embeddings ${BENCH_EMBED_BYTES} bytes"
fi
log "starting mock upstream on 127.0.0.1:$MOCK_PORT (latency ${MOCK_LATENCY_MS}ms, ${MOCK_STREAM_CHUNKS} chunks x ${MOCK_CHUNK_INTERVAL_MS}ms${MOCK_MODE})"
"$MOCK_BIN" \
  --listen "127.0.0.1:$MOCK_PORT" \
  --latency-ms "$MOCK_LATENCY_MS" \
  --stream-chunks "$MOCK_STREAM_CHUNKS" \
  --chunk-interval-ms "$MOCK_CHUNK_INTERVAL_MS" \
  ${MOCK_EXTRA_ARGS[@]+"${MOCK_EXTRA_ARGS[@]}"} \
  >"$RUN_DIR/logs/mock.log" 2>&1 &
MOCK_PID=$!
SERVER_PIDS+=("$MOCK_PID")
wait_for_http "mock upstream" "http://127.0.0.1:$MOCK_PORT/health" "$HEALTH_TIMEOUT_SECS" "$MOCK_PID" "$RUN_DIR/logs/mock.log"

# Lumewarp (configs are templated so ports can be overridden).
LUMEWARP_CONFIG="$RUN_DIR/configs/lumewarp.yaml"
LUMEWARP_SED=(
  -e "s|127\.0\.0\.1:9000|127.0.0.1:${MOCK_PORT}|g"
  -e "s|127\.0\.0\.1:4000|127.0.0.1:${LUMEWARP_PORT}|g"
  -e "/^  listen:/a\\  admin_listen: 127.0.0.1:0"
)
if (( AUTH )); then
  LUMEWARP_SED+=(
    -e "s|api_keys: \[\] # __BENCH_API_KEYS__|api_keys: [\"${BENCH_API_KEY_SHA256}\"] # __BENCH_API_KEYS__|"
  )
fi
sed "${LUMEWARP_SED[@]}" "$BENCH_DIR/lumewarp/config.yaml" >"$LUMEWARP_CONFIG"
grep -q "127.0.0.1:${MOCK_PORT}/v1" "$LUMEWARP_CONFIG" ||
  die "failed to template mock port into $LUMEWARP_CONFIG"
grep -q "listen: 127.0.0.1:${LUMEWARP_PORT}" "$LUMEWARP_CONFIG" ||
  die "failed to template lumewarp port into $LUMEWARP_CONFIG"
if (( AUTH )); then
  grep -q "api_keys: \[\"${BENCH_API_KEY_SHA256}\"\]" "$LUMEWARP_CONFIG" ||
    die "failed to template server.api_keys into $LUMEWARP_CONFIG"
else
  grep -q "^  api_keys: \[\]" "$LUMEWARP_CONFIG" ||
    die "server.api_keys should stay empty without BENCH_AUTH=1 in $LUMEWARP_CONFIG"
fi

LUMEWARP_OTEL_ENV=()
if [[ "$BENCH_OTEL" == 1 ]]; then
  require_port_free "$OTLP_SINK_PORT"
  log "starting OTLP sink on 127.0.0.1:$OTLP_SINK_PORT"
  # Replies 200 to any POST and keeps a running count, so a run shows spans arrived.
  python3 -I -c '
import http.server, pathlib, sys
stats = pathlib.Path(sys.argv[2])
requests = size = 0
class Sink(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_POST(self):
        global requests, size
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        requests += 1
        size += len(body)
        stats.write_text(f"export_requests={requests} bytes={size}\n")
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Sink).serve_forever()
' "$OTLP_SINK_PORT" "$RUN_DIR/otlp-sink.txt" >"$RUN_DIR/logs/otlp-sink.log" 2>&1 &
  SERVER_PIDS+=("$!")
  LUMEWARP_OTEL_ENV=(
    "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$OTLP_SINK_PORT"
    "OTEL_BSP_MAX_QUEUE_SIZE=65536"
    "OTEL_BSP_SCHEDULE_DELAY=200"
    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE=2048"
  )
  printf '%s\n' "${LUMEWARP_OTEL_ENV[@]}" >"$RUN_DIR/otel-env.txt"
fi

log "starting lumewarp on 127.0.0.1:$LUMEWARP_PORT"
env ${LUMEWARP_OTEL_ENV[@]+"${LUMEWARP_OTEL_ENV[@]}"} \
  "$LUMEWARP_BIN" --config "$LUMEWARP_CONFIG" >"$RUN_DIR/logs/lumewarp.log" 2>&1 &
LUMEWARP_PID=$!
SERVER_PIDS+=("$LUMEWARP_PID")
wait_for_http "lumewarp" "http://127.0.0.1:$LUMEWARP_PORT/healthz" "$HEALTH_TIMEOUT_SECS" "$LUMEWARP_PID" "$RUN_DIR/logs/lumewarp.log"

# LiteLLM (pinned; uvx caches after the first run).
LITELLM_PID=""
if (( BENCH_LITELLM )); then
  LITELLM_CONFIG="$RUN_DIR/configs/litellm.yaml"
  LITELLM_SED=(
    -e "s|127\.0\.0\.1:9000/v1|127.0.0.1:${MOCK_PORT}/v1|g"
  )
  if (( AUTH )); then
    LITELLM_SED+=(
      -e "s|^  # master_key: __BENCH_MASTER_KEY__$|  master_key: \"${BENCH_API_KEY}\"|"
      -e "s|^  dangerously_permit_weak_or_unset_master_key: true$|  # BENCH_AUTH=1: master_key is set, so the weak-key escape hatch is removed.|"
    )
  fi
  sed "${LITELLM_SED[@]}" "$BENCH_DIR/litellm/config.yaml" >"$LITELLM_CONFIG"
  grep -q "127.0.0.1:${MOCK_PORT}/v1" "$LITELLM_CONFIG" ||
    die "failed to template mock port into $LITELLM_CONFIG"
  if (( AUTH )); then
    grep -qx "  master_key: \"${BENCH_API_KEY}\"" "$LITELLM_CONFIG" ||
      die "failed to template general_settings.master_key into $LITELLM_CONFIG"
    if grep -q "^  dangerously_permit_weak_or_unset_master_key:" "$LITELLM_CONFIG"; then
      die "BENCH_AUTH=1 must remove dangerously_permit_weak_or_unset_master_key from $LITELLM_CONFIG"
    fi
  else
    if grep -q "^  master_key:" "$LITELLM_CONFIG"; then
      die "general_settings.master_key must stay unset without BENCH_AUTH=1 in $LITELLM_CONFIG"
    fi
  fi

  log "starting LiteLLM ${LITELLM_VERSION} on 127.0.0.1:$LITELLM_PORT with ${LITELLM_WORKERS} workers (first run downloads packages)"
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
    >"$RUN_DIR/logs/litellm.log" 2>&1 &
  LITELLM_PID=$!
  SERVER_PIDS+=("$LITELLM_PID")
  LITELLM_HEALTH_ARGS=()
  if (( AUTH )); then
    # /health is behind user_api_key_auth once a master key is configured.
    LITELLM_HEALTH_ARGS+=(-H "Authorization: Bearer ${BENCH_API_KEY}")
  fi
  wait_for_http "litellm" "http://127.0.0.1:$LITELLM_PORT/health" "$HEALTH_TIMEOUT_SECS" "$LITELLM_PID" "$RUN_DIR/logs/litellm.log" "${LITELLM_HEALTH_ARGS[@]}"
fi

# Let LiteLLM's workers finish booting (health is served before every worker is
# ready) and let the whole machine settle before the direct baseline.
log "settling for ${SETTLE_SECS}s"
sleep "$SETTLE_SECS"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

base_url() {
  case "$1" in
    direct) printf 'http://127.0.0.1:%s' "$MOCK_PORT" ;;
    lumewarp | lumewarp-rewrite) printf 'http://127.0.0.1:%s' "$LUMEWARP_PORT" ;;
    litellm) printf 'http://127.0.0.1:%s' "$LITELLM_PORT" ;;
    *) die "unknown target: $1" ;;
  esac
}

server_pid() {
  case "$1" in
    direct) printf '%s' "$MOCK_PID" ;;
    lumewarp | lumewarp-rewrite) printf '%s' "$LUMEWARP_PID" ;;
    litellm) printf '%s' "$LITELLM_PID" ;;
    *) die "unknown target: $1" ;;
  esac
}

logfile_for() {
  case "$1" in
    direct) printf '%s' "$RUN_DIR/logs/mock.log" ;;
    lumewarp-rewrite) printf '%s' "$RUN_DIR/logs/lumewarp.log" ;;
    *) printf '%s' "$RUN_DIR/logs/$1.log" ;;
  esac
}

ensure_alive() {
  local target="$1" pid logfile
  pid="$(server_pid "$target")"
  logfile="$(logfile_for "$target")"
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 40 "$logfile" >&2 2>/dev/null || true
    die "target $target exited during the run (see $logfile)"
  fi
}

connections_for_rate() {
  local rate="$1" conns fd_limit fd_cap
  # Half a second of requests in flight, so the connection count is not the
  # bottleneck (the mock adds ~50 ms, gateways more when saturated).
  conns=$(((rate + 1) / 2))
  (( conns < 64 )) && conns=64
  fd_limit="$(ulimit -n)"
  fd_cap=$((fd_limit - 1024))
  (( fd_cap < 64 )) && fd_cap=$((fd_limit / 2))
  (( fd_cap < 64 )) && fd_cap=64
  (( conns > fd_cap )) && conns=$fd_cap
  printf '%s' "$conns"
}

start_sampler() {
  local pid="$1" out="$2"
  SAMPLER_PID=""
  python3 -I "$SCRIPT_DIR/proc_sampler.py" --pid "$pid" --interval 1 --out "$out" \
    >"$RUN_DIR/logs/sampler.log" 2>&1 &
  SAMPLER_PID=$!
  sleep 0.3
  if ! kill -0 "$SAMPLER_PID" 2>/dev/null; then
    log "warning: /proc sampler failed for pid $pid; CPU/PSS for this scenario will be missing"
    SAMPLER_PID=""
  fi
}

stop_sampler() {
  if [[ -n "$SAMPLER_PID" ]] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
  SAMPLER_PID=""
}

run_load() {
  # Run a load generator in the background but wait for it, so the signal trap
  # can fire immediately (and kill the load generator) instead of waiting for
  # the current oha/sse-bench run to finish.
  local status=0
  "$@" &
  LOAD_PID=$!
  wait "$LOAD_PID" || status=$?
  LOAD_PID=""
  return "$status"
}

# The model a target is called with; the upstream always sees `mock`.
target_model() {
  if [[ "$1" == "lumewarp-rewrite" ]]; then
    printf '%s' "$REWRITE_MODEL"
  else
    printf '%s' "$MODEL"
  fi
}

# `body` with the model a target is called with.
body_for() {
  local target="$1" body="$2" old new
  old="\"model\":\"$MODEL\""
  new="\"model\":\"$(target_model "$target")\""
  printf '%s' "${body/"$old"/"$new"}"
}

run_oha() {
  local target="$1" rate="$2" duration="$3" out="$4" path="${5:-/v1/chat/completions}" body="${6:-$REQUEST_BODY}"
  local conns
  local -a auth_args=()
  conns="$(connections_for_rate "$rate")"
  if (( AUTH )) && [[ "$target" != "direct" ]]; then
    auth_args+=(-H "Authorization: Bearer ${BENCH_API_KEY}")
  fi
  run_load oha \
    --no-tui \
    --output-format json \
    -o "$out" \
    -q "$rate" \
    --latency-correction \
    -z "${duration}s" \
    -c "$conns" \
    -m POST \
    -T application/json \
    -d "$(body_for "$target" "$body")" \
    -t 600s \
    ${auth_args[@]+"${auth_args[@]}"} \
    "$(base_url "$target")$path"
}

run_sse() {
  local target="$1" concurrency="$2" duration="$3" out="$4"
  local -a auth_args=()
  if (( AUTH )) && [[ "$target" != "direct" ]]; then
    auth_args+=(--api-key "$BENCH_API_KEY")
  fi
  run_load "$SSE_BENCH_BIN" \
    --url "$(base_url "$target")" \
    --model "$(target_model "$target")" \
    ${auth_args[@]+"${auth_args[@]}"} \
    --concurrency "$concurrency" \
    --duration-secs "$duration" \
    --out "$out"
}

append_manifest() {
  printf '%s\n' "$1" >>"$RUN_DIR/scenarios.jsonl"
}

# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

run_nonstream() {
  local target="$1" rate="$2" rep="$3"
  local out_dir="$RUN_DIR/nonstream/$target"
  local raw="$out_dir/rate-${rate}.rep${rep}.json"
  local cpu="$out_dir/rate-${rate}.rep${rep}.cpu.csv"
  local warmup="$out_dir/warmup-rate-${rate}.rep${rep}.json"
  local conns
  conns="$(connections_for_rate "$rate")"
  mkdir -p "$out_dir"

  ensure_alive "$target"
  if (( WARMUP_SECS > 0 )); then
    log "  warmup ${WARMUP_SECS}s: nonstream $target @ ${rate} rps (rep ${rep}/${REPEAT})"
    if ! run_oha "$target" "$rate" "$WARMUP_SECS" "$warmup"; then
      die "warmup oha failed: nonstream $target @ ${rate} rps"
    fi
    sleep 1
  fi

  ensure_alive "$target"
  log "  measure ${DURATION_SECS}s: nonstream $target @ ${rate} rps (${conns} connections, rep ${rep}/${REPEAT})"
  start_sampler "$(server_pid "$target")" "$cpu"
  if ! run_oha "$target" "$rate" "$DURATION_SECS" "$raw"; then
    stop_sampler
    die "oha failed: nonstream $target @ ${rate} rps"
  fi
  stop_sampler
  [[ -s "$raw" ]] || die "oha produced no JSON: nonstream $target @ ${rate} rps"
  ensure_alive "$target"

  append_manifest "{\"kind\":\"nonstream\",\"target\":\"${target}\",\"rate\":${rate},\"repetition\":${rep},\"connections\":${conns},\"duration_secs\":${DURATION_SECS},\"raw\":\"nonstream/${target}/rate-${rate}.rep${rep}.json\",\"cpu\":\"nonstream/${target}/rate-${rate}.rep${rep}.cpu.csv\"}"
}

# Large non-stream response. No warmup: one scenario, and few requests per second.
run_embeddings() {
  local target="$1" rate="$2" rep="$3"
  local out_dir="$RUN_DIR/embeddings/$target"
  local raw="$out_dir/rate-${rate}.rep${rep}.json"
  local cpu="$out_dir/rate-${rate}.rep${rep}.cpu.csv"
  local conns
  conns="$(connections_for_rate "$rate")"
  mkdir -p "$out_dir"

  ensure_alive "$target"
  log "  measure ${DURATION_SECS}s: embeddings $target @ ${rate} rps (rep ${rep}/${REPEAT})"
  start_sampler "$(server_pid "$target")" "$cpu"
  if ! run_oha "$target" "$rate" "$DURATION_SECS" "$raw" /v1/embeddings "$EMBED_BODY"; then
    stop_sampler
    die "oha failed: embeddings $target @ ${rate} rps"
  fi
  stop_sampler
  [[ -s "$raw" ]] || die "oha produced no JSON: embeddings $target @ ${rate} rps"
  ensure_alive "$target"

  append_manifest "{\"kind\":\"embeddings\",\"target\":\"${target}\",\"rate\":${rate},\"repetition\":${rep},\"connections\":${conns},\"duration_secs\":${DURATION_SECS},\"raw\":\"embeddings/${target}/rate-${rate}.rep${rep}.json\",\"cpu\":\"embeddings/${target}/rate-${rate}.rep${rep}.cpu.csv\"}"
}

run_stream() {
  local target="$1" concurrency="$2" rep="$3"
  local out_dir="$RUN_DIR/stream/$target"
  local raw="$out_dir/c${concurrency}.rep${rep}.json"
  local cpu="$out_dir/c${concurrency}.rep${rep}.cpu.csv"
  mkdir -p "$out_dir"

  ensure_alive "$target"
  log "  measure ${DURATION_SECS}s: stream $target @ concurrency ${concurrency} (rep ${rep}/${REPEAT})"
  start_sampler "$(server_pid "$target")" "$cpu"
  if ! run_sse "$target" "$concurrency" "$DURATION_SECS" "$raw"; then
    stop_sampler
    die "sse-bench failed: stream $target @ concurrency ${concurrency}"
  fi
  stop_sampler
  [[ -s "$raw" ]] || die "sse-bench produced no JSON: stream $target @ concurrency ${concurrency}"
  ensure_alive "$target"

  append_manifest "{\"kind\":\"stream\",\"target\":\"${target}\",\"concurrency\":${concurrency},\"repetition\":${rep},\"duration_secs\":${DURATION_SECS},\"raw\":\"stream/${target}/c${concurrency}.rep${rep}.json\",\"cpu\":\"stream/${target}/c${concurrency}.rep${rep}.cpu.csv\"}"
}

# Refuses to measure anything if the two gateway targets are not the two paths
# they claim to be: the fast path keeps the upstream's name and its length, the
# rewrite path carries the called name and is chunked.
verify_rewrite_paths() {
  local path="$1" body="$2" min_bytes="$3" target headers out size expected
  local -a auth_args=()
  if (( AUTH )); then
    auth_args+=(-H "Authorization: Bearer ${BENCH_API_KEY}")
  fi
  headers="$RUN_DIR/logs/verify.headers"
  out="$RUN_DIR/logs/verify.body"
  for target in lumewarp lumewarp-rewrite; do
    expected="$(target_model "$target")"
    curl -fsS -m 30 -D "$headers" -o "$out" -H 'content-type: application/json' \
      ${auth_args[@]+"${auth_args[@]}"} \
      -d "$(body_for "$target" "$body")" \
      "$(base_url "$target")$path" || die "$target: request to $path failed"
    size="$(wc -c <"$out")"
    (( size >= min_bytes )) || die "$target: $path answered $size bytes, expected at least $min_bytes"
    python3 -I -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["model"] != sys.argv[2])' \
      "$out" "$expected" || die "$target: $path did not answer with model $expected"
    if [[ "$target" == "lumewarp" ]]; then
      grep -qi '^content-length:' "$headers" || die "fast path lost Content-Length on $path"
    else
      grep -qi '^transfer-encoding: chunked' "$headers" || die "rewrite path is not chunked on $path"
    fi
  done
  log "verified fast path and rewrite path on $path"
}

TARGETS=(direct lumewarp)
if (( BENCH_REWRITE )); then
  TARGETS+=(lumewarp-rewrite)
  verify_rewrite_paths /v1/chat/completions "$REQUEST_BODY" 1
  if (( BENCH_EMBED_BYTES > 0 )); then
    verify_rewrite_paths /v1/embeddings "$EMBED_BODY" "$BENCH_EMBED_BYTES"
  fi
fi
if (( BENCH_LITELLM )); then
  TARGETS+=(litellm)
fi

# Warn once if the fd limit forced fewer connections than "half a second of
# requests in flight".
for rate in "${RATE_LIST[@]}"; do
  wanted=$(((rate + 1) / 2))
  (( wanted < 64 )) && wanted=64
  got="$(connections_for_rate "$rate")"
  if (( got < wanted )); then
    log "warning: connections for ${rate} rps capped at ${got} by open-file limit ${fd_limit}"
  fi
done

# Rotate the target order per repetition so machine drift does not always
# favour the first (direct) or last (litellm) measurement.
rotated_targets() {
  local offset="$1" i
  for (( i = 0; i < ${#TARGETS[@]}; i++ )); do
    printf '%s\n' "${TARGETS[$(((offset + i) % ${#TARGETS[@]}))]}"
  done
}

for (( rep = 1; rep <= REPEAT; rep++ )); do
  mapfile -t target_order < <(rotated_targets $((rep - 1)))
  log "repetition ${rep}/${REPEAT} (target order: ${target_order[*]})"
  log "non-stream: rates [${RATE_LIST[*]}] for ${DURATION_SECS}s each"
  for rate in "${RATE_LIST[@]}"; do
    for target in "${target_order[@]}"; do
      run_nonstream "$target" "$rate" "$rep"
    done
  done
  log "stream: concurrency [${STREAM_CONCURRENCY_LIST[*]}] for ${DURATION_SECS}s each"
  for concurrency in "${STREAM_CONCURRENCY_LIST[@]}"; do
    for target in "${target_order[@]}"; do
      run_stream "$target" "$concurrency" "$rep"
    done
  done
  if (( BENCH_EMBED_BYTES > 0 )); then
    log "embeddings: ${BENCH_EMBED_BYTES} bytes, rates [${EMBED_RATE_LIST[*]}] for ${DURATION_SECS}s each"
    for rate in "${EMBED_RATE_LIST[@]}"; do
      for target in "${target_order[@]}"; do
        # LiteLLM parses every response; this scenario is about the gateway's scanner.
        [[ "$target" == "litellm" ]] && continue
        run_embeddings "$target" "$rate" "$rep"
      done
    done
  fi
done

# --------------------------------------------------------------------------- #
# Metadata and aggregation
# --------------------------------------------------------------------------- #

CONNECTIONS_ARG=""
for rate in "${RATE_LIST[@]}"; do
  CONNECTIONS_ARG+="${rate}:$(connections_for_rate "$rate"),"
done
CONNECTIONS_ARG="${CONNECTIONS_ARG%,}"

log "writing metadata"
python3 -I "$SCRIPT_DIR/write_meta.py" \
  --run-dir "$RUN_DIR" \
  --run-id "$RUN_ID" \
  --profile "$PROFILE" \
  --auth "$AUTH_MODE" \
  --started-at "$STARTED_AT" \
  --repo-root "$REPO_ROOT" \
  --mock-port "$MOCK_PORT" \
  --lumewarp-port "$LUMEWARP_PORT" \
  --litellm-port "$LITELLM_PORT" \
  --mock-latency-ms "$MOCK_LATENCY_MS" \
  --mock-stream-chunks "$MOCK_STREAM_CHUNKS" \
  --mock-chunk-interval-ms "$MOCK_CHUNK_INTERVAL_MS" \
  --litellm-version "$LITELLM_VERSION" \
  --litellm-workers "$LITELLM_WORKERS" \
  --duration-secs "$DURATION_SECS" \
  --warmup-secs "$WARMUP_SECS" \
  --repeat "$REPEAT" \
  --rates "${RATE_LIST[*]}" \
  --stream-concurrencies "${STREAM_CONCURRENCY_LIST[*]}" \
  --connections "$CONNECTIONS_ARG" \
  --rewrite "$BENCH_REWRITE" \
  --embed-bytes "$BENCH_EMBED_BYTES" \
  --embed-rates "${EMBED_RATE_LIST[*]}" \
  --request-body "$REQUEST_BODY" \
  --mock-bin "$MOCK_BIN" \
  --sse-bench-bin "$SSE_BENCH_BIN" \
  --lumewarp-bin "$LUMEWARP_BIN"

log "aggregating"
python3 -I "$SCRIPT_DIR/aggregate.py" --run-dir "$RUN_DIR"

log "done: $RUN_DIR/summary.md"

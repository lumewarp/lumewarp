# bench

Compares gateway overhead of Lumewarp vs LiteLLM on the same machine (P0 exit
gate: Lumewarp p99 overhead clearly below LiteLLM's).

- `mock-upstream/` — Rust mock upstream with fixed latency and SSE streaming,
  plus `sse-bench` (concurrent SSE clients).
- `scripts/run.sh` — the benchmark driver: builds release binaries, starts mock /
  Lumewarp / LiteLLM, runs the load, samples CPU/PSS, aggregates results.
- `scripts/ci_check.py` — CI gate over the latest `results.json` (used by the
  `bench` workflow).
- `lumewarp/config.yaml` — standalone gateway config for the bench (open by
  default; `BENCH_AUTH=1` templates a per-run key into `server.api_keys`).
- `litellm/config.yaml` — LiteLLM pointed at the mock upstream, quiet settings,
  pinned version.
- `results/` — run output (gitignored; only `.gitkeep` is tracked).

## What is measured

Three targets on the same machine, all running concurrently, measured in turn
with the same request body:

| target   | listens on | server |
|----------|-----------|--------|
| `direct` | :9000     | mock upstream (baseline) |
| `lumewarp` | :4000   | `gateway/target/release/lumewarp` |
| `litellm` | :4001   | `litellm[proxy]==1.106.2` via `uvx`, `--num_workers $(nproc)` |

- **Non-stream**: fixed-rate `POST /v1/chat/completions`, keep-alive, via `oha`
  with `--latency-correction` (coordinated-omission correction). Rates 1000 RPS
  (`--quick`, 15 s) or 300/1000/5000/10000 RPS (`--full`, 30 s each, plus a
  discarded 5 s warmup). `-c = max(rate × 0.5 s, 64)`, capped by the open-file
  limit, so the connection pool is not the bottleneck. 300 RPS is included
  because LiteLLM can hold it on a typical machine, giving an apples-to-apples
  overhead number. Reported: p50/p90/p99 latency, achieved RPS, error rate,
  CPU avg/max, peak PSS.
- **Stream**: `sse-bench` SSE clients at concurrency 100 (`--quick`) or
  100/1000 (`--full`). Reported: TTFT p50/p99, total p50/p99, successful RPS
  (`ok_rps` when available), errors, CPU avg/max, peak PSS.
- **Repetitions**: each scenario runs `REPEAT` times (1 for `--quick`, 3 for
  `--full`) with the target order rotated per repetition
  (`direct/lumewarp/litellm`, then `lumewarp/litellm/direct`, …). Results report
  medians; overhead p99 also reports min/max across repetitions.

**Overhead** = latency through the gateway − latency direct to the mock upstream,
at the same offered load on the same machine (negative = run-to-run noise). It is
only comparable where **both** direct and the target achieved ≥ 90% of the offered
rate with ≤ 1% errors; summary rows carry a `cmp` column (`yes` / `no`, with
`baseline` for direct) and non-comparable rows are informational only.

`results.json.a6` computes `lumewarp p99 overhead / litellm p99 overhead` and
passes only when: the ratio is ≤ 20%, Lumewarp's p99 overhead is < 1 ms,
Lumewarp is not saturated (median RPS ≥ 90% and no repetition saturated), its
error rate is ≤ 1%, and direct is not saturated. **A6 is judged at 1000 RPS**;
the 300 RPS row (and any other rate) is informational but shown in the same
table with reasons.

By default there is no client auth on either gateway: Lumewarp's bench config
leaves `server.api_keys` empty, and LiteLLM runs without a master key via its
local-only `dangerously_permit_weak_or_unset_master_key` flag. Set
`BENCH_AUTH=1` to measure the authenticated path instead: `run.sh` generates a
random 32-byte key per run, writes its SHA-256 into the templated Lumewarp
`server.api_keys` list, sets LiteLLM's `general_settings.master_key` (and drops
the weak-key escape hatch), and sends `Authorization: Bearer …` from `oha` and
`sse-bench` to both gateways. The direct mock-upstream baseline stays
unauthenticated, and `meta.json` / `summary.md` record `auth: on|off`.

No DB, no spend logging, no retries, no message logging. Do not deploy these
configs.

The mock only has fast paths for model `mock`; the driver always sends
`"model": "mock"` (dynamic model names are serialized per request and would skew
the baseline). Benchmarks must keep using it.

Set `MOCK_HEADERS_FIRST=1` to pass `--headers-first` to the mock: SSE responses
send headers immediately and only then wait `MOCK_LATENCY_MS` before the first
event, matching vLLM/OpenAI prefill-after-headers behavior. This is useful when
validating gateway TTFT/overhead metrics; it does not affect non-stream or
embedding responses. Default off.

## Prerequisites

- Rust toolchain (asdf) for release builds.
- `oha` 1.16 (`oha --version`; uses `--output-format json` and
  `--latency-correction`).
- `uv`/`uvx`; LiteLLM is pinned to `1.106.2` (`LITELLM_VERSION` in `run.sh`,
  documented in `litellm/config.yaml`). First run downloads the wheel.
- `python3` (stdlib only) for `/proc` sampling and aggregation. No `jq` needed.
- Free TCP ports 9000, 4000, 4001 unless overridden.

## Usage

```sh
bench/scripts/run.sh --quick        # ~3 min including LiteLLM startup
bench/scripts/run.sh --full         # 3 reps of 300/1k/5k/10k + stream 100/1000, ~30 min
bench/scripts/run.sh --quick --no-build
BENCH_AUTH=1 bench/scripts/run.sh --quick   # authenticated path (per-run key)
bench/scripts/run.sh --help         # all environment overrides
```

Release binaries are rebuilt with `cargo build --release` on every run unless
`--no-build` is passed (an unchanged build is a fast no-op; this avoids
benchmarking a stale binary). Ports can be moved, e.g. on a box where something
already listens on 9000 (MinIO does, on some dev machines):

```sh
MOCK_PORT=9100 LUMEWARP_PORT=4100 LITELLM_PORT=4101 bench/scripts/run.sh --quick
```

Other useful overrides: `MOCK_LATENCY_MS` (default 50), `MOCK_STREAM_CHUNKS`,
`MOCK_CHUNK_INTERVAL_MS`, `MOCK_HEADERS_FIRST` (see below), `BENCH_AUTH`
(`0`/`1`, default `0`), `LITELLM_WORKERS`,
`LITELLM_VERSION`, `DURATION_SECS`, `WARMUP_SECS`, `RATES`,
`STREAM_CONCURRENCIES`, `REPEAT`, `SETTLE_SECS`, `HEALTH_TIMEOUT_SECS`,
`RESULTS_DIR`, `RUN_ID`, plus `MOCK_BIN`/`SSE_BENCH_BIN`/`LUMEWARP_BIN` for
prebuilt binaries.

Exit code: **0** for a completed run (saturation is recorded, not fatal), **2**
for infrastructure failure (missing tool/binary, port in use, a server that fails
to start or dies mid-run). Ctrl-C kills the whole process tree.

## Saturation flags

A non-stream scenario is flagged when achieved RPS < 90% of the offered rate or
the error rate is > 1%; a stream scenario when its error rate is > 1% (stream
load is concurrency-based, so there is no target RPS). With repetitions, the
summary says how many repetitions were affected. Flags go to `summary.md` and
`results.json.flags` and never abort the run. If `direct` itself is flagged, the
machine or mock could not hold that load and gateway comparisons at that rate are
not meaningful.

`oha`'s end-of-run `aborted due to deadline` requests are reported as
`deadline_aborts` but not counted as errors.

## Output

```
bench/results/<UTC timestamp>/
  meta.json                  machine, git sha, versions, knobs, auth on/off,
                             binary hashes
  results.json               per target/scenario medians + overhead + flags
  summary.md                 markdown tables + machine info
  scenarios.jsonl            manifest consumed by the aggregator
  nonstream/<target>/rate-<N>.rep<M>.json      raw oha JSON
  nonstream/<target>/rate-<N>.rep<M>.cpu.csv   /proc samples (1 Hz, PSS)
  stream/<target>/c<N>.rep<M>.json             raw sse-bench JSON
  stream/<target>/c<N>.rep<M>.cpu.csv
  configs/                   templated configs actually used
  logs/                      server + sampler logs
```

`results.json` layout: `nonstream[rate][target]` and `stream[concurrency][target]`
each contain median `latency_ms`/`ttft_ms`, `rps`, `error_rate`, `repetitions`,
`overhead_ms`, `overhead_p99_min`/`overhead_p99_max`, `overhead_comparable`,
`cpu {avg_pct, max_pct, avg_mem_mb, max_mem_mb}` plus `mem_kind`, `saturated`,
`saturated_repetitions`, `saturation_reasons` and the raw file list. `a6` holds
the per-rate pass/reasons. `mem_kind` is `pss` for new runs (`smaps_rollup`, so
LiteLLM worker pages are not double-counted) and `rss` for old runs sampled
before PSS support.

CPU% is summed over the process tree (LiteLLM forks `--num_workers` processes,
`uvx` wraps the server) and can exceed 100%. Negative CPU deltas (a worker exits
between samples) are clamped to 0.

The aggregation helper can be tested without running a benchmark (includes
multi-repetition medians and an "all 404 must fail A6" case):

```sh
python3 -I bench/scripts/aggregate.py --selftest
```

## Notes

- LiteLLM startup takes ~10–60 s with 20 workers (health check timeout is 180 s
  by default; `SETTLE_SECS` lets workers finish booting).
- The full profile's 5 s warmup is only for non-stream scenarios, and is repeated
  for every repetition.
- Run on an otherwise idle machine; all three servers share the CPU with the
  load generator.
- Benchmark results are local artifacts: `bench/results/*` is gitignored.

## Recorded results

Same machine for every run: 20 × i7-12700, WSL2, idle, mock latency 50 ms,
20 SSE chunks × 10 ms, LiteLLM 1.106.2 with 20 workers, medians of 3 runs.
Overhead p99 is the median with the min–max across runs.

| run | auth | rate / conc | Lumewarp ovh p99 ms | LiteLLM ovh p99 ms |
|---|---|---|---:|---:|
| P0 quick (`1cf38c7`) | off | 1000 RPS | 0.10 [0.08–0.23] | 3882 (saturated) |
| P0 quick | off | stream c=100, TTFT | 0.51 [0.16–0.70] | – |
| P1 full (`2a60f24`) | key | 300 RPS | 0.20 [0.15–0.21] | 40.0 |
| P1 full | key | 1000 RPS | 0.07 [0.00–0.12] | 4288 (saturated) |
| P1 full | key | 5000 RPS | 0.20 [0.12–0.24] | 25128 (saturated) |
| P1 full | key | 10000 RPS | 0.23 [0.08–0.27] | 27581 (saturated, 3% errors) |
| P1 full | key | stream c=100, TTFT | 0.67 [-2.98–0.77] | 518 |
| P1 full | key | stream c=1000, TTFT | 2.30 [2.05–2.40] | 4678 |

P1 streams carry the injected `include_usage` and strip the extra event, since
clients in the bench do not ask for usage. LiteLLM could not hold 1000 RPS on
this machine. Lumewarp used 65 % CPU on average at 10k RPS.

Stream TTFT overhead p99 at c=100 is noisy on this box (single quick runs have
shown 0.5–1.8 ms for the same binary). A same-session A/B with key auth, quick
profile, 5 repetitions: P0 binary (`1cf38c7`) 0.15 ms non-stream / 1.44
[0.03–1.70] ms stream TTFT; P1 HEAD 0.10 ms / 0.74 [0.19–4.48] ms. No regression.

## CI

- The `ci` workflow's `bench-crate` job runs `cargo fmt/clippy/test` for
  `bench/mock-upstream` and `bash -n bench/scripts/run.sh` plus the
  `aggregate.py` and `ci_check.py` self-tests on every PR.
- The `bench` workflow runs `bench/scripts/run.sh --quick` on PRs and pushes to
  `main` that touch `gateway/**`, `bench/**` or the workflow itself. A
  `workflow_dispatch` run can choose `profile` (`quick` or `full`) and `repeat`
  (repetitions; default 1). It uses 4 LiteLLM workers on the 4-vCPU runner,
  raises `ulimit -n`, installs prebuilt, sha256-verified oha 1.16 and caches the
  uv/LiteLLM cache. Dispatch inputs are passed to the scripts through `env:`.
- The run directory is deterministic: `RUN_ID=<github.run_id>-<run_attempt>`,
  so the benchmark, `ci_check.py` and the step summary all use
  `bench/results/${RUN_ID}` (no mtime-based guessing).
- Every run uploads `bench/results/` as an artifact and appends that run's
  `summary.md` to the job summary.
- `bench/scripts/ci_check.py` hard-fails on: a non-zero `run.sh` exit, any
  Lumewarp scenario with > 1% errors or saturation, Lumewarp non-stream
  overhead p99 > 5 ms (when Lumewarp is not saturated), Lumewarp stream TTFT
  overhead p99 > 10 ms (when not saturated), or Lumewarp's non-stream p99
  overhead being worse than LiteLLM's at the A6 rate (1000 RPS).
- The absolute A6 thresholds (p99 < 1 ms, ratio ≤ 20%) remain
  `::warning::`-only annotations: GitHub runners are noisy and A6 is judged on
  an idle machine.
- On runners with ≤ 4 CPUs (GitHub-hosted `ubuntu-latest`), Lumewarp
  saturation and >1% error failures are only hard failures at non-stream rates
  ≤ 1000 RPS. Saturation/errors at 5k/10k RPS become warnings, so a manual
  `profile=full` dispatch can run without being dominated by the small runner's
  capacity. Use a bigger runner (or `--cpus` on a local run) to enforce all
  rates.
- CI is for catching gross regressions, not for certifying A6.

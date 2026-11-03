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

## Response rewrite and large responses

The default targets call `mock`, whose `upstream_model` is also `mock`: the fast
path, which never scans a response. Request-path changes to the rewrite must also
measure the scanner:

- `BENCH_REWRITE=1` adds the target `lumewarp-rewrite`: the same gateway process,
  calling the router `mock-renamed` over the same pool, so every response goes
  through the scanner and is chunked. `summary.md` gets a "Response rewrite" table
  (`results.json.rewrite`): overhead p99 of both paths and the paired per-repetition
  delta. A6 stays judged on `lumewarp`. The driver first checks that the fast path
  answers `mock` with `Content-Length` and the rewrite path answers `mock-renamed`
  chunked, and stops otherwise.
- `BENCH_EMBED_BYTES=<n>` starts the mock with `--large-embedding-bytes` and adds a
  non-stream `POST /v1/embeddings` scenario (`EMBED_RATES`, default 50 RPS) with a
  response of at least `n` bytes, `model` after `data`, so the scanner reads all of
  it. LiteLLM is left out of this one. Keep `n` at or below 4 MiB to include usage
  parsing as well: larger bodies skip it (`MAX_JSON_CAPTURE`, reported INVALID).
- `BENCH_LITELLM=0` skips LiteLLM, for runs that only compare the gateway's two
  paths. A6 then reports a missing LiteLLM result.

```sh
BENCH_AUTH=1 BENCH_REWRITE=1 BENCH_EMBED_BYTES=3145728 \
  MOCK_PORT=9100 LUMEWARP_PORT=4100 LITELLM_PORT=4101 bench/scripts/run.sh --full
```

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
(`0`/`1`, default `0`), `BENCH_REWRITE`, `BENCH_EMBED_BYTES`, `EMBED_RATES`,
`BENCH_LITELLM`, `LITELLM_WORKERS`,
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
  embeddings/<target>/rate-<N>.rep<M>.json     with BENCH_EMBED_BYTES
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

## Locust suite

`bench/scripts/run-locust.sh` is an end-to-end load test on the same setup: the
same mock upstream, the same templated Lumewarp/LiteLLM configs and the same
`BENCH_AUTH` handling, comparing `direct` / `lumewarp` / `litellm`. Where `oha`
and `sse-bench` measure one request shape each, Locust drives a mixed workload
and checks every response.

```sh
bench/scripts/run-locust.sh --quick            # step load, ~2 min per target
bench/scripts/run-locust.sh --full             # 300/1k/5k/10k RPS stages + soak, 3 reps
BENCH_AUTH=1 bench/scripts/run-locust.sh --quick
bench/scripts/run-locust.sh --help             # all overrides
```

- **Tooling**: `locust==2.32.4` (+ `geventhttpclient==2.6.1`) through `uvx`, no
  global install. `FastHttpUser`, headless, `--processes LOCUST_PROCESSES`
  (quick 8, full 12) so the generator is not the bottleneck.
- **Ports** default to `MOCK_PORT=19100 LUMEWARP_PORT=4100 LITELLM_PORT=4101`.
  9000, 5432, 8080 and 8081 are reserved on bench machines; 4000, 9464, 8091,
  50051, 9100 and 55432 are refused because they belong to live services. The
  generated gateway config sets `admin_listen: 127.0.0.1:0`, so the admin
  listener never takes the default 9464.
- **Workload** (`LOCUST_WEIGHTS`, defaults): chat non-stream 55, chat stream 25
  (half with `stream_options.include_usage`, `STREAM_USAGE_SHARE`), completions
  5, embeddings 10 (two inputs), models 5, plus negative 2 on the gateways only:
  unknown model and, with `BENCH_AUTH=1`, a bad key. Negatives are reported as
  `neg_*` and pass on any 4xx; they never count as failures, but a negative the
  gateway accepts is flagged. `LW_MAX_TOKENS` (16) sets the requested output
  limit and `LW_ACCEPT_MODELS` (comma-separated, default `mock`) the response
  model names accepted by the checks.
- **Keys**: with `BENCH_AUTH=1`, `LOCUST_KEYS` (8) random keys per run.
  Lumewarp gets all their SHA-256s and users spread over them. LiteLLM without
  a database only has `general_settings.master_key` (virtual keys need
  Postgres), so it gets the first key and its users all send it. The direct
  target stays open.
- **Checks** on every response: status 200; JSON shape per endpoint
  (`chat.completion`, `text_completion`, embedding list with one numeric vector
  per input, model list containing `mock`); the model is echoed; SSE is
  well-formed (each event is one `data: ` line holding a JSON
  `chat.completion.chunk` or `[DONE]`, `[DONE]` is last); without
  `include_usage` no chunk with a non-null `usage` reaches the client; with it,
  exactly one, as the last chunk.
- **Profiles**: each user runs `USER_RPS` (2) tasks/s (`constant_throughput`), so
  offered load = users × 2. `--quick` steps through `QUICK_STEPS`
  (`100 200 300 400 500`) with `QUICK_STEP_SECS` (24 s) per step, offered
  200→1000 RPS. `--full` runs constant-rate stages at the run.sh rates
  (`RATES`, users = rate / `USER_RPS`, 5 s warmup excluded + 30 s) and a soak
  stage (`SOAK_RATE=1000`, `SOAK_SECS=300`). `REPEAT` and the per-repetition
  target rotation match run.sh. LiteLLM defaults to `LITELLM_WORKERS=4` (sized
  for the 4-vCPU CI runner), unlike run.sh's `nproc`.
- **Metrics**: Locust's CSV percentiles are rounded to whole ms, too coarse for
  sub-ms overhead, so each worker process dumps full-precision samples and
  `bench/scripts/aggregate_locust.py` computes per target: non-stream
  p50/p90/p99/max (chat + completions + embeddings; `models` is only in the
  per-endpoint table because Lumewarp answers it without the upstream), RPS vs
  offered, failure %, stream total p99, TTFT p99 and its overhead,
  per-endpoint p50/p99, negative status counts, server CPU/PSS
  (`proc_sampler.py`) and generator CPU per process.
  - **TTFT**: `ttft_any_ms` is the first non-empty `reasoning`,
    `reasoning_content` or `content` delta; `ttft_content_ms` is the first
    non-empty `content` delta. `ttft_headline_ms` in `results.json` is
    `ttft_any_ms`, and the headline TTFT columns and overhead use it. Legacy
    dumps that lack a field fall back to their old `ttft_ms` distribution.
  - **Overhead**: paired target − direct within the same repetition, then the
    median across repetitions, shown as `median [min–max]`. With fewer than 5
    paired repetitions the uncertainty column is `range (n=N)`, not a
    confidence interval; with 5 or more it is a seeded bootstrap 95% CI of the
    paired median. The noise floor is max−min of direct's same percentile
    across repetitions; an overhead inside it gets a `(within floor X)`
    annotation, but the value is always shown.
  - **Per-step rate**: taken from `locust_stats_history.csv`, not Locust's
    sliding-window RPS. A step starts at the first history row that reaches its
    user count, skips `max(3 s worker report, observed spawn) + 1 s` of
    lead-in, and measures from the first count-changing report to the latest
    report in the same 3 s phase (±0.5 s) inside the step. The span must be at
    least 6 s; otherwise the step is reported as `incomplete history`
    (diagnostic, not a failure). An achieved rate below 90% of the step's
    offered rate flags the step, and so does a declared user count that never
    appears once the history covers the stage (`users never reached`).
- **Flags**: > 1% failures, < 90% of the offered rate (run or step), generator
  average CPU per process > `GEN_CPU_LIMIT_PCT` (85) or Locust's own "CPU usage
  above 90%" warning, a negative not rejected, or a step whose users were never
  reached. Overhead of a flagged row is marked `n/c` (not comparable).
- Locust is closed-loop (a slow target lowers the offered rate; no
  coordinated-omission correction), and Python adds per-request cost to every
  sample. Use it for correctness under load and relative comparisons; the
  `oha` numbers from `run.sh` remain the A6 reference.

Output, next to a run.sh run of the same `RUN_ID` (the section is also inserted
into its `summary.md` between `<!-- locust:begin/end -->` markers):

```
bench/results/<RUN_ID>/locust/
  meta.json  results.json  summary.md  scenarios.jsonl
  configs/  logs/                       templated configs, server logs
  <scenario>/<target>.rep<M>/           scenario = step | rate-<N> | soak
    locust_stats.csv  locust_stats_history.csv  locust_failures.csv
    locust_exceptions.csv  report.html  locust.log
    samples/samples-<pid>.json          full-precision samples per process
    server.cpu.csv  generator.cpu.csv   /proc samples (1 Hz, PSS)
```

`results.json` keeps the per-repetition runs and the per-scenario medians. TTFT
is exposed as `ttft_any_ms`, `ttft_content_ms` and the headline alias
`ttft_headline_ms`.

Self-tests:

```sh
python3 -I bench/locust/locustfile.py --selftest
uvx --from locust==2.32.4 --with geventhttpclient==2.6.1 \
  python -I bench/locust/locustfile.py --selftest-wiring
python3 -I bench/scripts/aggregate_locust.py --selftest
```

The first and third need only `python3`; `--selftest-wiring` loads `GatewayUser`
against a temporary loopback HTTP server and needs Locust. The `bench` workflow
runs all three before the smoke.

The smoke uses `QUICK_STEPS="20 50"`, `QUICK_STEP_SECS=15`, `BENCH_AUTH=1`,
`LOCUST_PROCESSES=2` and 4 keys, then runs `run-locust.sh --quick --no-build`
and `bench/scripts/aggregate_locust.py --ci-check --run-dir
bench/results/<RUN_ID>/locust`. `--ci-check` reads the already aggregated
`results.json` (it does not re-aggregate) and fails the job when direct or
Lumewarp has any flagged repetition — failures, an achieved rate or step below
90%, generator saturation, a step whose users were never reached — or when
Lumewarp accepts a negative request. Latency is not gated there.

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
  `aggregate.py` and `ci_check.py` self-tests on every PR. Its `control-plane`
  job also runs the `tests/load` helper unit tests; the nightly `schedule` and
  manual `workflow_dispatch` of that workflow run only `load-e2e`.
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

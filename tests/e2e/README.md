# P1 end-to-end exit gate

Real processes on ephemeral ports: Postgres database (created and dropped per test), control plane
(`python -m lumewarp_control_plane serve`), mock upstream (`bench/mock-upstream`) and the gateway in
control-plane mode. No separate project: the tests run in the control-plane's uv environment
(they use its `httpx`, `asyncpg` and security helpers).

    scripts/e2e-p1.sh          # builds binaries, starts dev Postgres if needed, runs pytest
    scripts/check-p1.sh        # every P1-A1 check, then the e2e gate

Environment: `E2E_PG_URL` (server DSN, default dev Postgres 127.0.0.1:55432), `E2E_PROFILE`
(`debug`|`release` gateway), `E2E_GATEWAY_BIN`, `E2E_MOCK_BIN`, `E2E_STRATEGY_GATEWAY_BIN` (a gateway
built with `--features strategy-priority-failover`; `scripts/e2e-p1.sh` builds and sets it, and
`test_strategy_crate.py` skips its crate test without it). Failing tests print every process log.

`test_p2_routing.py` (collected by `scripts/e2e-p1.sh` and `scripts/e2e-p2.sh`; no IdP) configures
P2 routing over the admin API against a second mock started with `--fail-every 1` (always 503)
and a closed port: retry to a second model with exactly one usage event for the final
model; fallback routers (503 and connect error, streaming too) answering with the client's
model name; a circuit breaker reaching `lumewarp_upstream_circuit_state=2` after N failures, after
which the failing model gets no attempts (`outcome="circuit_open"` only).

`test_catalog.py` (collected by both scripts; no IdP) is the acceptance of the model catalog
(`docs/plans/model-catalog.md`, 驗收條件): a model is callable, listed and answers with the called
name, streaming too, and so do aliases, routers and fallbacks; a new alias target takes new
requests; renames end the old name unless a model keeps it; a grant covers the called name only;
rollback restores all three kinds and weights, across renames, with grants and references
intact; usage rows record the served model, alias, router, fallback, attempts and the served
model's price, filter by every dimension and stay continuous across a rename. An in-process
OTLP sink proves a request is found by its `x-lumewarp-request-id`, with every attempt, without
the prompt, the response or the key, and that `traceparent` sampled=0 does not erase it.

## P2: real OIDC and authorization exit gate

    scripts/e2e-p2.sh

Runs five gates for each of `builtin` and `openfga` (10 tests): real Keycloak
authorization code + S256 PKCE login and zero-grant JIT, team visibility, cookie/CSRF
key creation by a Member and service-account creation by an Admin, streaming and
non-streaming gateway calls, owner/team usage and cost reconciliation, and removal
to gateway 401 in less than five seconds. The runner prints each backend's measured
revocation latency. P1's default collection excludes these live IdP tests.

The runner starts the local dev identity stack only if discovery or OpenFGA health
is unreachable. It reuses P1's binary builds, Postgres defaults, and process/database
reaper; it never shuts down the shared dev IdP stack. Docker, curl, Rust and uv are
required. Only loopback services and the mock upstream receive traffic.

Each backend uses its own disposable database. The OpenFGA run creates a fresh store
and model from `deploy/openfga/model.fga` using `openfga/cli:v0.8.2`, deleting the store
afterwards. Keycloak admin credentials are the dev compose credentials
(`admin` / `lumewarp`). Tests temporarily register the exact dynamic-port callback
on `lumewarp-cp` and remove only that URI on teardown. Alice and Bob use their
`@example.com` usernames and password `lumewarp`. Disable-login checks create,
successfully log in, disable and finally delete a throwaway realm user.

OpenFGA failure is isolated to a test-local HTTP relay that points requests at a
closed loopback port during the outage. Tests subscribe to completed requests and
require at least two failed changes polls and two failed full-scan BatchChecks,
checking that the admin API returns 503, neither key is suspended, and the gateway
continues serving both keys. Builtin has no separate remote IAM outage boundary.
Production defaults remain a 2-second changes poll and 300-second full scan; tests
set both to 1 second to exercise repeated error decisions promptly.

`p2_events.py` installs commit-notification triggers only in the disposable database.
It subscribes before actions and checks usage/publication/ACK state on notifications,
without fixed sleeps or timing-based retry loops. Stage fixtures explicitly express
the dependencies between the five gates; individual gates can also run via `-k`.
Live tests are scheduled nightly, not on pull requests.

# End-to-end gates

Real processes on ephemeral ports: Postgres database (created and dropped per test), control plane
(`python -m lumewarp_control_plane serve`), mock upstream (`bench/mock-upstream`) and the gateway in
control-plane mode. No separate project: the tests run in the control-plane's uv environment
(they use its `httpx`, `asyncpg` and security helpers).

    scripts/e2e-core.sh          # builds binaries, starts dev Postgres if needed, runs pytest
    scripts/check-all.sh         # every check, then the e2e gate

Environment: `E2E_PG_URL` (server DSN, default dev Postgres 127.0.0.1:55432), `E2E_PROFILE`
(`debug`|`release` gateway), `E2E_GATEWAY_BIN`, `E2E_MOCK_BIN`, `E2E_STRATEGY_GATEWAY_BIN` (a gateway
built with `--features strategy-priority-failover`; `scripts/e2e-core.sh` builds and sets it, and
`test_strategy_crate.py` skips its crate test without it). `test_strategy_cheapest_first.py` builds
its own binary with `--features strategy-cheapest-first` into
`gateway/target/e2e-cheapest-first`, so it runs under either script without replacing the default
binary. Failing tests print every process log.

`test_routing_resilience.py` (collected by `scripts/e2e-core.sh` and `scripts/e2e-identity.sh`; no IdP) configures
routing resilience over the admin API against a second mock started with `--fail-every 1` (always 503)
and a closed port: retry to a second model with exactly one usage event for the final
model; fallback routers (503 and connect error, streaming too) answering with the client's
model name; a circuit breaker reaching `lumewarp_upstream_circuit_state=2` after N failures, after
which the failing model gets no attempts (`outcome="circuit_open"` only).

`test_catalog.py` (collected by both scripts; no IdP) is the acceptance of the model catalog:
a model is callable, listed and answers with the called
name, streaming too, and so do aliases, routers and fallbacks; a new alias target takes new
requests; renames end the old name unless a model keeps it; a grant covers the called name only;
rollback restores all three kinds and weights, across renames, with grants and references
intact; usage rows record the served model, alias, router, fallback, attempts and the served
model's price, filter by every dimension and stay continuous across a rename. An in-process
OTLP sink proves a request is found by its `x-lumewarp-request-id`, with every attempt, without
the prompt, the response or the key, and that `traceparent` sampled=0 does not erase it.

## Identity: real OIDC and authorization gate

    scripts/e2e-identity.sh

Runs five identity gates and the self-service gate for each of `builtin` and `openfga`: real
Keycloak authorization code + S256 PKCE login and zero-grant JIT, team visibility, cookie/CSRF
key creation by a Member and service-account creation by an Admin, streaming and
non-streaming gateway calls, owner/team usage and cost reconciliation, removal
to gateway 401 in less than five seconds, and Bob's self-service key journey. The runner
prints each backend's measured revocation latency. The core gate's default collection excludes these
live IdP tests.

The runner starts the local dev identity stack only if discovery or OpenFGA health
is unreachable. It reuses the core gate's binary builds, Postgres defaults, and process/database
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

`database_events.py` installs commit-notification triggers only in the disposable database.
It subscribes before actions and checks usage/publication/ACK state on notifications,
without fixed sleeps or timing-based retry loops. Stage fixtures explicitly express
the dependencies between the gates; individual gates can also run via `-k`.
Live tests run by manual dispatch in CI, not on pull requests.

## Self-service keys

`test_self_service.py` (also collected only with `E2E_IDENTITY=1`, on both backends) is Bob's
journey as a plain member through the real IdP session: `/users/me` gives his team's
effective models, limits, member-key limits and remaining budget; he creates a key with an
expiry and the gateway serves a granted model while refusing one outside the grant; a
rotation with a one-hour grace keeps both secrets working as one key (usage stays on its
key id); a second rotation drops the older secret at once and graces the previous one; a
rotation with a five-second grace shows the gateway refusing the old secret at the
snapshot's `previous_key_expires_at` while the control plane's config version and key row
stay unchanged; a rotation without a grace stops the old secret; suspend, resume and
revoke agree with the gateway; another member's key, a team Bob is not in, an expiry past
the team lifetime and the member key limit are refused; the create bucket and the rotate
bucket are exhausted separately (rotate through a key that is refused after the limiter,
so it costs no rows) and each answers 429 with `Retry-After`, `rate_limited`, without
changing the key and without exhausting the other bucket; the audit rows for create and
rotate exist and carry no secret. Every gateway assertion polls the gateway for its
answer; nothing sleeps for a fixed time.

## No plaintext credentials in the database

The scan is **on by default**, including a direct `pytest` run with no script: an autouse
session fixture creates a run directory, a run id and the `secrets.jsonl` /
`secret-scans.jsonl` files when the scripts did not, and finalizes the scan at the end of
the session. `scripts/e2e-core.sh` (and therefore `scripts/e2e-identity.sh`) and
`scripts/load-e2e.sh` export the same context. The only opt-out is
`E2E_SKIP_SECRET_SCAN=1`; the end of the run then prints a warning saying nothing was
searched for. There is no per-suite or per-test switch.

The run is fail-closed. A missing or corrupt recorded-secret log, a corrupt line in the
scan-result file, a missing `pg_dump`, or a failed env-reference query is a hard failure
(the scan cannot claim to have looked). The harness records every secret it issues or
receives at the API boundary: `Stack.api`/`Stack.ok` and `identity.cookie_api` record
any returned `secret` or known token field before the value reaches the caller, so key
create, key rotate, catalog keys, PATs, gateway tokens and the load rotation are covered
without each test remembering. `stack.record_secret` additionally records the gateway
token from `bootstrap`, the break-glass password, session and CSRF cookies, the OIDC
state/nonce/PKCE verifier (from the redirect and the sealed login-state cookie), the OIDC
authorization code, and the values of `cp_env` names containing `SECRET`, `PASSWORD`,
`TOKEN` or `API_KEY` (for example `LUMEWARP_SECRET_KEY` and the OIDC client secret).
Upstream `api_key_ref` and notifier `url_ref` values are read from the database's
environment references and searched for too; no run creates a notifier channel yet (there
is no channel API), so only upstream refs appear.

Each stack's disposable database exists only until its teardown, so `Stack.teardown` runs
`secret_scan.scan_database` with `pg_dump` right before dropping it; this covers every
per-test/per-stack database, not just one. A database name carries a run-specific id
(`lumewarp_e2e_<run id>_<random>`). `secret_scan.py finalize` then runs after pytest and
before the scripts' reaper: it rescans databases a hard kill left behind, lists the live
databases that match this run's name pattern and scans any that the manifest does not
know about (then registers them so the reaper drops them), verifies that every database
the run created has a result, and fails the script on any hit, missing scan or scan
error.

The scan looks for full `lw-`/`lwp-`/`lwg-` tokens (prefix + 43 urlsafe characters, so a
stored `display_prefix` never matches) and for every recorded literal plain, base64,
percent-encoded, hex, and as `pg_dump` writes it in COPY text and as JSON inside that
COPY text (backslash, TAB, newline, quote and other escapes). Each `\\x...` bytea value in
the COPY data is decoded and searched as bytes, so an unrecorded token stored as binary is
a hit. The product stores no large objects; because `pg_dump` splits one over `lowrite()`
chunks, any large object in the dump (`lo_create`, `lo_import`, `lowrite`) is itself
reported as a hit instead of stitching chunks. Hits name the `COPY` table, kind, line and
offset and never print the secret. A failure in teardown fails the test; the scripts also
fail on a scan error. `E2E_PG_DUMP` overrides the dump command (for example
`E2E_PG_DUMP="docker exec -i lumewarp-dev-postgres-1 pg_dump"`) when the local client is
not the server's major version.

The Playwright UI suite is **not** scanned. Its runner (`ui/e2e/serve.py`) starts the
control plane as a child web server, and Playwright cannot turn that server's exit into a
test failure, so this scan cannot cover databases it creates. This is a known gap, not an
implication that the UI suite is clean.

What stays unobservable: Keycloak's own cookies, admin token and signing keys; the dev
users' human password (`lumewarp`, known and not issued by Lumewarp); and any value that
exists only in the parent shell's environment without being referenced by the database or
present in `cp_env`. A value that reaches only the control plane's environment is
recorded through `cp_env`; a test-process-only value that is neither exported to the
control plane nor referenced by a row cannot be attributed to a credential.

The scanner's unit test (no binaries or Postgres needed; the few real-dump cases use the
dev Postgres and `pg_dump` when reachable, and skip with a reason otherwise; the
no-stack session does not connect at all):

    PYTHONDONTWRITEBYTECODE=1 E2E_GATEWAY_BIN=/nonexistent E2E_MOCK_BIN=/nonexistent \
      uv run --frozen --project control-plane pytest -c tests/e2e/pytest.ini \
      --rootdir tests/e2e tests/e2e/test_secret_scan.py -q -p no:cacheprovider

They cover a planted full token of every kind, display prefixes and hashes passing, every
literal encoding, COPY/JSON escaping against a real `pg_dump` (backslash, TAB, newline,
quote), bytea decoding, large-object detection, the API-boundary recording of a real
`cookie_api` response (a base64 copy in a dump is a hit), the unregistered-database and
early-drop finalizer failures, and that a planted failure through real assertion
rewriting never prints the secret.

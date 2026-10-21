# P1 end-to-end exit gate

Real processes on ephemeral ports: Postgres database (created and dropped per test), control plane
(`python -m lumewarp_control_plane serve`), mock upstream (`bench/mock-upstream`) and the gateway in
control-plane mode. No separate project: the tests run in the control-plane's uv environment
(they use its `httpx`, `asyncpg` and security helpers).

    scripts/e2e-p1.sh          # builds binaries, starts dev Postgres if needed, runs pytest
    scripts/check-p1.sh        # every P1-A1 check, then the e2e gate

Environment: `E2E_PG_URL` (server DSN, default dev Postgres 127.0.0.1:55432), `E2E_PROFILE`
(`debug`|`release` gateway), `E2E_GATEWAY_BIN`, `E2E_MOCK_BIN`. Failing tests print every process log.

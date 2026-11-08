#!/usr/bin/env bash
# CP-backed MCP acceptance, reusing core's isolated database/process and pg_dump finalizer.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cargo build --manifest-path "$ROOT/bench/mock-upstream/Cargo.toml" --bin mcp-a2a-mock
bash "$ROOT/gateway/plugins/build.sh" --tests
export E2E_MCP=1
# Core gives this invocation its own random run id/databases; Stack uses free non-9000 ports.
bash "$ROOT/scripts/e2e-core.sh" -k test_cp_backed_mcp "$@"

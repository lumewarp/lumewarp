#!/usr/bin/env bash
# MCP acceptance: standalone proxy tests followed by Postgres + CP/API/config-stream tests.
# --standalone runs only Rust scenarios; --cp-only runs only CP-backed acceptance.
# CP mode accepts trailing pytest options and honours e2e-core.sh's E2E_PG_URL/E2E_PROFILE.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-all}"
case "$mode" in
  --cp-only) shift; exec bash "$ROOT/scripts/e2e-mcp-cp.sh" "$@" ;;
  --standalone) shift ;;
  all) ;;
  *) echo "usage: $0 [--standalone | --cp-only [pytest options...]]" >&2; exit 2 ;;
esac
cargo build --manifest-path "$ROOT/bench/mock-upstream/Cargo.toml" --bin mcp-a2a-mock
bash "$ROOT/gateway/plugins/build.sh" --tests
(cd "$ROOT/gateway" && cargo test -p lumewarp-gateway --test mcp -- --nocapture)
if [[ "$mode" != --standalone ]]; then
  bash "$ROOT/scripts/e2e-mcp-cp.sh" "$@"
fi

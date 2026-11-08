#!/usr/bin/env bash
# Standalone MCP acceptance; uses the real mock and gateway child processes.
# The control-plane MCP API is not required by this runner.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cargo build --manifest-path "$ROOT/bench/mock-upstream/Cargo.toml" --bin mcp-a2a-mock
bash "$ROOT/gateway/plugins/build.sh" --tests
cd "$ROOT/gateway"
cargo test -p lumewarp-gateway --test mcp -- --nocapture

#!/usr/bin/env bash
# Runs every check locally, then the e2e gate. Mirrors .github/workflows/ci.yml.
# Needs the dev Postgres (scripts/dev-db.sh up) or LUMEWARP_TEST_DATABASE_URL/E2E_PG_URL.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
step() { printf '\n==== %s\n' "$*"; }

step "gateway: fmt, clippy, test"
(cd "$root/gateway" && cargo fmt --all --check && cargo clippy --all-targets -- -D warnings && cargo test)

step "bench mock upstream: fmt, clippy, test"
(cd "$root/bench/mock-upstream" && cargo fmt --all --check && cargo clippy --all-targets -- -D warnings && cargo test)

step "control plane: ruff, pytest"
(cd "$root/control-plane" && uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run pytest)

step "ui: check, build"
(cd "$root/ui" && pnpm install --frozen-lockfile && pnpm check && pnpm build)

step "proto: generated code is current, buf lint"
"$root/scripts/gen-proto.sh"
if ! git -C "$root" diff --exit-code -- control-plane/src/lumewarp_control_plane/_proto; then
  echo "generated protobuf code is stale; commit the result of scripts/gen-proto.sh" >&2
  exit 1
fi
if [[ -n "$(git -C "$root" status --porcelain -- control-plane/src/lumewarp_control_plane/_proto | grep '^??' || true)" ]]; then
  echo "scripts/gen-proto.sh produced untracked files; commit them" >&2
  exit 1
fi
if command -v buf >/dev/null; then
  (cd "$root/proto" && buf lint)
else
  echo "buf not installed: skipping buf lint (CI runs it)"
fi

step "e2e: core gate"
"$root/scripts/e2e-core.sh"

echo; echo "all checks passed"

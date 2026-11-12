#!/usr/bin/env bash
# Builds and tests the lumewarp-plugin-dev skill's templates against this tree:
# the WASM templates with the SDK patched to gateway/crates/lumewarp-plugin-sdk,
# the Python templates with the control plane installed from control-plane/.
# Each template is copied out of the repository first, as a user would, so a
# path that only resolves inside the repository fails here too. Finally the
# gateway built from this tree must load all three components.
#
# Needs cargo with the wasm32-wasip2 target and uv. LUMEWARP_BIN may name a
# gateway binary already built; otherwise one is built.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
skill="$root/.claude/skills/lumewarp-plugin-dev"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for ref in "$skill"/references/*.md; do
  [[ -e "$ref" ]] || { echo "dangling reference: $ref" >&2; exit 1; }
done

sdk="$root/gateway/crates/lumewarp-plugin-sdk"
patch="patch.\"https://github.com/lumewarp/lumewarp\".lumewarp-plugin-sdk.path=\"$sdk\""
mkdir -p "$work/plugins/router" "$work/plugins/auth" "$work/plugins/a2a-middleware"
declare -A dir=([router-strategy]=router [upstream-auth]=auth [a2a-middleware]=a2a-middleware)
for kind in router-strategy upstream-auth a2a-middleware; do
  echo "== $kind"
  cp -r "$skill/templates/$kind" "$work/$kind"
  cp "$skill/templates/rust-toolchain.toml" "$work/$kind/"
  (
    cd "$work/$kind"
    cargo fmt --check
    cargo clippy --config "$patch" --all-targets -q -- -D warnings
    cargo test --config "$patch" -q
    cargo build --config "$patch" --release --target wasm32-wasip2 -q
    cp target/wasm32-wasip2/release/*.wasm "$work/plugins/${dir[$kind]}/"
  )
done

for kind in python-auth python-authz; do
  echo "== $kind"
  cp -r "$skill/templates/$kind" "$work/$kind"
  (
    cd "$work/$kind"
    sed -i 's|"lumewarp-control-plane @ git+[^"]*"|"lumewarp-control-plane"|' pyproject.toml
    printf '\n[tool.uv.sources]\nlumewarp-control-plane = { path = "%s", editable = true }\n' \
      "$root/control-plane" >> pyproject.toml
    uvx ruff check --select E,F,I,UP,B .
    uvx ruff format --check .
    uv run --quiet pytest -q -p no:cacheprovider
  )
done

echo "== load"
bin="${LUMEWARP_BIN:-}"
if [[ -z "$bin" ]]; then
  cargo build --manifest-path "$root/gateway/Cargo.toml" --release -q -p lumewarp-gateway
  bin="$root/gateway/target/release/lumewarp"
fi
listed="$(LUMEWARP_ROUTER_PLUGIN_DIR="$work/plugins/router" \
  LUMEWARP_AUTH_PLUGIN_DIR="$work/plugins/auth" \
  LUMEWARP_A2A_MIDDLEWARE_DIR="$work/plugins/a2a-middleware" \
  "$bin" plugins)"
for expected in $'1\tmy_strategy' $'2\tmy_auth' $'3\tmy_middleware'; do
  grep -qF "$expected" <<<"$listed" || { echo "not loaded: $expected" >&2; echo "$listed" >&2; exit 1; }
done
echo "ok"

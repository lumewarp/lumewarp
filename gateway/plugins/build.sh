#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
for name in priority-failover cheapest-first; do
  cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/router/$name/Cargo.toml" --target-dir "$root/target/plugin-build"
  mkdir -p "$root/target/plugins/router"
  cp "$root/target/plugin-build/wasm32-wasip2/release/lumewarp_plugin_${name//-/_}.wasm" "$root/target/plugins/router/$name.wasm"
done
for name in hmac-signature; do
  cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/auth/$name/Cargo.toml" --target-dir "$root/target/plugin-build"
  mkdir -p "$root/target/plugins/auth"
  cp "$root/target/plugin-build/wasm32-wasip2/release/lumewarp_plugin_${name//-/_}.wasm" "$root/target/plugins/auth/$name.wasm"
done
for name in strip-artifacts; do
  cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/a2a-middleware/$name/Cargo.toml" --target-dir "$root/target/plugin-build"
  mkdir -p "$root/target/plugins/a2a-middleware"
  cp "$root/target/plugin-build/wasm32-wasip2/release/lumewarp_plugin_${name//-/_}.wasm" "$root/target/plugins/a2a-middleware/$name.wasm"
done
if [[ "${1:-}" == "--tests" ]]; then
  for entry in auth/noop-test auth/fixture a2a-middleware/noop-test router/loop-test router/misbehave-test; do
    kind="${entry%/*}"
    cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/$entry/Cargo.toml" --target-dir "$root/target/plugin-build"
    case "$entry" in
      auth/noop-test) file=lumewarp_plugin_auth_noop_test ;;
      auth/fixture) file=lumewarp_plugin_auth_fixture ;;
      a2a-middleware/noop-test) file=lumewarp_plugin_middleware_noop_test ;;
      router/loop-test) file=lumewarp_plugin_loop_test ;;
      router/misbehave-test) file=lumewarp_plugin_router_misbehave_test ;;
    esac
    mkdir -p "$root/target/plugins-test/$kind"
    cp "$root/target/plugin-build/wasm32-wasip2/release/$file.wasm" "$root/target/plugins-test/$entry.wasm"
  done
  # The A2A middleware fixture lives under the strip-artifacts example so the
  # example and its tests stay together.
  cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/a2a-middleware/strip-artifacts/fixtures/misbehave/Cargo.toml" --target-dir "$root/target/plugin-build"
  mkdir -p "$root/target/plugins-test/a2a-middleware"
  cp "$root/target/plugin-build/wasm32-wasip2/release/lumewarp_plugin_misbehave_test.wasm" "$root/target/plugins-test/a2a-middleware/misbehave.wasm"
fi

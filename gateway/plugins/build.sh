#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
for name in priority-failover cheapest-first; do
  cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/router/$name/Cargo.toml" --target-dir "$root/target/plugin-build"
  mkdir -p "$root/target/plugins/router"
  cp "$root/target/plugin-build/wasm32-wasip2/release/lumewarp_plugin_${name//-/_}.wasm" "$root/target/plugins/router/$name.wasm"
done
if [[ "${1:-}" == "--tests" ]]; then
  for entry in auth/noop-test a2a-middleware/noop-test router/loop-test; do
    kind="${entry%/*}"
    cargo build --release --target wasm32-wasip2 --manifest-path "$root/plugins/$entry/Cargo.toml" --target-dir "$root/target/plugin-build"
    case "$kind" in
      auth) file=lumewarp_plugin_auth_noop_test ;;
      a2a-middleware) file=lumewarp_plugin_middleware_noop_test ;;
      router) file=lumewarp_plugin_loop_test ;;
    esac
    mkdir -p "$root/target/plugins-test/$kind"
    cp "$root/target/plugin-build/wasm32-wasip2/release/$file.wasm" "$root/target/plugins-test/$entry.wasm"
  done
fi

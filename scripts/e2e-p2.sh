#!/usr/bin/env bash
# Real Keycloak + isolated OpenFGA stores; reuse P1 builds and process/database reaping.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
if ! curl -fsS -o /dev/null http://127.0.0.1:18080/realms/lumewarp/.well-known/openid-configuration ||
   ! curl -fsS -o /dev/null http://127.0.0.1:18081/healthz; then
  "$root/scripts/dev-idp.sh" up
fi
export E2E_P2=1
exec "$root/scripts/e2e-p1.sh" --ignore="$root/tests/e2e/test_p1_exit_gate.py" -s "$@"

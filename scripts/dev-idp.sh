#!/usr/bin/env bash
# Manage the Lumewarp dev identity stack next to the dev Postgres:
# Keycloak (OIDC login) on 127.0.0.1:18080, OpenFGA (authorization) on 127.0.0.1:18081.
#   scripts/dev-idp.sh up      start, wait until healthy, `openfga init` the store, print env
#   scripts/dev-idp.sh down    stop and remove only the IdP containers
#   scripts/dev-idp.sh status  show containers, health and `openfga check`
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo_root/deploy/dev/compose.yaml"
dc() { docker compose -f "$compose_file" --profile idp "$@"; }

keycloak_url="http://127.0.0.1:18080"
openfga_url="http://127.0.0.1:18081"
realm="lumewarp"
store_name="lumewarp"
oidc_client_id="lumewarp-cp"
oidc_client_secret="lumewarp-dev-secret"
ready_timeout=180

wait_http() { # url, what
  local url=$1 what=$2 i
  for ((i = 0; i < ready_timeout; i++)); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 1
  done
  echo "timed out after ${ready_timeout}s waiting for $what at $url" >&2
  return 1
}

openfga() { # init|check: the control plane's own command against the dev store
  (cd "$repo_root/control-plane" &&
    env -u LUMEWARP_OPENFGA_STORE_ID -u LUMEWARP_OPENFGA_MODEL_ID \
      LUMEWARP_OPENFGA_URL="$openfga_url" LUMEWARP_OPENFGA_STORE="$store_name" \
      uv run -q lumewarp-control-plane openfga "$1")
}

print_env() {
  cat <<EOF
export LUMEWARP_OIDC_ISSUER=$keycloak_url/realms/$realm
export LUMEWARP_OIDC_CLIENT_ID=$oidc_client_id
export LUMEWARP_OIDC_CLIENT_SECRET=$oidc_client_secret
export LUMEWARP_OPENFGA_URL=$openfga_url
export LUMEWARP_OPENFGA_STORE=$store_name
EOF
}

case "${1:-}" in
  up)
    dc up -d keycloak openfga
    wait_http "$keycloak_url/realms/$realm/.well-known/openid-configuration" keycloak
    wait_http "$openfga_url/healthz" openfga

    openfga init
    print_env
    ;;
  down)
    # Only the IdP containers; dev Postgres is not part of this profile.
    dc rm -sf keycloak openfga
    ;;
  status)
    dc ps keycloak openfga
    if curl -fsS -o /dev/null "$keycloak_url/realms/$realm/.well-known/openid-configuration"; then
      echo "keycloak: ready ($keycloak_url/realms/$realm)"
    else
      echo "keycloak: not ready"
    fi
    if curl -fsS -o /dev/null "$openfga_url/healthz"; then
      echo "openfga: ready ($openfga_url)"
      if openfga check; then print_env; fi
    else
      echo "openfga: not ready"
    fi
    ;;
  *)
    echo "usage: $0 up|down|status" >&2
    exit 2
    ;;
esac

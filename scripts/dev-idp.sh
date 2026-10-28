#!/usr/bin/env bash
# Manage the Lumewarp dev identity stack next to the dev Postgres:
# Keycloak (OIDC login) on 127.0.0.1:18080, OpenFGA (authorization) on 127.0.0.1:18081.
#   scripts/dev-idp.sh up      start, wait until healthy, seed OpenFGA, print env
#   scripts/dev-idp.sh down    stop and remove only the IdP containers
#   scripts/dev-idp.sh status  show containers, health and the current OpenFGA ids
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo_root/deploy/dev/compose.yaml"
model_file="$repo_root/deploy/openfga/model.fga"
dc() { docker compose -f "$compose_file" --profile idp "$@"; }

keycloak_url="http://127.0.0.1:18080"
openfga_url="http://127.0.0.1:18081"
realm="lumewarp"
store_name="lumewarp"
oidc_client_id="lumewarp-cp"
oidc_client_secret="lumewarp-dev-secret"
openfga_cli_image="openfga/cli:v0.8.2"
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

openfga_store_id() {
  curl -fsS "$openfga_url/stores?name=$store_name" | jq -r '.stores[0].id // empty'
}

latest_model_id() { # store id
  curl -fsS "$openfga_url/stores/$1/authorization-models?page_size=1" |
    jq -r '.authorization_models[0].id // empty'
}

write_model() { # store id
  # The CLI container shares the host network so it can reach OpenFGA on loopback.
  docker run --rm --network host \
    -e FGA_API_URL="$openfga_url" \
    -v "$model_file:/model.fga:ro" \
    "$openfga_cli_image" model write \
    --store-id "$1" --file /model.fga |
    jq -r '.authorization_model_id'
}

print_env() { # store id, model id
  cat <<EOF
export LUMEWARP_OIDC_ISSUER=$keycloak_url/realms/$realm
export LUMEWARP_OIDC_CLIENT_ID=$oidc_client_id
export LUMEWARP_OIDC_CLIENT_SECRET=$oidc_client_secret
export LUMEWARP_OPENFGA_URL=$openfga_url
export LUMEWARP_OPENFGA_STORE_ID=$1
export LUMEWARP_OPENFGA_MODEL_ID=$2
EOF
}

case "${1:-}" in
  up)
    dc up -d keycloak openfga
    wait_http "$keycloak_url/realms/$realm/.well-known/openid-configuration" keycloak
    wait_http "$openfga_url/healthz" openfga

    store_id="$(openfga_store_id)"
    if [[ -z $store_id ]]; then
      store_id="$(curl -fsS -X POST -H 'Content-Type: application/json' \
        -d "{\"name\":\"$store_name\"}" "$openfga_url/stores" | jq -r '.id')"
    fi
    [[ -n $store_id ]] || {
      echo "failed to create the OpenFGA store" >&2
      exit 1
    }

    model_id="$(write_model "$store_id")"
    [[ -n $model_id ]] || {
      echo "failed to write the OpenFGA model" >&2
      exit 1
    }

    echo "store id: $store_id"
    echo "model id: $model_id"
    print_env "$store_id" "$model_id"
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
      store_id="$(openfga_store_id)"
      model_id=""
      if [[ -n $store_id ]]; then
        model_id="$(latest_model_id "$store_id")"
        echo "store id: $store_id"
        echo "model id: ${model_id:-<none>}"
      else
        echo "store id: <none> (run '$0 up')"
      fi
      [[ -z $store_id || -z $model_id ]] || print_env "$store_id" "$model_id"
    else
      echo "openfga: not ready"
    fi
    ;;
  *)
    echo "usage: $0 up|down|status" >&2
    exit 2
    ;;
esac

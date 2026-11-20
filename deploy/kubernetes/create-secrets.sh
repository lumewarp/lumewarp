#!/usr/bin/env bash
# Create the Secrets the plain manifests reference, with random values, in namespace
# `lumewarp` (override with NAMESPACE=...). A Secret that already exists is left alone, so a
# rerun never changes a password the database already uses.
#
#   deploy/kubernetes/create-secrets.sh
#
#   lumewarp-postgres            password       the PostgreSQL password
#   lumewarp-database            database-url   the control plane's URL to the `lumewarp` database
#   lumewarp-openfga-datastore   uri            OpenFGA's URL to the `openfga` database
#   lumewarp-encryption-key      encryption-key seals stored upstream API keys
#   lumewarp-gateway-tokens     LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN  the default zone's
#                               fixed gateway token; the control plane reads it at startup
#
# `global.zone` defaults to `default`; if you change it, rename the token key to
# LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN (upper-cased, `-` -> `_`).
set -euo pipefail

namespace="${NAMESPACE:-lumewarp}"
k() { kubectl -n "$namespace" "$@"; }
exists() { k get secret "$1" >/dev/null 2>&1; }
# Finite input, so no stage dies of SIGPIPE under pipefail.
random() { head -c 4096 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c "1-$1"; }

# --save-config: the manifests apply the same Namespace later without a warning.
kubectl get namespace "$namespace" >/dev/null 2>&1 || kubectl create namespace "$namespace" --save-config

if exists lumewarp-postgres; then
  password="$(k get secret lumewarp-postgres -o jsonpath='{.data.password}' | base64 -d)"
  echo "lumewarp-postgres exists; reusing its password"
else
  password="$(random 32)"
  k create secret generic lumewarp-postgres --from-literal=password="$password"
fi

host=lumewarp-postgres:5432
exists lumewarp-database || k create secret generic lumewarp-database \
  --from-literal=database-url="postgresql+asyncpg://lumewarp:${password}@${host}/lumewarp"
exists lumewarp-openfga-datastore || k create secret generic lumewarp-openfga-datastore \
  --from-literal=uri="postgres://lumewarp:${password}@${host}/openfga?sslmode=disable"
exists lumewarp-encryption-key || k create secret generic lumewarp-encryption-key \
  --from-literal=encryption-key="$(random 48)"
if exists lumewarp-gateway-tokens; then
  if [[ -z "$(k get secret lumewarp-gateway-tokens \
      -o jsonpath='{.data.LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN}')" ]]; then
    echo "lumewarp-gateway-tokens has no LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN;" \
      "delete it and rerun this script" >&2
    exit 1
  fi
  echo "lumewarp-gateway-tokens exists; reusing its zone token"
else
  k create secret generic lumewarp-gateway-tokens \
    --from-literal=LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN="$(random 48)"
fi
echo "secrets ready in namespace $namespace"

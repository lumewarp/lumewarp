#!/usr/bin/env bash
# Render and lint the lumewarp-stack chart for its value sets and assert the wiring: the
# bundled PostgreSQL and the database Secret, the migration Job, and OpenFGA. Uses helm from
# PATH, or the alpine/helm image through docker. Runs `helm dependency build` first.
#
#   bash deploy/helm/lumewarp-stack/check.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v helm >/dev/null 2>&1; then
  H() { (cd "$here" && helm "$@"); }
else
  command -v docker >/dev/null 2>&1 || { echo "check.sh needs helm or docker" >&2; exit 1; }
  H() { docker run --rm -v "$here:/c" -w /c alpine/helm:3.16.3 "$@"; }
fi
chart=lumewarp-stack

failures=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; failures=$((failures + 1)); }
expect() { if printf '%s\n' "$3" | grep -qE "$2"; then ok "$1"; else bad "$1"; fi; }
refute() { if printf '%s\n' "$3" | grep -qE "$2"; then bad "$1"; else ok "$1"; fi; }
render() { # NAME ARGS...
  local name="$1"; shift
  file="$tmp/$name.yaml"
  H lint --strict -n lumewarp "$chart" "$@" >"$tmp/$name.lint" 2>&1 || bad "$name: lint failed: $(tr '\n' ' ' <"$tmp/$name.lint" | cut -c1-300)"
  if H template lumewarp "$chart" -n lumewarp "$@" >"$file" 2>"$file.err"; then echo "case $name"; else bad "$name: render error: $(tr '\n' ' ' <"$file.err" | cut -c1-300)"; : >"$file"; fi
}
# doc KIND NAME prints one rendered document.
doc() { awk -v k="$1" -v n="$2" 'BEGIN{RS="\n---\n"} $0 ~ "(^|\n)kind: "k"\n" && $0 ~ "\n  name: "n"\n"' "$file"; }

# The OpenFGA chart repository must be known to helm before a dependency build.
repo=https://openfga.github.io/helm-charts
if command -v helm >/dev/null 2>&1; then
  helm repo add openfga "$repo" >/dev/null 2>&1
  (cd "$here" && helm dependency build "$chart") >"$tmp/dep.log" 2>&1 || { cat "$tmp/dep.log"; bad "helm dependency build failed"; }
else
  docker run --rm --entrypoint sh -e HOME=/tmp -v "$here:/c" -w /c alpine/helm:3.16.3 \
    -c "helm repo add openfga $repo >/dev/null && helm dependency build $chart" >"$tmp/dep.log" 2>&1 || { cat "$tmp/dep.log"; bad "helm dependency build failed"; }
fi

# --- defaults ------------------------------------------------------------------------------
render defaults
all="$(cat "$file")"
expect "defaults: bundled PostgreSQL StatefulSet" '^kind: StatefulSet' "$(doc StatefulSet lumewarp-postgres)"
expect "defaults: postgres image pinned to an exact tag" 'image: "postgres:[0-9]+\.[0-9]+-alpine"' "$(doc StatefulSet lumewarp-postgres)"
expect "defaults: postgres runs as uid 70" 'runAsUser: 70' "$(doc StatefulSet lumewarp-postgres)"
expect "defaults: postgres has a volume claim" 'volumeClaimTemplates' "$(doc StatefulSet lumewarp-postgres)"
expect "defaults: database Secret exists under the fixed name" 'database-url:' "$(doc Secret lumewarp-stack-database)"
url="$(doc Secret lumewarp-stack-database | sed -n 's/^  database-url: "\(.*\)"/\1/p' | base64 -d)"
expect "defaults: database URL points at the bundled service" '^postgresql\+asyncpg://lumewarp:[A-Za-z0-9]{32}@lumewarp-postgres:5432/lumewarp$' "$url"
expect "defaults: control plane reads the stack Secret" 'name: lumewarp-stack-database' "$(doc Deployment lumewarp-control-plane)"
expect "defaults: migrate Job renders and waits for postgres" 'wait-for-postgres' "$(doc Job lumewarp-migrate-1)"
expect "defaults: migrate Job reads the same Secret" 'name: lumewarp-stack-database' "$(doc Job lumewarp-migrate-1)"
refute "defaults: no helm migrate hook" 'helm.sh/hook' "$all"
refute "defaults: no OpenFGA" 'lumewarp-openfga|app.kubernetes.io/name: openfga' "$all"

# --- external database ---------------------------------------------------------------------
render external --set postgres.enabled=false --set lumewarp.controlPlane.databaseUrl.secretName=mydb
all="$(cat "$file")"
refute "external: no postgres objects" 'lumewarp-postgres|lumewarp-stack-database' "$all"
expect "external: control plane reads mydb" 'name: mydb' "$(doc Deployment lumewarp-control-plane)"
expect "external: migrate Job reads mydb" 'name: mydb' "$(doc Job lumewarp-migrate-1)"
refute "external: migrate Job does not wait for postgres" 'wait-for-postgres' "$(doc Job lumewarp-migrate-1)"

# --- OpenFGA -------------------------------------------------------------------------------
render openfga -f "$chart/values-openfga.yaml"
all="$(cat "$file")"
cp="$(doc Deployment lumewarp-control-plane)"
expect "openfga: backend openfga" 'LUMEWARP_AUTHZ_BACKEND' "$cp"
expect "openfga: URL is the openfga service" 'value: "http://lumewarp-openfga:8080"' "$cp"
expect "openfga: init container" 'name: openfga-init' "$cp"
expect "openfga: service has the fixed name" 'name: lumewarp-openfga' "$(doc Service lumewarp-openfga)"
of="$(doc Deployment lumewarp-openfga)"
expect "openfga: datastore is postgres from the stack Secret" 'lumewarp-stack-openfga-datastore' "$of"
expect "openfga: migrates in an init container, not a hook Job" 'name: migrate-database' "$of"
refute "openfga: no OpenFGA migrate Job" 'name: lumewarp-openfga-migrate' "$all"
expect "openfga: datastore Secret targets database openfga" 'uri:' "$(doc Secret lumewarp-stack-openfga-datastore)"
ouri="$(doc Secret lumewarp-stack-openfga-datastore | sed -n 's/^  uri: "\(.*\)"/\1/p' | base64 -d)"
expect "openfga: URI" '^postgres://lumewarp:[A-Za-z0-9]{32}@lumewarp-postgres:5432/openfga\?sslmode=disable$' "$ouri"
refute "openfga: no helm hooks on the postgres or migration path" 'helm.sh/hook: pre-install' "$all"

if [ "$failures" -gt 0 ]; then echo "$failures check(s) failed"; exit 1; fi
echo "all lumewarp-stack render cases passed"

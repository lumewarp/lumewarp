#!/usr/bin/env bash
# Render cases for gateway.otel.samplingRatio. Needs helm on PATH, or docker for alpine/helm.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chart="$here/lumewarp"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v helm >/dev/null 2>&1; then
  helm_cmd=(helm)
  chart_arg="$chart"
  values_dir="$tmp"
else
  helm_cmd=(docker run --rm -v "$chart:/chart:ro" -v "$tmp:/values:ro" alpine/helm:3.16.4)
  chart_arg=/chart
  values_dir=/values
fi

common=(
  --set gateway.gatewayId=gw
  --set gateway.token.secretName=gw-token
  --set gateway.controlPlaneUrl=http://cp:50051
  --set controlPlane.enabled=false
  --set gateway.otel.endpoint=http://otel:4317
)
failures=0

render() {
  "${helm_cmd[@]}" template lumewarp "$chart_arg" "${common[@]}" --show-only templates/gateway-deployment.yaml "$@" 2>&1
}

check_accept() { # name, expected rendered value, helm args
  local name="$1" want="$2"
  shift 2
  local out
  if ! out="$(render "$@")"; then
    echo "FAIL $name: rejected: $out"
    failures=$((failures + 1))
    return
  fi
  local got
  got="$(printf '%s\n' "$out" | grep -A1 'name: OTEL_TRACES_SAMPLER_ARG' | sed -n 's/.*value: "\(.*\)"/\1/p')"
  if [ "$got" = "$want" ]; then
    echo "ok   $name: $got"
  else
    echo "FAIL $name: rendered '$got', wanted '$want'"
    failures=$((failures + 1))
  fi
}

check_reject() { # name, helm args
  local name="$1"
  shift
  local out
  if out="$(render "$@")"; then
    echo "FAIL $name: accepted"
    failures=$((failures + 1))
  elif printf '%s' "$out" | grep -q 'samplingRatio must be a number from 0 to 1'; then
    echo "ok   $name: rejected"
  else
    echo "FAIL $name: rejected for another reason: $out"
    failures=$((failures + 1))
  fi
}

from_file() { # value as YAML; prints the path helm sees
  local file="values-$((++file_count)).yaml"
  printf 'gateway:\n  otel:\n    samplingRatio: %s\n' "$1" >"$tmp/$file"
  chmod a+r "$tmp/$file"
  values_arg="$values_dir/$file"
}

chmod a+rx "$tmp"
file_count=0

from_file 0; check_accept "file 0" 0 -f "$values_arg"
from_file 1; check_accept "file 1" 1 -f "$values_arg"
from_file 0.25; check_accept "file 0.25" 0.25 -f "$values_arg"
from_file 1.0; check_accept "file 1.0" 1 -f "$values_arg"
from_file 0.00000001; check_accept "file 0.00000001" 1e-08 -f "$values_arg"
from_file 1e-08; check_accept "file 1e-08" 1e-08 -f "$values_arg"
check_accept "--set 0.25" 0.25 --set gateway.otel.samplingRatio=0.25
check_accept "--set 1e-8" 1e-8 --set gateway.otel.samplingRatio=1e-8
check_accept "--set-string 0.5" 0.5 --set-string gateway.otel.samplingRatio=0.5
check_accept "default" 1

from_file -0.1; check_reject "file -0.1" -f "$values_arg"
from_file 1.5; check_reject "file 1.5" -f "$values_arg"
from_file 2; check_reject "file 2" -f "$values_arg"
from_file 1e3; check_reject "file 1e3" -f "$values_arg"
from_file abc; check_reject "file abc" -f "$values_arg"
from_file true; check_reject "file true" -f "$values_arg"
check_reject "--set -0.1" --set gateway.otel.samplingRatio=-0.1
check_reject "--set abc" --set gateway.otel.samplingRatio=abc
check_reject "--set true" --set gateway.otel.samplingRatio=true
check_reject "--set-string empty" --set-string gateway.otel.samplingRatio=

if [ "$failures" -ne 0 ]; then
  echo "$failures case(s) failed"
  exit 1
fi
echo "all sampling ratio render cases passed"

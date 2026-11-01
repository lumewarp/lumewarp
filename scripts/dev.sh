#!/usr/bin/env bash
# Local dev preview: dev Postgres (+ Keycloak/OpenFGA), mock upstream, control plane,
# gateway and the Vite UI.
#
#   scripts/dev.sh tab        inside herdr: open a `lumewarp-dev` tab, one pane per service
#   scripts/dev.sh all        everything in this terminal with prefixed logs; Ctrl+C stops it
#   scripts/dev.sh stop       close the herdr tab (containers keep running)
#   scripts/dev.sh run <svc>  run one service in the foreground (mock, cp, gw, ui)
#
# `make dev` picks `tab` inside herdr and `all` elsewhere.
#
#   DEV_AUTHZ=openfga         authorization backend (default builtin)
#   LUMEWARP_BREAK_GLASS_PASSWORD  override the development emergency password
#
# Ports: UI 5173 (Vite, proxies /api to the CP), CP 8091 (+ gRPC 50051, metrics 9465),
# gateway 4000 (+ admin 9464), mock upstream 9100.
# An empty database gets a demo router `demo`, a team `demo` and one key; the key secret
# is kept in .dev/demo-key. Each start issues a fresh gateway token (.dev/env).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
self="$root/scripts/dev.sh"
state="$root/.dev"
sso=1
authz="${DEV_AUTHZ:-builtin}"
break_glass_password="${LUMEWARP_BREAK_GLASS_PASSWORD:-dev-break-glass}"
admin_csrf=""
tab_label="lumewarp-dev"
gateway_id="gw-dev"
cp_url="http://127.0.0.1:8091"
ui_url="http://127.0.0.1:5173"
export PATH="$HOME/.cargo/bin:$PATH"

log() { printf '\033[1m[dev]\033[0m %s\n' "$*"; }
die() { log "$*" >&2; exit 1; }

wait_http() { # url, what
  for _ in $(seq 1 240); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0
    sleep 0.5
  done
  die "timed out waiting for $2 ($1)"
}

dev_tab() { # prints the tab id of an open dev tab, if any
  herdr tab list | jq -r --arg l "$tab_label" '.result.tabs[] | select(.label == $l) | .tab_id'
}

# Containers, builds, migrations and a gateway token; writes .dev/env for `run`.
prepare() {
  for port in 4000 5173 8091 9100 9464 9465 50051; do
    if ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      die "port $port is already in use; stop the other dev stack first"
    fi
  done
  mkdir -p "$state"
  local env="$state/env"
  : >"$env"
  chmod 600 "$env"

  log "starting dev Postgres"
  "$root/scripts/dev-db.sh" up
  if [[ $sso == 1 ]]; then
    log "starting Keycloak and OpenFGA"
    "$root/scripts/dev-idp.sh" up | grep '^export ' >>"$env"
    {
      echo "export LUMEWARP_PUBLIC_URL=$ui_url"
      echo "export LUMEWARP_SESSION_COOKIE_SECURE=false"
      if [[ $authz == openfga ]]; then echo "export LUMEWARP_AUTHZ_BACKEND=openfga"; fi
    } >>"$env"
  elif [[ $authz != builtin ]]; then
    die "DEV_AUTHZ=$authz needs DEV_SSO=1"
  fi

  log "building mock upstream and gateway"
  cargo build --release -q --manifest-path "$root/bench/mock-upstream/Cargo.toml"
  cargo build --release -q --manifest-path "$root/gateway/Cargo.toml"

  log "migrating the database"
  (
    cd "$root/control-plane"
    uv sync -q
    uv run -q lumewarp-control-plane migrate
  )
  local token
  token="$(cd "$root/control-plane" && uv run -q lumewarp-control-plane bootstrap \
    --gateway-id "$gateway_id" 2>/dev/null)"
  {
    echo "export LUMEWARP_DEV_MODE=true"
    printf 'export LUMEWARP_BREAK_GLASS_PASSWORD=%q\n' "$break_glass_password"
    echo "export LUMEWARP_ADMIN_GROUP=lumewarp-admins"
    echo "export LUMEWARP_GATEWAY_TOKEN=$token"
  } >>"$env"

  if [[ ! -d $root/ui/node_modules ]]; then
    log "installing UI dependencies"
    pnpm --dir "$root/ui" install --silent
  fi
}

run_service() { # mock | cp | gw | ui
  # shellcheck source=/dev/null
  source "$state/env"
  case "$1" in
    mock) exec "$root/bench/mock-upstream/target/release/mock-upstream" --listen 127.0.0.1:9100 ;;
    cp) cd "$root/control-plane" && exec uv run lumewarp-control-plane serve ;;
    gw)
      wait_http "$cp_url/healthz" "control plane" # avoids connect-retry warnings at startup
      exec "$root/gateway/target/release/lumewarp" --control-plane http://127.0.0.1:50051 \
        --gateway-token-env LUMEWARP_GATEWAY_TOKEN --state-dir "$state/gateway" \
        --scope default --gateway-id "$gateway_id"
      ;;
    ui) exec pnpm --dir "$root/ui" dev --host 127.0.0.1 --port 5173 --strictPort ;;
    *) die "unknown service: $1" ;;
  esac
}

api() { # method, path, [json]
  curl -fsS -X "$1" -b "$state/admin-cookie" -H "X-Lumewarp-CSRF: $admin_csrf" \
    -H 'Content-Type: application/json' ${3:+--data-binary "$3"} "$cp_url/api/v1$2"
}

seed() {
  wait_http "$cp_url/healthz" "control plane"
  touch "$state/admin-cookie"
  chmod 600 "$state/admin-cookie"
  curl -fsS -c "$state/admin-cookie" -H 'Content-Type: application/json' \
    --data-binary "$(jq -nc --arg password "$break_glass_password" '{password:$password}')" \
    "$cp_url/api/v1/session/break-glass" >/dev/null
  admin_csrf="$(curl -fsS -b "$state/admin-cookie" "$cp_url/api/v1/session" | jq -r .csrf_token)"
  [[ $(api GET /upstreams | jq '.items | length') == 0 ]] || return 0
  log "seeding demo upstream, router, team and key"
  local up dep team sa
  up="$(api POST /upstreams '{"name":"mock","kind":"openai_compatible","base_url":"http://127.0.0.1:9100/v1"}' | jq -r .id)"
  dep="$(api POST /models "{\"name\":\"mock-local\",\"upstream_id\":\"$up\",\"upstream_model\":\"mock\"}" | jq -r .id)"
  api POST /routers "{\"name\":\"demo\",\"members\":[{\"model_id\":\"$dep\"}]}" >/dev/null
  team="$(api POST /teams '{"name":"demo","model_access":"all"}' | jq -r .id)"
  sa="$(api POST "/teams/$team/service-accounts" '{"name":"chat"}' | jq -r .id)"
  api POST "/teams/$team/keys" "{\"name\":\"demo\",\"service_account_id\":\"$sa\"}" \
    | jq -r .secret >"$state/demo-key"
}

summary() { # stop hint
  wait_http "http://127.0.0.1:9464/readyz" "gateway"
  wait_http "$ui_url" "UI"
  cat <<EOF

  UI        $ui_url   (SSO: alice/bob/carol; alice belongs to lumewarp-admins)
  Emergency login uses LUMEWARP_BREAK_GLASS_PASSWORD (development default configured).
  API       $cp_url/api/v1
  gateway   http://127.0.0.1:4000/v1   (model demo)
  metrics   http://127.0.0.1:9464/metrics (gateway), http://127.0.0.1:9465/metrics (CP)
  Demo API key is stored in .dev/demo-key (not printed).
  $1

EOF
}

case "${1:-all}" in
  run)
    run_service "${2:?usage: $0 run mock|cp|gw|ui}"
    ;;
  all)
    prepare
    trap 'trap - EXIT INT TERM; log stopping; kill 0 2>/dev/null || true' EXIT INT TERM
    for svc in mock cp gw ui; do
      ("$self" run "$svc" 2>&1 | sed -u "s/^/[$svc] /") &
    done
    seed
    summary "Ctrl+C stops everything (containers keep running: make dev-down)."
    wait
    ;;
  tab)
    [[ ${HERDR_ENV:-} == 1 ]] || die "not inside herdr; use: $0 all"
    if [[ -z ${DEV_IN_TAB:-} ]]; then
      # Called from another pane: open the tab and continue in its first pane.
      existing="$(dev_tab)"
      if [[ -n $existing ]]; then
        herdr tab focus "$existing" >/dev/null
        die "a $tab_label tab is already open ($existing); make dev-stop closes it"
      fi
      herdr tab create --label "$tab_label" --cwd "$root" --env DEV_IN_TAB=1 \
        --env "DEV_AUTHZ=$authz" --env "LUMEWARP_BREAK_GLASS_PASSWORD=$break_glass_password" \
        --focus | jq -r '.result.root_pane.pane_id' | {
        read -r pane
        herdr pane run "$pane" "$self tab" >/dev/null
      }
      exit 0
    fi
    prepare
    # Layout: this pane (setup + summary) | cp | gw on the right; mock and ui below this one.
    split() { # pane, direction, service -> new pane id
      local id
      id="$(herdr pane split "$1" --direction "$2" --no-focus --cwd "$root" \
        | jq -r '.result.pane.pane_id')"
      herdr pane run "$id" "$self run $3" >/dev/null
      echo "$id"
    }
    me="$HERDR_PANE_ID"
    cp_pane="$(split "$me" right cp)"
    split "$cp_pane" down gw >/dev/null
    mock_pane="$(split "$me" down mock)"
    split "$mock_pane" right ui >/dev/null
    seed
    summary "make dev-stop closes this tab (containers keep running: make dev-down)."
    ;;
  stop)
    tab="$(dev_tab)"
    [[ -n $tab ]] || die "no $tab_label tab is open"
    herdr tab close "$tab" >/dev/null
    log "closed $tab"
    ;;
  *)
    sed -n '2,19p' "$self" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

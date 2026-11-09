#!/usr/bin/env bash
# Local dev preview: dev Postgres (+ Keycloak/OpenFGA), mock upstream, control plane,
# gateway and the Vite UI.
#
#   scripts/dev.sh tab        inside herdr: open a `lumewarp-dev` tab, one pane per service
#   scripts/dev.sh all        everything in this terminal with prefixed logs; Ctrl+C stops it
#   scripts/dev.sh stop       close the herdr tab (containers keep running)
#   scripts/dev.sh run <svc>  run one service in the foreground (mock, mcp, a2a, cp, gw, ui)
#
# `make dev` picks `tab` inside herdr and `all` elsewhere.
#
#   DEV_AUTHZ=openfga         authorization backend (default builtin)
#   LUMEWARP_BREAK_GLASS_PASSWORD  override the development emergency password
#   DEV_DATABASE=name         use this database in the dev Postgres, created if missing
#   DEV_OPENWEBUI=0           do not start Open WebUI
#
# Ports: UI 5173 (Vite, proxies /api to the CP), CP 8091 (+ gRPC 50051, metrics 9465),
# gateway 4000 (+ admin 9464), mock upstream 9100, example MCP 9110, example A2A 9120,
# Open WebUI 3000.
# An empty database gets a model `mock-local`, a router `demo` over it, an alias
# `demo-latest` to the router, a team `demo` and one key; the key secret is kept in
# .dev/demo-key. One with no MCP server or A2A agent gets `example` and `helper`,
# the example servers above. Each start issues a fresh gateway token (.dev/env).
# Open WebUI runs in a container (no login) with the demo key for both the gateway's
# OpenAI API and its aggregate MCP endpoint; its chats live in a Docker volume. Its
# connections come from the environment on every start, so settings changed in its
# admin panel do not survive a restart.
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
openwebui="${DEV_OPENWEBUI:-1}"
openwebui_port=3000
openwebui_image="ghcr.io/open-webui/open-webui:v0.12.5"
openwebui_name="lumewarp-dev-openwebui"
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
  local ports=(4000 5173 8091 9100 9110 9120 9464 9465 50051)
  if [[ $openwebui == 1 ]]; then
    # A previous run's container is replaced below; drop it before checking the port.
    docker rm -f "$openwebui_name" >/dev/null 2>&1 || true
    ports+=("$openwebui_port")
  fi
  for port in "${ports[@]}"; do
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
      echo "export LUMEWARP_SECRET_KEY=dev-only-lumewarp-secret-key-0123456789abcdef"
      if [[ $authz == openfga ]]; then echo "export LUMEWARP_AUTHZ_BACKEND=openfga"; fi
    } >>"$env"
  elif [[ $authz != builtin ]]; then
    die "DEV_AUTHZ=$authz needs DEV_SSO=1"
  fi

  log "building mock upstream, example MCP/A2A servers and gateway"
  cargo build --release -q --bins --manifest-path "$root/bench/mock-upstream/Cargo.toml"
  cargo build --release -q --manifest-path "$root/gateway/Cargo.toml"

  log "building the example WASM plugins"
  bash "$root/gateway/plugins/build.sh" >/dev/null

  if [[ -n ${DEV_DATABASE:-} ]]; then
    [[ $DEV_DATABASE =~ ^[a-z][a-z0-9_]*$ ]] || die "DEV_DATABASE must be a plain lowercase name"
    log "using database $DEV_DATABASE"
    local psql=(docker exec -i lumewarp-dev-postgres-1 psql -U lumewarp -d lumewarp -Atq)
    if [[ -z "$("${psql[@]}" -c "SELECT 1 FROM pg_database WHERE datname = '$DEV_DATABASE'")" ]]; then
      "${psql[@]}" -c "CREATE DATABASE $DEV_DATABASE"
    fi
    export LUMEWARP_DATABASE_URL="postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/$DEV_DATABASE"
    echo "export LUMEWARP_DATABASE_URL=$LUMEWARP_DATABASE_URL" >>"$env"
  fi

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
    echo "export LUMEWARP_PLAYGROUND_GATEWAY_URL=http://127.0.0.1:4000"
    # Seals upstream API keys entered in the UI; the gateway opens them with the same key.
    echo "export LUMEWARP_ENCRYPTION_KEY=dev-only-lumewarp-encryption-key-0123456789"
    printf 'export LUMEWARP_BREAK_GLASS_PASSWORD=%q\n' "$break_glass_password"
    echo "export LUMEWARP_INITIAL_ADMIN_EMAIL=alice@example.com"
    echo "export LUMEWARP_GATEWAY_TOKEN=$token"
    # Without an id key the gateway answers 503 on /mcp and /a2a.
    echo "export LUMEWARP_GATEWAY_ID_KEY=ZGV2LW9ubHktbHVtZXdhcnAtZ2F0ZXdheS1pZC1rZXktMDEyMzQ1Njc4OQ=="
    echo "export LUMEWARP_GATEWAY_PUBLIC_URL=http://127.0.0.1:4000"
    echo "export LUMEWARP_ROUTER_PLUGIN_DIR=$root/gateway/target/plugins/router"
    echo "export LUMEWARP_AUTH_PLUGIN_DIR=$root/gateway/target/plugins/auth"
    echo "export LUMEWARP_A2A_MIDDLEWARE_DIR=$root/gateway/target/plugins/a2a-middleware"
  } >>"$env"

  if [[ ! -d $root/ui/node_modules ]]; then
    log "installing UI dependencies"
    pnpm --dir "$root/ui" install --silent
  fi

  if [[ $openwebui == 1 ]] && ! docker image inspect "$openwebui_image" >/dev/null 2>&1; then
    log "pulling $openwebui_image (first run only, several GB)"
    docker pull -q "$openwebui_image" >/dev/null
  fi
}

# Open WebUI on the host network, bound to loopback, using the demo key. Runs after
# `seed`, which writes the key on an empty database.
openwebui_up() {
  [[ $openwebui == 1 ]] || return 0
  if [[ ! -s $state/demo-key ]]; then
    log "skipping Open WebUI: .dev/demo-key is missing (seeded only on an empty database)"
    return 0
  fi
  local key env="$state/openwebui.env"
  key="$(<"$state/demo-key")"
  : >"$env"
  chmod 600 "$env"
  # A Docker env file takes each value literally, so the JSON needs no quoting.
  {
    echo "PORT=$openwebui_port"
    echo "HOST=127.0.0.1"
    echo "WEBUI_AUTH=False"
    echo "WEBUI_SECRET_KEY=dev-only-open-webui-secret"
    echo "ENABLE_PERSISTENT_CONFIG=False"
    echo "ENABLE_OLLAMA_API=False"
    echo "OPENAI_API_BASE_URL=http://127.0.0.1:4000/v1"
    echo "OPENAI_API_KEY=$key"
    jq -nc --arg key "$key" '[{
      type: "mcp", url: "http://127.0.0.1:4000/mcp", path: "", auth_type: "none", key: "",
      headers: {"X-Lumewarp-Key": $key}, config: {enable: true},
      info: {id: "lumewarp", name: "Lumewarp", description: "Lumewarp gateway, every MCP server the demo key may use"}
    }]' | sed 's/^/TOOL_SERVER_CONNECTIONS=/'
    echo "ENABLE_EVALUATION_ARENA_MODELS=False"
    echo "OFFLINE_MODE=True"
    echo "ANONYMIZED_TELEMETRY=False"
    echo "DO_NOT_TRACK=True"
    echo "SCARF_NO_ANALYTICS=True"
  } >>"$env"
  log "starting Open WebUI"
  docker run -d --name "$openwebui_name" --network host --env-file "$env" \
    -v lumewarp-dev-openwebui:/app/backend/data "$openwebui_image" >/dev/null
}

run_service() { # mock | mcp | a2a | cp | gw | ui
  # shellcheck source=/dev/null
  source "$state/env"
  case "$1" in
    mock) exec "$root/bench/mock-upstream/target/release/mock-upstream" --listen 127.0.0.1:9100 ;;
    mcp) exec "$root/bench/mock-upstream/target/release/mcp-a2a-mock" mcp --listen 127.0.0.1:9110 ;;
    a2a) exec "$root/bench/mock-upstream/target/release/mcp-a2a-mock" a2a --listen 127.0.0.1:9120 ;;
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
  if [[ $(api GET /mcp-servers | jq '.items | length') == 0 ]]; then
    log "seeding the example MCP server"
    api POST /mcp-servers '{"name":"example","url":"http://127.0.0.1:9110/mcp","auth":{"plugin":"none","params":{}}}' >/dev/null
  fi
  if [[ $(api GET /a2a-agents | jq '.items | length') == 0 ]]; then
    log "seeding the example A2A agent"
    api POST /a2a-agents '{"name":"helper","url":"http://127.0.0.1:9120/a2a","auth":{"plugin":"none","params":{}}}' >/dev/null
  fi
  [[ $(api GET /upstreams | jq '.items | length') == 0 ]] || return 0
  log "seeding demo upstream, model, router, alias, team and key"
  local org up model router team sa
  org="$(api GET /orgs | jq -r '.items | map(select(.name == "default")) | first | .id // empty')"
  [[ -n "$org" ]] || org="$(api POST /orgs '{"name":"default"}' | jq -r .id)"
  up="$(api POST /upstreams '{"name":"mock","kind":"openai_compatible","base_url":"http://127.0.0.1:9100/v1"}' | jq -r .id)"
  model="$(api POST /models "{\"name\":\"mock-local\",\"upstream_id\":\"$up\",\"upstream_model\":\"mock\"}" | jq -r .id)"
  router="$(api POST /routers "{\"name\":\"demo\",\"members\":[{\"model_id\":\"$model\"}]}" | jq -r .id)"
  api POST /aliases "{\"name\":\"demo-latest\",\"target\":\"$router\"}" >/dev/null
  team="$(api POST /teams "{\"name\":\"demo\",\"org_id\":\"$org\",\"model_access\":\"all\"}" | jq -r .id)"
  sa="$(api POST "/teams/$team/service-accounts" '{"name":"chat"}' | jq -r .id)"
  api POST "/teams/$team/keys" "{\"name\":\"demo\",\"service_account_id\":\"$sa\"}" \
    | jq -r .secret >"$state/demo-key"
}

summary() { # stop hint
  wait_http "http://127.0.0.1:9464/readyz" "gateway"
  wait_http "$ui_url" "UI"
  wait_http "http://127.0.0.1:9110/_mock/headers" "example MCP server"
  wait_http "http://127.0.0.1:9120/health" "example A2A agent"
  local webui=""
  if [[ $openwebui == 1 && -s $state/demo-key ]]; then
    webui="  Open WebUI http://127.0.0.1:$openwebui_port   (demo key; MCP tools: Lumewarp in the chat's tool menu)"
    # Its first start migrates its own database and can take minutes; never fail on it.
    local up=0
    for _ in $(seq 1 600); do
      curl -fsS -o /dev/null "http://127.0.0.1:$openwebui_port/health" 2>/dev/null && { up=1; break; }
      sleep 0.5
    done
    [[ $up == 1 ]] || webui="  Open WebUI not ready after 5 minutes: docker logs $openwebui_name"
  fi
  cat <<EOF

  UI        $ui_url   (SSO: alice/bob/carol; alice becomes proxy_admin on first login)
  Emergency login uses LUMEWARP_BREAK_GLASS_PASSWORD (development default configured).
  API       $cp_url/api/v1
  gateway   http://127.0.0.1:4000/v1   (names: mock-local, demo, demo-latest)
  MCP       http://127.0.0.1:9110/mcp   (example upstream; control /_mock/)
  A2A       http://127.0.0.1:9120/a2a   (agent card /.well-known/agent-card.json)
  metrics   http://127.0.0.1:9464/metrics (gateway), http://127.0.0.1:9465/metrics (CP)
  Demo API key is stored in .dev/demo-key (not printed).
$webui
  $1

EOF
}

case "${1:-all}" in
  run)
    run_service "${2:?usage: $0 run mock|mcp|a2a|cp|gw|ui}"
    ;;
  all)
    prepare
    trap 'trap - EXIT INT TERM; log stopping; kill 0 2>/dev/null || true' EXIT INT TERM
    for svc in mock mcp a2a cp gw ui; do
      ("$self" run "$svc" 2>&1 | sed -u "s/^/[$svc] /") &
    done
    seed
    openwebui_up
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
        --env "DEV_DATABASE=${DEV_DATABASE:-}" --env "DEV_OPENWEBUI=$openwebui" \
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
    ui_pane="$(split "$mock_pane" right ui)"
    mcp_pane="$(split "$ui_pane" down mcp)"
    split "$mcp_pane" down a2a >/dev/null
    seed
    openwebui_up
    summary "make dev-stop closes this tab (containers keep running: make dev-down)."
    ;;
  stop)
    tab="$(dev_tab)"
    [[ -n $tab ]] || die "no $tab_label tab is open"
    herdr tab close "$tab" >/dev/null
    log "closed $tab"
    ;;
  *)
    sed -n '2,20p' "$self" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

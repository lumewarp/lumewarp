# Local development stack: dev Postgres, Valkey, Keycloak (+ OpenFGA with DEV_AUTHZ=openfga),
# the mock upstream and example MCP/A2A servers, the control plane, four gateways (two per
# zone) and the Vite UI; with DEV_EXTRAS=1 also Prometheus, Grafana and Open WebUI.
#
#   make dev          inside herdr a `lumewarp-dev` tab with one pane per service, elsewhere
#                     everything in this terminal with prefixed logs
#   make dev-all      everything in this terminal; Ctrl+C stops it and removes the containers
#   make dev-stop     close the herdr tab and remove the containers
#   make dev-down     remove the dev containers (Postgres data and Open WebUI chats stay)
#   make dev-reset    remove the containers and their volumes (a fresh database)
#   make dev-run-<s>  one service in the foreground: mock mcp a2a cp gw1 gw2 gw3 gw4 ui
#   make dev-seed     seed the running stack again
#
#   DEV_AUTHZ=openfga   authorization backend (default builtin); only then is OpenFGA started
#   DEV_DATABASE=name   use this database in the dev Postgres, created if missing
#   DEV_EXTRAS=1        also start Prometheus, Alertmanager (webhook to the control plane),
#                       Grafana (shipped rules and dashboards) and Open WebUI with the demo
#                       key; off by default
#   LUMEWARP_BREAK_GLASS_PASSWORD   the emergency password (default dev-break-glass)
#
# Ports: UI 5173 (proxies /api to the CP), CP 8091 (+ gRPC 50051, metrics 9465); zone
# `default` gateways on 4000 and 4001 (admin 9464, 9466), zone `prod` on 4002 and 4003
# (admin 9468, 9470); mock upstream 9100, example MCP 9110, example A2A 9120, Postgres
# 55432, Valkey 56379, Keycloak 18080, OpenFGA 18081; extras: Prometheus 19090,
# Alertmanager 19093, Grafana 13000, Open WebUI 3000.
# Every start runs `lumewarp-control-plane seed-dev`, which keeps the demo (`mock-local`,
# router `demo`, alias `demo-latest`, team `demo`, keys in .dev/demo-key and
# .dev/demo-key-prod) and the larger seed catalog with 30 days of usage, plus demo incidents
# (one still firing, so the status banner shows).

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := dev

ROOT := $(CURDIR)
DEV := $(ROOT)/.dev
COMPOSE := docker compose -f $(ROOT)/deploy/dev/compose.yaml
SERVICES := mock mcp a2a cp gw1 gw2 gw3 gw4 ui
PORTS := 4000 4001 4002 4003 5173 8091 9100 9110 9120 9464 9465 9466 9468 9470 50051
DEV_AUTHZ ?= builtin
DEV_EXTRAS ?= 0
DEV_DATABASE ?=

# Development-only settings shared by every service. None of them protects anything real.
export PATH := $(HOME)/.cargo/bin:$(PATH)
export LUMEWARP_DEV_MODE := true
export LUMEWARP_PUBLIC_URL := http://127.0.0.1:5173
export LUMEWARP_SESSION_COOKIE_SECURE := false
export LUMEWARP_SECRET_KEY := dev-only-lumewarp-secret-key-0123456789abcdef
# Seals upstream API keys entered in the UI; the gateway opens them with the same key.
export LUMEWARP_ENCRYPTION_KEY := dev-only-lumewarp-encryption-key-0123456789
export LUMEWARP_BREAK_GLASS_PASSWORD ?= dev-break-glass
export LUMEWARP_INITIAL_ADMIN_EMAIL := alice@example.com
export LUMEWARP_OIDC_ISSUER := http://127.0.0.1:18080/realms/lumewarp
export LUMEWARP_OIDC_CLIENT_ID := lumewarp-cp
export LUMEWARP_OIDC_CLIENT_SECRET := lumewarp-dev-secret
export LUMEWARP_SSO_DISPLAY_NAME := Keycloak
# One fixed token per zone, shared by that zone's gateways; it also declares the zone.
export LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN := dev-only-gateway-token-zone-default-0123456789
export LUMEWARP_GATEWAY_ZONE_PROD_TOKEN := dev-only-gateway-token-zone-prod-0123456789ab
# Without an id key the gateway answers 503 on /mcp and /a2a.
export LUMEWARP_GATEWAY_ID_KEY := ZGV2LW9ubHktbHVtZXdhcnAtZ2F0ZXdheS1pZC1rZXktMDEyMzQ1Njc4OQ==
# The Alertmanager receiver; deploy/dev/alertmanager.yaml sends the same token.
export LUMEWARP_ALERTMANAGER_TOKEN := dev-only-alertmanager-token-0123456789abcdef
export LUMEWARP_GATEWAY_PUBLIC_URL := http://127.0.0.1:4000
export LUMEWARP_PLAYGROUND_GATEWAY_URL := http://127.0.0.1:4000
export LUMEWARP_SHARED_LIMITS_REDIS_URL := redis://127.0.0.1:56379/0
export LUMEWARP_ROUTER_PLUGIN_DIR := $(ROOT)/gateway/target/plugins/router
export LUMEWARP_AUTH_PLUGIN_DIR := $(ROOT)/gateway/target/plugins/auth
export LUMEWARP_A2A_MIDDLEWARE_DIR := $(ROOT)/gateway/target/plugins/a2a-middleware

ifeq ($(DEV_AUTHZ),openfga)
export LUMEWARP_AUTHZ_BACKEND := openfga
export LUMEWARP_OPENFGA_URL := http://127.0.0.1:18081
export LUMEWARP_OPENFGA_STORE := lumewarp
DEPS := postgres redis keycloak openfga
else ifeq ($(DEV_AUTHZ),builtin)
DEPS := postgres redis keycloak
else
$(error DEV_AUTHZ=$(DEV_AUTHZ): use builtin or openfga)
endif

ifeq ($(DEV_EXTRAS),1)
PORTS += 3000 13000 19090
endif

ifneq ($(DEV_DATABASE),)
ifeq ($(shell [[ "$(DEV_DATABASE)" =~ ^[a-z][a-z0-9_]*$$ ]] && echo ok),)
$(error DEV_DATABASE must be a plain lowercase name)
endif
export LUMEWARP_DATABASE_URL := postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/$(DEV_DATABASE)
endif

# $(call wait_http,url,what): poll for two minutes.
wait_http = (for _ in $$(seq 240); do curl -fsS -o /dev/null $(1) 2>/dev/null && exit 0; sleep 0.5; done; \
	echo "timed out waiting for $(2) ($(1))" >&2; exit 1)
log = printf '\033[1m[dev]\033[0m %s\n'
TAB := lumewarp-dev
dev_tab = herdr tab list | jq -r '.result.tabs[] | select(.label == "$(TAB)") | .tab_id'

.PHONY: dev dev-all dev-tab dev-tab-panes dev-stop dev-down dev-reset dev-prepare dev-ports \
	dev-deps dev-build dev-migrate dev-seed dev-extras dev-info dev-after

dev:
ifeq ($(HERDR_ENV),1)
	@$(MAKE) --no-print-directory dev-tab
else
	@$(MAKE) --no-print-directory dev-all
endif

# ----------------------------------------------------------------------------- preparation

dev-prepare: dev-ports dev-deps dev-build dev-migrate

dev-ports:
	@for port in $(PORTS); do \
	  if ss -ltn "sport = :$$port" 2>/dev/null | grep -q LISTEN; then \
	    echo "port $$port is already in use; stop the other dev stack first" >&2; exit 1; fi; \
	done

dev-deps:
	@$(log) "starting $(DEPS)"
	@$(COMPOSE) --profile idp --profile openfga up -d --wait $(DEPS)
	@$(call wait_http,$(LUMEWARP_OIDC_ISSUER)/.well-known/openid-configuration,Keycloak)
ifeq ($(DEV_AUTHZ),openfga)
	@$(call wait_http,$(LUMEWARP_OPENFGA_URL)/healthz,OpenFGA)
	@cd control-plane && env -u LUMEWARP_OPENFGA_STORE_ID -u LUMEWARP_OPENFGA_MODEL_ID \
	  uv run -q lumewarp-control-plane openfga init
endif
ifneq ($(DEV_DATABASE),)
	@psql() { docker exec -i lumewarp-dev-postgres-1 psql -U lumewarp -d lumewarp -Atq -c "$$1"; }; \
	[[ -n "$$(psql "SELECT 1 FROM pg_database WHERE datname = '$(DEV_DATABASE)'")" ]] || \
	  psql "CREATE DATABASE $(DEV_DATABASE)"
endif

dev-build:
	@$(log) "building the mock upstream, example MCP/A2A servers, gateway and WASM plugins"
	@cargo build --release -q --bins --manifest-path bench/mock-upstream/Cargo.toml
	@cargo build --release -q --manifest-path gateway/Cargo.toml
	@bash gateway/plugins/build.sh >/dev/null
	@cd control-plane && uv sync -q
	@[[ -d ui/node_modules ]] || pnpm --dir ui install --silent

dev-migrate:
	@$(log) "migrating the database"
	@cd control-plane && uv run -q lumewarp-control-plane migrate

# ----------------------------------------------------------------------------- services

dev-run-mock:
	@exec bench/mock-upstream/target/release/mock-upstream --listen 127.0.0.1:9100
dev-run-mcp:
	@exec bench/mock-upstream/target/release/mcp-a2a-mock mcp --listen 127.0.0.1:9110
dev-run-a2a:
	@exec bench/mock-upstream/target/release/mcp-a2a-mock a2a --listen 127.0.0.1:9120
dev-run-cp:
	@cd control-plane && exec uv run lumewarp-control-plane serve
dev-run-ui:
	@exec pnpm --dir ui dev --host 127.0.0.1 --port 5173 --strictPort
# gw1 and gw2 serve zone `default`, gw3 and gw4 zone `prod`.
dev-run-gw%:
	@$(call wait_http,http://127.0.0.1:8091/healthz,control plane)
	@n=$*; zone=default; (( n <= 2 )) || zone=prod; mkdir -p $(DEV); \
	printf "server:\n  listen: '127.0.0.1:%s'\n  admin_listen: '127.0.0.1:%s'\n" \
	  $$((3999 + n)) $$((9462 + 2 * n)) >$(DEV)/gateway$$n.yaml; \
	exec gateway/target/release/lumewarp --control-plane http://127.0.0.1:50051 \
	  --gateway-token-env "LUMEWARP_GATEWAY_ZONE_$${zone^^}_TOKEN" --state-dir $(DEV)/gateway$$n \
	  --zone $$zone --local-config $(DEV)/gateway$$n.yaml

# A service with its log lines prefixed, for dev-all.
dev-log-%:
	@$(MAKE) --no-print-directory dev-run-$* 2>&1 | sed -u "s/^/[$*] /"

# ----------------------------------------------------------------------------- after start

dev-seed:
	@$(call wait_http,http://127.0.0.1:8091/healthz,control plane)
	@$(log) "seeding the demo, the development catalog and 30 days of usage"
	@cd control-plane && uv run -q lumewarp-control-plane seed-dev

dev-extras:
ifeq ($(DEV_EXTRAS),1)
	@$(log) "starting Prometheus, Alertmanager, Grafana and Open WebUI"
	@DEV_DEMO_KEY="$$(<$(DEV)/demo-key)" $(COMPOSE) --profile extras up -d \
	  prometheus alertmanager grafana openwebui
endif

dev-info:
	@for url in 9464 9466 9468 9470; do $(call wait_http,http://127.0.0.1:$$url/readyz,gateway); done
	@$(call wait_http,http://127.0.0.1:5173,UI)
	@echo; \
	echo "  UI        http://127.0.0.1:5173   (SSO: alice/bob/carol; alice becomes proxy_admin on first login)"; \
	echo "  API       http://127.0.0.1:8091/api/v1   (emergency login: LUMEWARP_BREAK_GLASS_PASSWORD)"; \
	echo "  gateway   zone default: http://127.0.0.1:4000/v1 and :4001/v1   (key .dev/demo-key)"; \
	echo "            zone prod:    http://127.0.0.1:4002/v1 and :4003/v1   (key .dev/demo-key-prod)"; \
	echo "  MCP       http://127.0.0.1:9110/mcp    A2A http://127.0.0.1:9120/a2a"; \
	echo "  metrics   gateways :9464 :9466 :9468 :9470, control plane :9465"; \
	if [[ "$(DEV_EXTRAS)" == 1 ]]; then \
	  echo "  Prometheus http://127.0.0.1:19090   Alertmanager http://127.0.0.1:19093"; \
	  echo "  Grafana   http://127.0.0.1:13000 (Lumewarp folder)"; \
	  echo "  Open WebUI http://127.0.0.1:3000   (demo key; its first start takes a few minutes)"; fi; \
	echo

dev-after: dev-seed dev-extras
	@$(MAKE) --no-print-directory dev-info

# ----------------------------------------------------------------------------- whole stack

dev-all: dev-prepare
	@trap 'trap - EXIT INT TERM; echo "[dev] stopping"; $(MAKE) --no-print-directory dev-down' EXIT INT TERM; \
	$(MAKE) --no-print-directory -j$(words $(SERVICES) x) \
	  $(addprefix dev-log-,$(SERVICES)) dev-after

# Opens the tab from another pane; its first pane prepares and starts the services.
dev-tab:
	@[[ "$${HERDR_ENV:-}" == 1 ]] || { echo "not inside herdr; use make dev-all" >&2; exit 1; }
	@tab="$$($(dev_tab))"; if [[ -n "$$tab" ]]; then \
	  herdr tab focus "$$tab" >/dev/null; echo "a $(TAB) tab is already open; make dev-stop closes it" >&2; exit 1; fi
	@pane="$$(herdr tab create --label $(TAB) --cwd $(ROOT) --focus \
	  --env "DEV_AUTHZ=$(DEV_AUTHZ)" --env "DEV_DATABASE=$(DEV_DATABASE)" \
	  --env "DEV_EXTRAS=$(DEV_EXTRAS)" \
	  --env "LUMEWARP_BREAK_GLASS_PASSWORD=$(LUMEWARP_BREAK_GLASS_PASSWORD)" \
	  | jq -r '.result.root_pane.pane_id')"; \
	herdr pane run "$$pane" "make dev-tab-panes" >/dev/null

# Layout: this pane (setup + summary) | cp, gw1..gw4 on the right; mock, ui, mcp, a2a below.
dev-tab-panes: dev-prepare
	@split() { local id; id="$$(herdr pane split "$$1" --direction "$$2" --no-focus --cwd $(ROOT) \
	    | jq -r '.result.pane.pane_id')"; herdr pane run "$$id" "make dev-run-$$3" >/dev/null; echo "$$id"; }; \
	cp="$$(split "$$HERDR_PANE_ID" right cp)"; gw1="$$(split "$$cp" down gw1)"; \
	gw2="$$(split "$$gw1" down gw2)"; gw3="$$(split "$$gw2" down gw3)"; split "$$gw3" down gw4 >/dev/null; \
	mock="$$(split "$$HERDR_PANE_ID" down mock)"; ui="$$(split "$$mock" right ui)"; \
	mcp="$$(split "$$ui" down mcp)"; split "$$mcp" down a2a >/dev/null
	@$(MAKE) --no-print-directory dev-after
	@$(log) "make dev-stop closes this tab and removes the containers"

# Removes the containers first: run from inside the tab, closing it ends this command too.
dev-stop:
	@tab="$$($(dev_tab))"; [[ -n "$$tab" ]] || { echo "no $(TAB) tab is open" >&2; exit 1; }; \
	$(MAKE) --no-print-directory dev-down; herdr tab close "$$tab" >/dev/null

dev-down:
	@$(COMPOSE) --profile '*' down --remove-orphans

dev-reset:
	@$(COMPOSE) --profile '*' down --remove-orphans --volumes

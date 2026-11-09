# Local dev helpers. See scripts/dev.sh for the settings (DEV_SSO, DEV_AUTHZ, ...).
.PHONY: dev dev-all dev-stop dev-down

# Inside herdr: a new `lumewarp-dev` tab with one pane per service; elsewhere one terminal.
dev:
ifeq ($(HERDR_ENV),1)
	@scripts/dev.sh tab
else
	@scripts/dev.sh all
endif

# Everything in this terminal with prefixed logs.
dev-all:
	@scripts/dev.sh all

# Close the herdr dev tab (stops the services; containers keep running).
dev-stop:
	@scripts/dev.sh stop

# Stop the dev containers (Postgres and Open WebUI keep their volumes; scripts/dev-db.sh
# reset wipes Postgres).
dev-down:
	@docker rm -f lumewarp-dev-openwebui >/dev/null 2>&1 || true
	@scripts/dev-idp.sh down
	@scripts/dev-db.sh down

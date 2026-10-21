#!/usr/bin/env bash
# Manage the Lumewarp dev Postgres (127.0.0.1:55432).
#   scripts/dev-db.sh up     start and wait until healthy
#   scripts/dev-db.sh down   stop (keeps data)
#   scripts/dev-db.sh reset  stop, delete the volume, start fresh
set -euo pipefail

compose_file="$(cd "$(dirname "$0")/.." && pwd)/deploy/dev/compose.yaml"
dc() { docker compose -f "$compose_file" "$@"; }

case "${1:-}" in
  up) dc up -d --wait postgres ;;
  down) dc down ;;
  reset)
    dc down -v
    dc up -d --wait postgres
    ;;
  *)
    echo "usage: $0 up|down|reset" >&2
    exit 2
    ;;
esac

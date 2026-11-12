#!/usr/bin/env bash
# Regenerates the Admin UI screenshots in docs/public/screenshots/ with the
# Playwright spec ui/e2e/docs-shots.spec.ts. Needs what `pnpm test:e2e` needs: the
# dev Postgres (or LUMEWARP_TEST_DATABASE_URL), uv and the ui/ dependencies. The
# spec starts its own control plane on a fresh database, so the pictures do not
# depend on local state.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/docs/public/screenshots"
mkdir -p "$out"
cd "$root/ui"
pnpm build:cp
E2E_DOCS_SHOTS="$out" pnpm exec playwright test e2e/docs-shots.spec.ts
ls -1 "$out"

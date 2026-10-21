"""Admin REST API, mounted at /api/v1 (owner: C2).

Conventions (frozen, see .omh/runs/p1/plan.md):
- Auth: `security.CurrentPrincipal`; authorization via `rbac.require` / `rbac.authorize`.
- Lists return `{items, next_cursor}` and accept `?limit=&cursor=`.
- Errors are `{error: {code, message}}`: raise `errors.ApiError` (handlers in errors.py).
- Every mutation writes `audit.record(...)` and, if it changes compiled state,
  `outbox.enqueue_recompile(...)` in the same transaction.
- Secrets (`lw-` keys, `lwp-` PATs, `lwg-` gateway tokens) are returned once at creation
  and stored only as sha256 hashes.

Sub-routers are included into `router` here; app.py mounts `router` and nothing else.
"""

from fastapi import APIRouter

from lumewarp_control_plane.api import (
    audit,
    config,
    gateways,
    keys,
    memberships,
    orgs,
    projects,
    routing,
    teams,
    usage,
    users,
)

router = APIRouter(prefix="/api/v1")
router.include_router(orgs.router)
router.include_router(teams.router)
router.include_router(projects.router)
router.include_router(keys.router)
router.include_router(routing.router)
router.include_router(users.router)
router.include_router(memberships.router)
router.include_router(usage.router)
router.include_router(audit.router)
router.include_router(gateways.router)
router.include_router(config.router)

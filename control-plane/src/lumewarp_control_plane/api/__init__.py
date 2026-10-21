"""Admin REST API, mounted at /api/v1 (owner: C2).

Conventions (frozen, see .omh/runs/p1/plan.md):
- Auth: `security.CurrentPrincipal`; authorization via `rbac.require` / `rbac.authorize`.
- Lists return `{items, next_cursor}` and accept `?limit=&cursor=`.
- Errors are `{error: {code, message}}`: raise `errors.ApiError` (handlers in errors.py).
- Every mutation writes `audit.record(...)` and, if it changes compiled state,
  `outbox.enqueue_recompile(...)` in the same transaction.
- Secrets (`lw-` keys, `lwp-` PATs) are returned once at creation and never stored.

Sub-routers are included into `router` here; app.py mounts `router` and nothing else.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

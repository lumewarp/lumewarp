"""Admin REST API, mounted at /api/v1.

Conventions (frozen):
- Auth: handlers take `authz.Authz` (caller in `authz.principal`) and ask it
  `require` / `allowed`; unreadable resources answer 404, readable-but-denied answer 403.
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
    installation_admins,
    keys,
    members,
    routing,
    service_accounts,
    session,
    strategies,
    teams,
    usage,
    users,
)
from lumewarp_control_plane.api.schemas import ErrorBody

# Declared once so the OpenAPI document describes the real `{error: {code, message}}` body
# (and replaces FastAPI's default 422 `HTTPValidationError`) for every operation.
_ERROR_RESPONSES: dict[int | str, dict] = {
    status: {"model": ErrorBody, "description": description}
    for status, description in (
        (401, "Missing or invalid credentials"),
        (403, "Not allowed"),
        (404, "Not found"),
        (409, "Conflict"),
        (422, "Validation error"),
    )
}

router = APIRouter(prefix="/api/v1", responses=_ERROR_RESPONSES)
router.include_router(teams.router)
router.include_router(members.router)
router.include_router(service_accounts.router)
router.include_router(keys.router)
router.include_router(routing.router)
router.include_router(strategies.router)
router.include_router(users.router)
router.include_router(installation_admins.router)
router.include_router(usage.router)
router.include_router(audit.router)
router.include_router(gateways.router)
router.include_router(config.router)
router.include_router(session.router)

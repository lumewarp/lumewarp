"""Serve the built Admin UI (a static SPA) from package data.

`pnpm build:cp` in `ui/` copies `ui/dist` into `static/` next to this module. Routes:

- a file that exists under `static/` is served as is; Vite's hashed `assets/*` are immutable
  and cached for a year, everything else (notably `index.html`) is revalidated every time so a
  new build is picked up immediately;
- any other GET/HEAD path that looks like a client-side route (no file extension) gets
  `index.html`, so deep links and refreshes work;
- `/api/*`, `/healthz`, `/readyz` are never answered with the SPA: `/api/*` stays a JSON 404
  (see `create_app`) and the health routes are registered first.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from lumewarp_control_plane.errors import error_body

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
# Prefixes (first path segment) that belong to the backend, never to the SPA.
RESERVED = frozenset({"api", "healthz", "readyz", "docs", "redoc", "openapi.json"})


def _resolve(root: Path, rel: str) -> Path | None:
    """The file `rel` names inside `root`, or None (missing, a directory, or an escape attempt)."""
    candidate = (root / rel).resolve()
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None


def mount_static(app: FastAPI, root: Path | None = None) -> bool:
    """Register the SPA routes; returns False (serving nothing) if no UI build is present."""
    root = (root or STATIC_DIR).resolve()
    index = root / "index.html"
    if not index.is_file():
        log.warning("admin UI not built (%s missing); run `pnpm build:cp` in ui/", index)
        return False

    def serve(path: Path) -> Response:
        immutable = path.relative_to(root).parts[:1] == ("assets",)
        headers = {"Cache-Control": IMMUTABLE if immutable else REVALIDATE}
        return FileResponse(path, headers=headers, stat_result=path.stat())

    # The SPA is the router's last resort rather than a catch-all route, so routes added to the
    # app after this call (and every API route) still win.
    not_found = app.router.default

    async def spa(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            return await not_found(scope, receive, send)
        path = scope["path"].lstrip("/")
        response: Response
        if path.split("/", 1)[0] in RESERVED:
            response = JSONResponse(error_body("not_found", "Not Found"), status_code=404)
        elif (file := _resolve(root, path)) is not None:
            response = serve(file)
        elif Path(path).suffix:
            # A missing asset must be a 404, not an HTML page that fails to parse as JS/CSS.
            response = JSONResponse(error_body("not_found", "Not Found"), status_code=404)
        else:
            response = serve(index)
        await response(scope, receive, send)

    app.router.default = spa
    return True

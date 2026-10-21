from fastapi import FastAPI

from lumewarp_control_plane import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="Lumewarp control plane", version=__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app

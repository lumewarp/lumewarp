from fastapi.testclient import TestClient

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.plugins import GROUPS, discover
from lumewarp_control_plane.settings import Settings


def test_healthz() -> None:
    resp = TestClient(create_app(Settings(background_enabled=False))).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_discover_known_groups() -> None:
    for group in GROUPS:
        assert isinstance(discover(group), dict)


def test_openapi_without_database() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        background_enabled=False,
    )
    spec = create_app(settings).openapi()
    assert spec["info"]["title"] == "Lumewarp control plane"
    assert "/readyz" in spec["paths"]


def test_readyz_ready(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"


def test_readyz_database_down() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        background_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "unreachable"


def test_unknown_route_error_shape() -> None:
    resp = TestClient(create_app(Settings(background_enabled=False))).get("/api/v1/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"

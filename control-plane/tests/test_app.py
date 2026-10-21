from fastapi.testclient import TestClient

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.plugins import GROUPS, discover


def test_healthz() -> None:
    resp = TestClient(create_app()).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_discover_known_groups() -> None:
    for group in GROUPS:
        assert isinstance(discover(group), dict)

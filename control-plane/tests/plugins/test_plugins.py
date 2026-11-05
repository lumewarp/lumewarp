import importlib
import sys
from pathlib import Path

import pytest

from lumewarp_control_plane import plugins

EXAMPLE = Path(__file__).parent / "example_plugin"


@pytest.fixture
def example_installed(monkeypatch: pytest.MonkeyPatch):
    """Put the example package (with its .dist-info) on sys.path so importlib.metadata
    discovers its entry points exactly like an installed distribution.
    """
    monkeypatch.syspath_prepend(str(EXAMPLE))
    importlib.invalidate_caches()
    yield
    sys.modules.pop("lumewarp_example_plugin", None)
    importlib.invalidate_caches()


def test_discovery(example_installed) -> None:
    assert "example" in plugins.discover(plugins.AUTH)
    [auth] = plugins.load(plugins.AUTH)
    assert isinstance(auth.plugin, plugins.AuthPlugin)


def test_no_plugins_without_install() -> None:
    assert plugins.load(plugins.AUTH) == []


async def test_example_auth_maps_identity(example_installed) -> None:
    [auth] = plugins.load(plugins.AUTH)
    claims = {"email": "dev@example.test", "name": "Dev"}
    mapping = await auth.plugin.map_identity(claims)
    assert mapping == plugins.IdentityMapping(email="dev@example.test", display_name="Dev")
    assert auth.plugin.seen[-1] == claims
    assert await auth.plugin.map_identity({"sub": "no-email"}) is None


def test_api_version_mismatch_and_load_errors_skipped(monkeypatch) -> None:
    class Old:
        api_version = 0

    class EP:
        def __init__(self, name, obj):
            self.name, self.obj = name, obj

        def load(self):
            if isinstance(self.obj, Exception):
                raise self.obj
            return self.obj

    eps = {
        "old": EP("old", Old),
        "broken": EP("broken", ImportError("x")),
        "ok": EP("ok", type("Valid", (), {"api_version": 1})),
    }
    monkeypatch.setattr(plugins, "discover", lambda group: eps)
    assert [p.name for p in plugins.load(plugins.AUTH)] == ["ok"]

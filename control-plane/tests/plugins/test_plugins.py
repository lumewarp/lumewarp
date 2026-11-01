import importlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lumewarp_control_plane import plugins
from lumewarp_control_plane.models import ScopeType
from lumewarp_control_plane.notifier import BudgetEvent

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
    assert "example" in plugins.discover(plugins.NOTIFIER)
    [notifier] = plugins.load(plugins.NOTIFIER)
    assert isinstance(notifier.plugin, plugins.NotifierPlugin)


def test_no_plugins_without_install() -> None:
    assert plugins.load(plugins.NOTIFIER) == []


async def test_example_notifier_send(example_installed) -> None:
    [notifier] = plugins.load(plugins.NOTIFIER)
    event = BudgetEvent(
        kind="soft",
        scope_type=ScopeType.TEAM,
        scope_id=uuid.uuid4(),
        team_id=uuid.uuid4(),
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=80,
        budget_nano_usd=100,
    )
    await notifier.plugin.send(event, {"channel": "#ops"})
    assert notifier.plugin.sent[-1] == (event, {"channel": "#ops"})


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
    assert [p.name for p in plugins.load(plugins.NOTIFIER)] == ["ok"]

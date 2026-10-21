import asyncio
import importlib
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lumewarp_control_plane import plugins
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import ScopeType
from lumewarp_control_plane.notifier import BudgetEvent
from lumewarp_control_plane.plugins import (
    DeploymentView,
    LoadedPlugin,
    RouteView,
    WeightOverride,
    apply_router_plugins,
)

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
    assert "example" in plugins.discover(plugins.ROUTER)
    assert "example" in plugins.discover(plugins.NOTIFIER)
    [router] = plugins.load_router_plugins()
    assert router.name == "example" and isinstance(router.plugin, plugins.RouterPolicyPlugin)
    [notifier] = plugins.load(plugins.NOTIFIER)
    assert isinstance(notifier.plugin, plugins.NotifierPlugin)


def test_no_plugins_without_install() -> None:
    assert plugins.load_router_plugins() == []


async def test_example_router_applied_in_compile(example_installed, factory) -> None:
    from tests.compiler.helpers import SCOPE, add_route, seed_scope

    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_route(
            s, "llama", [("llama-pricey", 9), ("llama-cheap", 2), ("llama-free?", None)]
        )
    async with factory() as s:
        # router_plugins=None -> discovered via entry points.
        c = await compile_scope(s, SCOPE, now=datetime(2026, 10, 1, tzinfo=UTC))
    weights = {d.name: d.weight for d in c.snapshot.deployments}
    assert weights == {"llama-cheap": 100, "llama-pricey": 1, "llama-free?": 1}


async def test_example_notifier_send(example_installed) -> None:
    [notifier] = plugins.load(plugins.NOTIFIER)
    event = BudgetEvent(
        kind="soft",
        scope_type=ScopeType.TEAM,
        scope_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=80,
        budget_nano_usd=100,
    )
    await notifier.plugin.send(event, {"channel": "#ops"})
    assert notifier.plugin.sent[-1] == (event, {"channel": "#ops"})


# --------------------------------------------------------------------------- isolation

ROUTES = [
    RouteView(
        model="m",
        strategy="weighted",
        deployments=(DeploymentView("d1", "u", "x", 1, None, None),),
    )
]


class Fixed:
    api_version = 1

    def __init__(self, weight: int, deployment: str = "d1") -> None:
        self.out = [WeightOverride(deployment, weight)]

    def adjust(self, routes):
        return self.out


class Raises:
    api_version = 1

    def adjust(self, routes):
        raise RuntimeError("boom")


class SlowSync:
    api_version = 1

    def adjust(self, routes):
        time.sleep(0.5)
        return [WeightOverride("d1", 7)]


class SlowAsync:
    api_version = 1

    async def adjust(self, routes):
        await asyncio.sleep(5)
        return [WeightOverride("d1", 8)]


class Garbage:
    api_version = 1

    def adjust(self, routes):
        return [{"deployment": "d1", "weight": 3}]


def lp(name: str, plugin) -> LoadedPlugin:
    return LoadedPlugin(name=name, plugin=plugin)


async def test_failing_plugins_are_skipped() -> None:
    result = await apply_router_plugins(
        ROUTES,
        [
            lp("a", Fixed(5)),
            lp("raises", Raises()),
            lp("slow-sync", SlowSync()),
            lp("slow-async", SlowAsync()),
            lp("garbage", Garbage()),
            lp("unknown-deployment", Fixed(9, "nope")),
            lp("negative", Fixed(-1)),
            lp("too-big", Fixed(2**32)),
        ],
        timeout_seconds=0.1,
    )
    assert result == {"d1": 5}


async def test_later_plugin_wins() -> None:
    assert await apply_router_plugins(ROUTES, [lp("a", Fixed(5)), lp("b", Fixed(0))]) == {"d1": 0}


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
        "ok": EP("ok", Fixed(1)),
    }
    monkeypatch.setattr(plugins, "discover", lambda group: eps)
    assert [p.name for p in plugins.load(plugins.ROUTER)] == ["ok"]

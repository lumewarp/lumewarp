"""Plugin discovery and protocols.

Plugins run only in the control plane and produce declarative output that the
config compiler bakes into the snapshot; they never run on the gateway hot path.

A plugin is an entry point in one of GROUPS. The entry point may name an instance, a
class or a zero-argument factory; classes and factories are called once at load. Every
plugin declares `api_version`; plugins for another version are skipped with a warning.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lumewarp_control_plane.notifier import BudgetEvent

log = logging.getLogger(__name__)

AUTH = "lumewarp.auth"
ROUTER = "lumewarp.router"
NOTIFIER = "lumewarp.notifier"

GROUPS = (AUTH, ROUTER, NOTIFIER)

PLUGIN_API_VERSION = 1
# Per-plugin budget for one `adjust` call during a compile.
ROUTER_PLUGIN_TIMEOUT_SECONDS = 2.0
# Weights must round-trip through deployments.weight (Postgres INTEGER) for routing
# rollback, so the plugin range is the column's, not uint32's.
MAX_WEIGHT = 2**31 - 1
MAX_OVERRIDES = 10_000


def discover(group: str) -> dict[str, EntryPoint]:
    if group not in GROUPS:
        raise ValueError(f"unknown plugin group {group!r}")
    return {ep.name: ep for ep in entry_points(group=group)}


# --------------------------------------------------------------------------- router


@dataclass(frozen=True)
class DeploymentView:
    name: str
    upstream: str
    upstream_model: str
    weight: int
    # nano-USD per million tokens; None = price unknown.
    price_input_nano_usd_per_mtok: int | None
    price_output_nano_usd_per_mtok: int | None


@dataclass(frozen=True)
class RouteView:
    """Read-only view of one route as the compiler is about to emit it."""

    model: str
    strategy: str  # models.RoutingStrategy value
    deployments: tuple[DeploymentView, ...]


@dataclass(frozen=True)
class WeightOverride:
    """Set `deployment`'s weight in the compiled snapshot (source rows are unchanged)."""

    deployment: str
    weight: int


@runtime_checkable
class RouterPolicyPlugin(Protocol):
    """Computes routing weights at compile time (e.g. from GPU cost or time of day).

    `adjust` may be sync or async. The call and the iteration of its result run off the
    event loop under one timeout. If it raises, times out or returns anything invalid
    (not WeightOverride items, unknown deployment, weight outside 0..2^31-1, more than
    MAX_OVERRIDES items, or an output that leaves a weighted route with zero total
    weight), its whole output is discarded and the compile continues without it. Later
    plugins win on conflicts (plugins are applied in entry-point name order).
    """

    api_version: int

    def adjust(
        self, routes: Sequence[RouteView]
    ) -> Sequence[WeightOverride] | Awaitable[Sequence[WeightOverride]]: ...


# --------------------------------------------------------------------------- notifier


@runtime_checkable
class NotifierPlugin(Protocol):
    """Delivers budget notifications for `notifier_channels` rows whose `kind` is the
    plugin's entry-point name. Called from the notifier outbox loop, never from ingest;
    raising makes the outbox retry with backoff. `config` is the channel's config.
    """

    api_version: int

    async def send(self, event: BudgetEvent, config: Mapping[str, Any]) -> None: ...


# --------------------------------------------------------------------------- auth (P2)


@dataclass(frozen=True)
class IdentityMapping:
    """Declarative result of an auth plugin: who the user is and which grants they get."""

    email: str
    display_name: str = ""
    # (org name, team name or None, role) tuples, applied by the control plane.
    grants: tuple[tuple[str, str | None, str], ...] = ()
    is_superuser: bool = False


@runtime_checkable
class AuthPlugin(Protocol):
    """P2: maps an external identity (OIDC claims, LDAP entry, ...) to a Lumewarp user and
    memberships. Runs in the control plane at login only. Protocol only in P1.
    """

    api_version: int

    async def map_identity(self, claims: Mapping[str, Any]) -> IdentityMapping | None: ...


# --------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class LoadedPlugin:
    name: str
    plugin: Any


def load(group: str) -> list[LoadedPlugin]:
    """Load every plugin of `group`, sorted by name. Broken or incompatible plugins are
    logged and skipped.
    """
    loaded = []
    for name, ep in sorted(discover(group).items()):
        try:
            obj = ep.load()
            if inspect.isclass(obj) or (callable(obj) and not hasattr(obj, "api_version")):
                obj = obj()
        except Exception:
            log.exception("plugin %s:%s failed to load; skipped", group, name)
            continue
        version = getattr(obj, "api_version", None)
        if version != PLUGIN_API_VERSION:
            log.warning(
                "plugin %s:%s has api_version %r, expected %d; skipped",
                group,
                name,
                version,
                PLUGIN_API_VERSION,
            )
            continue
        loaded.append(LoadedPlugin(name=name, plugin=obj))
    return loaded


def load_router_plugins() -> list[LoadedPlugin]:
    return [p for p in load(ROUTER) if isinstance(p.plugin, RouterPolicyPlugin)]


class PluginOutputError(ValueError):
    """A router plugin returned something the compiler cannot use."""


def _materialize(result: Any, known: frozenset[str]) -> list[tuple[str, int]]:
    """Copy a plugin's result into plain, validated (deployment, weight) tuples.

    Runs on the plugin's worker thread: the result may be any user object whose iteration
    or attribute access is slow or never ends, so it must never be touched on the event
    loop. Bounded by MAX_OVERRIDES.
    """
    out: list[tuple[str, int]] = []
    for item in result:
        if len(out) >= MAX_OVERRIDES:
            raise PluginOutputError(f"more than {MAX_OVERRIDES} overrides")
        if type(item) is not WeightOverride:
            raise PluginOutputError(f"expected WeightOverride, got {type(item).__name__}")
        deployment, weight = item.deployment, item.weight
        if type(deployment) is not str or deployment not in known:
            raise PluginOutputError(f"unknown deployment {deployment!r}")
        if type(weight) is not int or not 0 <= weight <= MAX_WEIGHT:
            raise PluginOutputError(f"weight {weight!r} out of range 0..{MAX_WEIGHT}")
        out.append((deployment, weight))
    return out


async def _in_daemon_thread(fn: Callable[[], Any]) -> Any:
    """Run `fn` on a fresh daemon thread and await its result. Unlike asyncio.to_thread,
    a plugin that never returns cannot hold up interpreter or executor shutdown; on
    timeout the thread is abandoned.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def deliver(ok: bool, value: Any) -> None:
        if future.done():
            return
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)

    def run() -> None:
        try:
            value, ok = fn(), True
        except BaseException as exc:  # handed to the awaiting coroutine
            value, ok = exc, False
        try:
            loop.call_soon_threadsafe(deliver, ok, value)
        except RuntimeError:  # loop already closed: the result is no longer wanted
            pass

    threading.Thread(target=run, name="lumewarp-plugin", daemon=True).start()
    return await future


async def _run_router_plugin(
    plugin: Any, routes: Sequence[RouteView], known: frozenset[str], timeout_seconds: float
) -> list[tuple[str, int]]:
    """Call, materialize and validate one plugin's output under one deadline."""
    async with asyncio.timeout(timeout_seconds):
        if inspect.iscoroutinefunction(plugin.adjust):
            result = await plugin.adjust(routes)
            return await _in_daemon_thread(lambda: _materialize(result, known))

        def call() -> list[tuple[str, int]]:
            result = plugin.adjust(routes)
            if inspect.isawaitable(result):
                raise PluginOutputError("sync adjust returned an awaitable")
            return _materialize(result, known)

        return await _in_daemon_thread(call)


def _breaks_weighted_route(routes: Sequence[RouteView], before: dict[str, int],
                           after: dict[str, int]) -> str | None:  # fmt: skip
    """Name of a weighted route whose total weight the overrides drive from >0 to 0."""
    for r in routes:
        if r.strategy != "weighted":
            continue
        names = [d.name for d in r.deployments]
        if sum(before[n] for n in names) > 0 and sum(after[n] for n in names) == 0:
            return r.model
    return None


async def apply_router_plugins(
    routes: Sequence[RouteView],
    plugins: Sequence[LoadedPlugin],
    timeout_seconds: float = ROUTER_PLUGIN_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """Run router plugins in order; return deployment name -> overridden weight.

    Each plugin's call, result iteration and validation run off the event loop under one
    deadline. A plugin that raises, times out, returns invalid output (unknown
    deployment, weight outside 0..MAX_WEIGHT, more than MAX_OVERRIDES items) or would
    leave a weighted route with zero total weight is skipped as a whole.
    """
    known = frozenset(d.name for r in routes for d in r.deployments)
    weights = {d.name: d.weight for r in routes for d in r.deployments}
    overrides: dict[str, int] = {}
    for p in plugins:
        try:
            output = await _run_router_plugin(p.plugin, routes, known, timeout_seconds)
            candidate = {**weights, **dict(output)}
            if (broken := _breaks_weighted_route(routes, weights, candidate)) is not None:
                raise PluginOutputError(f"route {broken!r} would have zero total weight")
        except Exception as exc:
            log.warning("router plugin %s skipped: %s", p.name, exc or type(exc).__name__)
            continue
        weights = candidate
        overrides.update(output)
    return overrides

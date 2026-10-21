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
from collections.abc import Awaitable, Mapping, Sequence
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
MAX_WEIGHT = 2**32 - 1


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

    `adjust` may be sync or async. It runs under a timeout; if it raises, times out or
    returns anything invalid (unknown deployment, weight outside 0..2^32-1), its whole
    output is discarded and the compile continues without it. Later plugins win on
    conflicts (plugins are applied in entry-point name order).
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


async def apply_router_plugins(
    routes: Sequence[RouteView],
    plugins: Sequence[LoadedPlugin],
    timeout_seconds: float = ROUTER_PLUGIN_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """Run router plugins in order; return deployment name -> overridden weight."""
    known = {d.name for r in routes for d in r.deployments}
    weights: dict[str, int] = {}
    for p in plugins:
        try:
            async with asyncio.timeout(timeout_seconds):
                if inspect.iscoroutinefunction(p.plugin.adjust):
                    result = await p.plugin.adjust(routes)
                else:
                    # A thread, so a blocking plugin cannot stall the event loop past the
                    # timeout (the thread itself is abandoned, not killed).
                    result = await asyncio.to_thread(p.plugin.adjust, routes)
                    if inspect.isawaitable(result):
                        result = await result
            overrides = list(result)
            for o in overrides:
                if not isinstance(o, WeightOverride):
                    raise TypeError(f"expected WeightOverride, got {type(o).__name__}")
                if o.deployment not in known:
                    raise ValueError(f"unknown deployment {o.deployment!r}")
                if not isinstance(o.weight, int) or not 0 <= o.weight <= MAX_WEIGHT:
                    raise ValueError(f"weight {o.weight!r} out of range")
        except Exception as exc:
            log.warning("router plugin %s skipped: %s", p.name, exc or type(exc).__name__)
            continue
        weights.update({o.deployment: o.weight for o in overrides})
    return weights

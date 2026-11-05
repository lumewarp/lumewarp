"""Plugin discovery and protocols.

Plugins run only in the control plane: auth maps identities and notifiers deliver
budget events. They never run on the gateway hot path.

A plugin is an entry point in one of GROUPS. The entry point may name an instance, a
class or a zero-argument factory; classes and factories are called once at load. Every
plugin declares `api_version`; plugins for another version are skipped with a warning.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lumewarp_control_plane.notifier import BudgetEvent

log = logging.getLogger(__name__)

AUTH = "lumewarp.auth"
NOTIFIER = "lumewarp.notifier"

GROUPS = (AUTH, NOTIFIER)

PLUGIN_API_VERSION = 1


def discover(group: str) -> dict[str, EntryPoint]:
    if group not in GROUPS:
        raise ValueError(f"unknown plugin group {group!r}")
    return {ep.name: ep for ep in entry_points(group=group)}


# --------------------------------------------------------------------------- notifier


@runtime_checkable
class NotifierPlugin(Protocol):
    """Delivers budget notifications for `notifier_channels` rows whose `kind` is the
    plugin's entry-point name. Called from the notifier outbox loop, never from ingest;
    raising makes the outbox retry with backoff. `config` is the channel's config.
    """

    api_version: int

    async def send(self, event: BudgetEvent, config: Mapping[str, Any]) -> None: ...


# -------------------------------------------------------------------------------- auth


@dataclass(frozen=True)
class IdentityMapping:
    """Declarative result of an auth plugin: who the user is. Team roles and the installation
    admin group are answered by the authz backend, not by this mapping."""

    email: str
    display_name: str = ""


@runtime_checkable
class AuthPlugin(Protocol):
    """Maps an external identity (OIDC claims, LDAP entry, ...) to a Lumewarp user.
    Runs in the control plane at login only.
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

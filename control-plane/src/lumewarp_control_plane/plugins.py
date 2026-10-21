"""Plugin discovery via entry points.

Plugins run only in the control plane and produce declarative output that the
config compiler bakes into the snapshot; they never run on the gateway hot path.
"""

from importlib.metadata import EntryPoint, entry_points

AUTH = "lumewarp.auth"
ROUTER = "lumewarp.router"
NOTIFIER = "lumewarp.notifier"

GROUPS = (AUTH, ROUTER, NOTIFIER)


def discover(group: str) -> dict[str, EntryPoint]:
    if group not in GROUPS:
        raise ValueError(f"unknown plugin group {group!r}")
    return {ep.name: ep for ep in entry_points(group=group)}

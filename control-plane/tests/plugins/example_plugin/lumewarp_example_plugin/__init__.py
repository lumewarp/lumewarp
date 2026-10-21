"""Example plugins for Lumewarp's `lumewarp.router` and `lumewarp.notifier` groups."""

from collections.abc import Mapping, Sequence
from typing import Any

from lumewarp_control_plane.plugins import RouteView, WeightOverride

PREFERRED_WEIGHT = 100


class CheapestFirstRouter:
    """Gives the cheapest priced deployment of each route weight 100. Deployments
    without a price are never preferred.
    """

    api_version = 1

    def adjust(self, routes: Sequence[RouteView]) -> list[WeightOverride]:
        out = []
        for route in routes:
            priced = [d for d in route.deployments if d.price_input_nano_usd_per_mtok is not None]
            if priced:
                cheapest = min(
                    priced,
                    key=lambda d: (
                        d.price_input_nano_usd_per_mtok + (d.price_output_nano_usd_per_mtok or 0),
                        d.name,
                    ),
                )
                out.append(WeightOverride(cheapest.name, PREFERRED_WEIGHT))
        return out


class RecordingNotifier:
    """Keeps every event it is sent, for tests."""

    api_version = 1
    sent: list[tuple[Any, Mapping[str, Any]]] = []

    async def send(self, event: Any, config: Mapping[str, Any]) -> None:
        self.sent.append((event, dict(config)))

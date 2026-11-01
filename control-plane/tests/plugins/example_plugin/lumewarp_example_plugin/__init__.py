"""Example Lumewarp notifier plugin."""

from collections.abc import Mapping
from typing import Any


class RecordingNotifier:
    """Keeps every event it is sent, for tests."""

    api_version = 1
    sent: list[tuple[Any, Mapping[str, Any]]] = []

    async def send(self, event: Any, config: Mapping[str, Any]) -> None:
        self.sent.append((event, dict(config)))

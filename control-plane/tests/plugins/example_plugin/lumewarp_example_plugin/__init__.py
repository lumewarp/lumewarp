"""Example Lumewarp auth plugin."""

from collections.abc import Mapping
from typing import Any

from lumewarp_control_plane.plugins import IdentityMapping


class RecordingAuth:
    """Keeps every claim set it was given, for tests."""

    api_version = 1
    seen: list[dict[str, Any]] = []

    async def map_identity(self, claims: Mapping[str, Any]) -> IdentityMapping | None:
        self.seen.append(dict(claims))
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            return None
        return IdentityMapping(email=email, display_name=claims.get("name", ""))

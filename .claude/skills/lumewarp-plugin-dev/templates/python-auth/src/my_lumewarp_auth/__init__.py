"""A `lumewarp.auth` plugin: maps verified OIDC claims to a Lumewarp user.

It runs in the control plane on a user's first SSO login only; a returning user is found
by issuer and subject. It decides who the user is and grants nothing: roles and team
memberships come from Lumewarp's database or the authorization backend.

This example accepts only addresses in the domains listed in `MY_AUTH_ALLOWED_DOMAINS`
(comma-separated) and builds the display name from the `given_name` and `family_name`
claims. Returning `None` refuses the login.
"""

import os
from collections.abc import Mapping
from typing import Any

from lumewarp_control_plane.plugins import IdentityMapping


class Identity:
    api_version = 1

    def __init__(self) -> None:
        # Read once: the class is instantiated when the control plane loads the plugin.
        raw = os.environ.get("MY_AUTH_ALLOWED_DOMAINS", "")
        self.allowed_domains = {d.strip().lower() for d in raw.split(",") if d.strip()}

    async def map_identity(self, claims: Mapping[str, Any]) -> IdentityMapping | None:
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            return None
        if email.rsplit("@", 1)[1].lower() not in self.allowed_domains:
            return None
        parts = [claims.get("given_name"), claims.get("family_name")]
        name = " ".join(p for p in parts if isinstance(p, str) and p)
        # The control plane still requires `email_verified` and bounds both fields
        # (email at most 320 characters, display name at most 200).
        return IdentityMapping(email=email, display_name=name[:200])

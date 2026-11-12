from importlib.metadata import entry_points

import pytest
from lumewarp_control_plane import plugins
from lumewarp_control_plane.plugins import IdentityMapping

from my_lumewarp_auth import Identity


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("MY_AUTH_ALLOWED_DOMAINS", "example.com, Example.org")
    return Identity()


async def test_an_allowed_domain_maps_to_the_user(identity):
    claims = {"email": "ada@EXAMPLE.org", "given_name": "Ada", "family_name": "Lovelace"}
    assert await identity.map_identity(claims) == IdentityMapping(
        email="ada@EXAMPLE.org", display_name="Ada Lovelace"
    )


async def test_other_domains_and_missing_emails_are_refused(identity):
    assert await identity.map_identity({"email": "eve@evil.test"}) is None
    assert await identity.map_identity({"email": "example.com"}) is None
    assert await identity.map_identity({}) is None


def test_the_control_plane_loads_it(monkeypatch):
    """The same loader the control plane runs at login: entry point, api_version."""
    assert "my-auth" in {ep.name for ep in entry_points(group=plugins.AUTH)}
    monkeypatch.setenv("MY_AUTH_ALLOWED_DOMAINS", "example.com")
    loaded = {p.name: p.plugin for p in plugins.load(plugins.AUTH)}
    assert isinstance(loaded["my-auth"], Identity)

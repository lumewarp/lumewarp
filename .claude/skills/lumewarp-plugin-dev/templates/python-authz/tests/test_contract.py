"""Runs the control plane's backend conformance suite against `DirectoryBackend`.

The directory is faked with an httpx mock transport that serves the grants the suite's
seed expects: a team admin and a team member on the first team, an org admin on the org,
and nothing for anyone else (an external directory owns deactivation, so the inactive user
has no grants).
"""

import uuid

import httpx
import pytest
from lumewarp_control_plane.authz import Decision, Relation, load_authorizer, team_resource
from lumewarp_control_plane.authz.contract_tests import AuthzContract, AuthzSeed
from lumewarp_control_plane.settings import Settings

from my_lumewarp_authz import DirectoryBackend


@pytest.fixture
def authz_seed():
    return AuthzSeed(
        *(str(uuid.uuid4()) for _ in range(5)),
        teams=(uuid.uuid4(), uuid.uuid4()),
        org=uuid.uuid4(),
        org_admin=str(uuid.uuid4()),
    )


def directory(seed: AuthzSeed) -> httpx.MockTransport:
    grants = {
        seed.admin: [{"relation": "admin", "type": "team", "id": str(seed.teams[0])}],
        seed.member: [{"relation": "member", "type": "team", "id": str(seed.teams[0])}],
        seed.org_admin: [{"relation": "admin", "type": "org", "id": str(seed.org)}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        user = request.url.path.split("/")[2]
        if user not in grants:
            return httpx.Response(404)
        return httpx.Response(200, json=grants[user])

    return httpx.MockTransport(respond)


class TestDirectoryContract(AuthzContract):
    @pytest.fixture
    async def authz_backend(self, authz_seed):
        client = httpx.AsyncClient(
            base_url="https://directory.test", transport=directory(authz_seed)
        )
        yield DirectoryBackend(client)
        await client.aclose()


async def test_an_unreachable_directory_is_an_error_not_a_deny(authz_seed):
    def fail(request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(
        base_url="https://directory.test", transport=httpx.MockTransport(fail)
    )
    backend = DirectoryBackend(client)
    resource = team_resource(authz_seed.teams[0])
    assert await backend.check(authz_seed.admin, Relation.ADMIN, resource) is Decision.ERROR
    await client.aclose()


def test_the_control_plane_loads_it(monkeypatch):
    """The loader `serve` runs for LUMEWARP_AUTHZ_BACKEND=my-authz."""
    monkeypatch.setenv("MY_AUTHZ_DIRECTORY_URL", "https://directory.test")
    authorizer = load_authorizer(Settings(authz_backend="my-authz"), None)
    assert isinstance(authorizer.backend, DirectoryBackend)

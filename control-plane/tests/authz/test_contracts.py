import json

import httpx
import pytest

from lumewarp_control_plane.authz import Relation, Resource
from lumewarp_control_plane.authz.contract_tests import AuthzContract
from lumewarp_control_plane.authz.http import HttpBackend
from lumewarp_control_plane.settings import Settings


class TestCheckOnlyContract(AuthzContract):
    @pytest.fixture
    def authz_backend(self, grant_check):
        class CheckOnly:
            async def check(self, user_id, relation, resource):
                return await grant_check(user_id, relation, resource)

        return CheckOnly()


class TestHttpContract(AuthzContract):
    @pytest.fixture
    async def authz_backend(self, grant_check):
        async def respond(request):
            body = json.loads(request.content)
            decision = await grant_check(
                body["user_id"], Relation(body["relation"]), Resource(**body["resource"])
            )
            return httpx.Response(200, json={"decision": decision})

        backend = HttpBackend(Settings(authz_http_url="https://authz.test/check"))
        await backend._client.aclose()
        backend._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        yield backend
        await backend.aclose()


class TestOpenFGAContract(AuthzContract):
    @pytest.fixture
    def authz_backend(self, live_backend):
        return live_backend

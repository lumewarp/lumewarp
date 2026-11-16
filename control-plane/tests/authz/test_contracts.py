import json

import httpx
import pytest

from lumewarp_control_plane.authz import Decision, Relation, Resource
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


async def _http_backend(settings, respond):
    backend = HttpBackend(settings)
    headers = backend._client.headers
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(respond))
    return backend


class TestHttpContract(AuthzContract):
    """check, list and changes endpoints all answered from the same grants."""

    @pytest.fixture
    async def authz_backend(self, grant_check, authz_seed):
        async def respond(request):
            body = json.loads(request.content)
            if request.url.path == "/list":
                kind = body["resource_type"]
                candidates = (
                    [str(t) for t in authz_seed.teams] if kind == "team" else [str(authz_seed.org)]
                )
                ids = [
                    i
                    for i in candidates
                    if await grant_check(
                        body["user_id"], Relation(body["relation"]), Resource(kind, i)
                    )
                    is Decision.ALLOW
                ]
                return httpx.Response(200, json={"ids": ids})
            decision = await grant_check(
                body["user_id"], Relation(body["relation"]), Resource(**body["resource"])
            )
            return httpx.Response(200, json={"decision": decision})

        backend = await _http_backend(
            Settings(
                authz_http_url="https://authz.test/check",
                authz_http_list_url="https://authz.test/list",
            ),
            respond,
        )
        assert backend.list_objects is not None
        yield backend
        await backend.aclose()


class TestHttpCheckOnlyContract(AuthzContract):
    @pytest.fixture
    async def authz_backend(self, grant_check):
        async def respond(request):
            body = json.loads(request.content)
            decision = await grant_check(
                body["user_id"], Relation(body["relation"]), Resource(**body["resource"])
            )
            return httpx.Response(200, json={"decision": decision})

        backend = await _http_backend(Settings(authz_http_url="https://authz.test/check"), respond)
        assert not hasattr(backend, "list_objects")
        yield backend
        await backend.aclose()


class TestOpenFGAContract(AuthzContract):
    @pytest.fixture
    def authz_backend(self, live_backend):
        return live_backend

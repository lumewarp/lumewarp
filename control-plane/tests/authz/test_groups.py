"""Group resources use the same live contract as team resources."""

import json

import httpx

from lumewarp_control_plane.authz import Authorizer, Decision, Relation, Resource
from lumewarp_control_plane.authz.http import HttpBackend
from lumewarp_control_plane.settings import Settings

GROUP = Resource("group", "lumewarp-admins")


async def test_http_group_check_serializes_contract_and_reflects_revocation(authz_seed):
    backend = HttpBackend(Settings(authz_http_url="https://iam.example/check"))
    await backend._client.aclose()
    decision = ["allow"]

    def respond(request):
        assert json.loads(request.content) == {
            "user_id": authz_seed.outsider,
            "relation": "member",
            "resource": {"type": "group", "id": "lumewarp-admins"},
        }
        return httpx.Response(200, json={"decision": decision[0]})

    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        authz = Authorizer(backend, "http")
        assert await authz.check(authz_seed.outsider, Relation.MEMBER, GROUP) is Decision.ALLOW
        decision[0] = "deny"
        assert await authz.check(authz_seed.outsider, Relation.MEMBER, GROUP) is Decision.DENY
    finally:
        await backend.aclose()


async def test_openfga_group_membership_is_live(live_backend, authz_seed):
    uid = authz_seed.outsider
    assert await live_backend.check(uid, Relation.MEMBER, GROUP) is Decision.ALLOW
    assert await live_backend.check(authz_seed.member, Relation.MEMBER, GROUP) is Decision.DENY
    key = live_backend._tuple(uid, Relation.MEMBER, GROUP)
    await live_backend._post("write", {"deletes": {"tuple_keys": [key]}})
    try:
        assert await live_backend.check(uid, Relation.MEMBER, GROUP) is Decision.DENY
    finally:
        await live_backend._post("write", {"writes": {"tuple_keys": [key]}})

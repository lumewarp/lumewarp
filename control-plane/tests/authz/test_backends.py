import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lumewarp_control_plane.authz import (
    Authorizer,
    ChangeBatch,
    Decision,
    Relation,
    Resource,
    load_authorizer,
    team_resource,
)
from lumewarp_control_plane.authz.http import HttpBackend
from lumewarp_control_plane.authz.openfga import OpenFGABackend
from lumewarp_control_plane.settings import Settings

ITEM = ("alice", Relation.MEMBER, team_resource("one"))
ALICE = "00000000-0000-0000-0000-000000000001"
BOB = "00000000-0000-0000-0000-000000000002"
CAROL = "00000000-0000-0000-0000-000000000003"


@pytest.fixture(params=["openfga", "http"])
async def backend(request):
    settings = Settings(
        authz_backend=request.param,
        openfga_url="https://fga.test",
        openfga_store_id="store",
        openfga_model_id="model",
        openfga_api_token="fga-secret",
        authz_http_url="https://authz.test/check",
        authz_http_token="http-secret",
    )
    result = load_authorizer(settings, None).backend
    yield result
    await result.aclose()
    assert result._client.is_closed


async def mock_client(backend, respond):
    headers = backend._client.headers
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(respond))


@pytest.mark.parametrize("allowed", [True, False])
async def test_check_mapping(backend, allowed):
    def respond(request):
        body = json.loads(request.content)
        if isinstance(backend, OpenFGABackend):
            assert request.url.path == "/stores/store/check"
            assert request.headers["authorization"] == "Bearer fga-secret"
            assert body == {
                "authorization_model_id": "model",
                "tuple_key": {"user": "user:alice", "relation": "member", "object": "team:one"},
                "consistency": "HIGHER_CONSISTENCY",
            }
            return httpx.Response(200, json={"allowed": allowed})
        assert request.url == "https://authz.test/check"
        assert request.headers["authorization"] == "Bearer http-secret"
        assert body == {
            "user_id": "alice",
            "relation": "member",
            "resource": {"type": "team", "id": "one"},
        }
        return httpx.Response(200, json={"decision": "allow" if allowed else "deny"})

    await mock_client(backend, respond)
    assert await backend.check(*ITEM) is (Decision.ALLOW if allowed else Decision.DENY)
    assert backend.manages_grants is False


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 429, 500, 503])
async def test_non_success_is_error_not_deny(backend, status):
    await mock_client(backend, lambda request: httpx.Response(status, json={"allowed": False}))
    with pytest.raises(httpx.HTTPStatusError):
        await backend.check(*ITEM)
    assert await Authorizer(backend).check(*ITEM) is Decision.ERROR


@pytest.mark.parametrize(
    "body", [None, [], {}, {"allowed": "false"}, {"allowed": 0}, {"decision": "error"}]
)
async def test_bad_body_is_error(backend, body):
    await mock_client(backend, lambda request: httpx.Response(200, json=body))
    assert await Authorizer(backend).check(*ITEM) is Decision.ERROR


async def test_invalid_json_and_transport_error(backend):
    await mock_client(backend, lambda request: httpx.Response(200, content=b"not JSON"))
    assert await Authorizer(backend).check(*ITEM) is Decision.ERROR

    def disconnected(request):
        raise httpx.ConnectError("unavailable", request=request)

    await mock_client(backend, disconnected)
    assert await Authorizer(backend).check(*ITEM) is Decision.ERROR


@pytest.fixture
async def fga():
    backend = OpenFGABackend(
        Settings(openfga_url="https://fga.test", openfga_store_id="store", openfga_model_id="model")
    )
    yield backend
    await backend.aclose()


async def test_unknown_type_400_is_error(fga):
    await mock_client(fga, lambda request: httpx.Response(400, json={"code": "type_not_found"}))
    assert (
        await Authorizer(fga).check("alice", Relation.MEMBER, Resource("unknown", "one"))
        is Decision.ERROR
    )


async def test_batch_correlation_errors_and_chunking(fga):
    sizes = []

    def respond(request):
        assert request.url.path == "/stores/store/batch-check"
        body = json.loads(request.content)
        assert body["authorization_model_id"] == "model"
        assert body["consistency"] == "HIGHER_CONSISTENCY"
        checks = body["checks"]
        sizes.append(len(checks))
        assert checks[0]["tuple_key"] == fga._tuple(*ITEM)
        results = {}
        for check in reversed(checks):
            index = int(check["correlation_id"])
            results[str(index)] = (
                {"allowed": True}
                if index % 3 == 0
                else {"allowed": False}
                if index % 3 == 1
                else {"error": {"code": "validation_error", "message": "invalid"}}
            )
        return httpx.Response(200, json={"result": results})

    await mock_client(fga, respond)
    assert await fga.batch_check([]) == []
    assert sizes == []
    assert await fga.batch_check([ITEM] * 52) == [
        [Decision.ALLOW, Decision.DENY, Decision.ERROR][i % 3]
        for size in (50, 2)
        for i in range(size)
    ]
    assert sizes == [50, 2]


@pytest.mark.parametrize("invalid", [{"allowed": "true"}, {"allowed": True, "error": {}}])
async def test_batch_missing_or_invalid_item(fga, invalid):
    await mock_client(
        fga,
        lambda request: httpx.Response(
            200, json={"result": {"0": invalid, "1": {"allowed": False}}}
        ),
    )
    assert await fga.batch_check([ITEM] * 3) == [Decision.ERROR, Decision.DENY, Decision.ERROR]
    await mock_client(fga, lambda request: httpx.Response(200, json={"result": []}))
    assert await Authorizer(fga).batch_check([ITEM]) == [Decision.ERROR]


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed"])
async def test_batch_chunk_failure_preserves_other_chunks(fga, failure):
    seen = []
    started, cancelled = asyncio.Event(), asyncio.Event()
    fga._timeout = 0.01

    async def respond(request):
        body = json.loads(request.content)
        seen.append(len(body["checks"]))
        if len(seen) == 2:
            if failure == "timeout":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            if failure == "http":
                return httpx.Response(503)
            return httpx.Response(200, json={"result": []})
        return httpx.Response(
            200,
            json={
                "result": {
                    check["correlation_id"]: {"allowed": len(seen) == 1} for check in body["checks"]
                }
            },
        )

    await mock_client(fga, respond)
    async with asyncio.timeout(2):
        task = asyncio.create_task(Authorizer(fga, timeout=0.1).batch_check([ITEM] * 101))
        if failure == "timeout":
            await started.wait()
        decisions = await task
    assert decisions == [Decision.ALLOW] * 50 + [Decision.ERROR] * 50 + [Decision.DENY]
    assert seen == [50, 50, 1]
    if failure == "timeout":
        assert cancelled.is_set()


@pytest.mark.parametrize("size, multiplier", [(0, 1), (1, 2), (50, 2), (51, 3), (501, 12)])
async def test_native_batch_deadline_scales_with_chunks(monkeypatch, size, multiplier):
    from lumewarp_control_plane import authz

    deadlines = []
    original = asyncio.timeout

    def timeout(seconds):
        deadlines.append(seconds)
        return original(seconds)

    class Native:
        async def check(self, *args):
            return Decision.ALLOW

        async def batch_check(self, items):
            return [Decision.ALLOW] * len(items)

    monkeypatch.setattr(authz.asyncio, "timeout", timeout)
    assert (
        await Authorizer(Native(), timeout=2).batch_check([ITEM] * size) == [Decision.ALLOW] * size
    )
    assert deadlines == [2 * multiplier]


async def test_list_objects_mapping_and_validation(fga):
    def respond(request):
        assert request.url.path == "/stores/store/list-objects"
        assert json.loads(request.content) == {
            "authorization_model_id": "model",
            "user": "user:alice",
            "relation": "member",
            "type": "team",
        }
        return httpx.Response(200, json={"objects": ["team:one", "team:two"]})

    await mock_client(fga, respond)
    assert await fga.list_teams("alice", Relation.MEMBER) == ["one", "two"]
    await mock_client(fga, lambda request: httpx.Response(200, json={"objects": ["user:alice"]}))
    with pytest.raises(ValueError):
        await fga.list_teams("alice", Relation.MEMBER)


def change(
    user=f"user:{ALICE}", relation="member", obj="team:one", operation="TUPLE_OPERATION_WRITE"
):
    return {
        "tuple_key": {"user": user, "relation": relation, "object": obj},
        "operation": operation,
    }


async def test_changes_pages_writes_and_deletes(fga):
    pages = {
        "start": (
            [change(), change(f"user:{BOB}", "admin", operation="TUPLE_OPERATION_DELETE")],
            "p2",
        ),
        "p2": ([change(f"user:{CAROL}")], "end"),
        "end": ([], "end"),
    }
    seen = []

    def respond(request):
        assert request.url.path == "/stores/store/changes"
        token = request.url.params["continuation_token"]
        seen.append(token)
        changes, next_token = pages[token]
        return httpx.Response(200, json={"changes": changes, "continuation_token": next_token})

    await mock_client(fga, respond)
    assert await fga.changes("start") == ChangeBatch(frozenset({ALICE, BOB, CAROL}), "end")
    assert seen == ["start", "p2", "end"]


@pytest.mark.parametrize("cursor", [None, "expired"])
async def test_changes_initial_or_invalid_cursor_resets_from_end(fga, cursor):
    seen = []
    before = datetime.now(UTC)

    def respond(request):
        token = request.url.params.get("continuation_token", "")
        seen.append(token)
        if token == "expired":
            return httpx.Response(400)
        if not token:
            start = datetime.fromisoformat(request.url.params["start_time"])
            assert before - timedelta(seconds=5) <= start <= datetime.now(UTC)
            return httpx.Response(200, json={"changes": [change()], "continuation_token": "end"})
        assert "start_time" not in request.url.params
        return httpx.Response(200, json={"continuation_token": "end"})

    await mock_client(fga, respond)
    batch = await fga.changes(cursor)
    assert batch.reset and batch.cursor == "end"
    assert seen == (["", "end"] if cursor is None else ["expired", "", "end"])


@pytest.mark.parametrize("initial", [True, False])
async def test_changes_page_limit_continues_next_poll(fga, initial):
    seen = []

    def respond(request):
        token = request.url.params.get("continuation_token", "0")
        seen.append(token)
        assert ("start_time" in request.url.params) == (initial and token == "0")
        number = int(token) + 1
        return httpx.Response(
            200,
            json={
                "changes": [change()] if number <= 21 else [],
                "continuation_token": str(number),
            },
        )

    await mock_client(fga, respond)
    batch = await fga.changes(None if initial else "0")
    assert len(seen) == 20
    assert batch.cursor == "20"
    assert batch.reset is initial
    following = await fga.changes(batch.cursor)
    assert following.cursor == "22"
    assert not following.reset
    assert seen[-2:] == ["20", "21"]


@pytest.mark.parametrize(
    "tuple_change",
    [
        change(relation="member", obj="group:lumewarp-admins"),
        change(relation="admin"),
    ],
)
async def test_group_and_team_changes_identify_affected_user(fga, tuple_change):
    await mock_client(
        fga,
        lambda request: httpx.Response(
            200, json={"changes": [tuple_change], "continuation_token": "same"}
        ),
    )
    batch = await fga.changes("same")
    assert not batch.reset
    assert batch.user_ids == frozenset({ALICE})


@pytest.mark.parametrize(
    "subject",
    [
        "installation:default",
        "team:other#member",
        "user:*",
        "user:alice#member",
        "user:not-a-uuid",
        "user:",
        "service:abc",
        None,
    ],
)
async def test_changes_ignore_invalid_user_subjects(fga, subject, caplog):
    await mock_client(
        fga,
        lambda request: httpx.Response(
            200, json={"changes": [change(subject), change()], "continuation_token": "same"}
        ),
    )
    with caplog.at_level("DEBUG", logger="lumewarp_control_plane.authz.openfga"):
        batch = await fga.changes("same")
    assert batch == ChangeBatch(frozenset({ALICE}), "same")
    assert len(caplog.records) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_changes_errors_are_not_empty_batches(fga, status):
    await mock_client(fga, lambda request: httpx.Response(status))
    assert await Authorizer(fga).changes(None) is None


@pytest.mark.parametrize("method", ["check", "batch_check", "list_teams", "changes"])
async def test_authorizer_timeout_cancels_hung_backend(method):
    started, cancelled = asyncio.Event(), asyncio.Event()
    never = asyncio.Event()

    async def hung(*args):
        started.set()
        try:
            await never.wait()
        finally:
            cancelled.set()

    class HungBackend:
        check = staticmethod(hung)
        batch_check = staticmethod(hung)
        list_teams = staticmethod(hung)
        changes = staticmethod(hung)

    settings = Settings(authz_timeout_seconds=0.01)
    authz = Authorizer(HungBackend(), timeout=settings.authz_timeout_seconds)
    calls = {
        "check": lambda: authz.check(*ITEM),
        "batch_check": lambda: authz.batch_check([ITEM]),
        "list_teams": lambda: authz.list_teams("alice", Relation.MEMBER, []),
        "changes": lambda: authz.changes(None),
    }
    async with asyncio.timeout(1):
        task = asyncio.create_task(calls[method]())
        await started.wait()
        result = await task
        await cancelled.wait()
    assert (
        result
        == {
            "check": Decision.ERROR,
            "batch_check": [Decision.ERROR],
            "list_teams": None,
            "changes": None,
        }[method]
    )


@pytest.mark.parametrize("backend", [OpenFGABackend, HttpBackend])
def test_missing_settings_rejected(backend):
    with pytest.raises(ValueError):
        backend(Settings())


async def test_live_changes_tracks_member_delete(live_backend, authz_seed):
    initial = await live_backend.changes(None)
    assert initial.reset and initial.cursor
    key = live_backend._tuple(
        authz_seed.member, Relation.MEMBER, team_resource(authz_seed.teams[0])
    )
    await live_backend._post("write", {"deletes": {"tuple_keys": [key]}})
    try:
        batch = await live_backend.changes(initial.cursor)
        assert not batch.reset
        assert authz_seed.member in batch.user_ids
        assert (
            await live_backend.check(
                authz_seed.member, Relation.MEMBER, team_resource(authz_seed.teams[0])
            )
            is Decision.DENY
        )
    finally:
        await live_backend._post("write", {"writes": {"tuple_keys": [key]}})

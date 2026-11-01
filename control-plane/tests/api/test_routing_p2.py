"""P2 routing API: health checks, circuit breakers, retries, fallbacks, traffic splits.

Every validation rule mirrors the gateway (snapshot.rs::validate / validate_split,
router.rs::RoutingTable::compile).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api import routing
from lumewarp_control_plane.api.schemas import RouterCreate, RouterMemberIn
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import AuditLog, CatalogName, ConfigOutbox, Model
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token

H = ADMIN_AUTH


def _upstream(client, **body):
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1", **body}
    return client.post("/api/v1/upstreams", json=body, headers=H)


def _model(client, upstream_id: str | None = None, **body) -> dict:
    if upstream_id is None:
        upstream_id = _upstream(client).json()["id"]
    body = {"name": f"dep-{suffix()}", "upstream_id": upstream_id, "upstream_model": "m", **body}
    resp = client.post("/api/v1/models", json=body, headers=H)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _members(*ids: str, weight: int = 1) -> list[dict]:
    return [{"model_id": i, "weight": weight} for i in ids]


def _router(client, **body):
    if "members" not in body and "traffic_split" not in body:
        body["members"] = _members(_model(client)["id"])
    if body.get("fallback_model_ids"):
        # Fallbacks need at least two attempts.
        body.setdefault("retry", {})
    body = {"name": f"alias-{suffix()}", **body}
    return client.post("/api/v1/routers", json=body, headers=H)


def _ok(resp, code: int = 201) -> dict:
    assert resp.status_code == code, resp.text
    return resp.json()


def _422(resp, needle: str) -> None:
    assert resp.status_code == 422, resp.text
    assert needle in resp.json()["error"]["message"], resp.json()


def _patch(client, router_id: str, **body):
    if body.get("fallback_model_ids"):
        body.setdefault("retry", {})
    return client.patch(f"/api/v1/routers/{router_id}", json=body, headers=H)


# --------------------------------------------------------------------------- upstreams


def test_custom_strategy_router_create_patch_and_clear(client):
    router = _ok(_router(client, strategy="header_pin", params={"headers": ["x-pin"], "pin": 2}))
    assert router["strategy"] == "header_pin"
    assert router["params"]["pin"] == 2
    updated = _ok(_patch(client, router["id"], params={"pin": 3}), 200)
    assert updated["params"] == {"pin": 3}
    assert updated["strategy"] == "header_pin"
    assert (
        _ok(_patch(client, router["id"], name=f"renamed-{suffix()}"), 200)["strategy"]
        == "header_pin"
    )
    cleared = _ok(_patch(client, router["id"], strategy="least_in_flight"), 200)
    assert cleared["params"] == {}
    assert cleared["strategy"] == "least_in_flight"


@pytest.mark.parametrize(
    "body",
    [
        {"strategy": "Bad"},
        {"strategy": "a-b"},
        {"strategy": "a" * 64},
        {"strategy": "ok", "params": []},
        {"strategy": "ok", "params": {"x": "a" * 16384}},
        {"strategy": "ok", "params": {"headers": ["bad name"]}},
        {"params": {"x": 1}},
    ],
)
def test_custom_strategy_router_validation(client, body):
    assert _router(client, **body).status_code == 422


@pytest.mark.parametrize(
    "header",
    ["authorization", "Authorization", "proxy-authorization", "cookie", "Set-Cookie", "X-API-Key"],
)
def test_custom_strategy_cannot_receive_credential_headers(client, header):
    params = {"headers": [header]}
    assert _router(client, strategy="header_pin", params=params).status_code == 422
    router = _ok(_router(client))
    assert _patch(client, router["id"], strategy="header_pin", params=params).status_code == 422


def test_patch_rejects_null_strategy_and_params(client):
    router = _ok(_router(client, strategy="ok"))
    assert _patch(client, router["id"], strategy=None).status_code == 422
    assert _patch(client, router["id"], params=None).status_code == 422


def test_upstream_health_breaker_metrics_roundtrip(client) -> None:
    up = _ok(
        _upstream(
            client,
            health_check={"path": "/health", "interval_ms": 5000, "timeout_ms": 1000},
            circuit_breaker={"failure_threshold": 3},
            metrics_url="http://127.0.0.1:9/metrics",
        )
    )
    assert up["health_check"] == {
        "path": "/health",
        "interval_ms": 5000,
        "timeout_ms": 1000,
        "unhealthy_threshold": None,
        "healthy_threshold": None,
    }
    assert up["circuit_breaker"] == {"failure_threshold": 3, "open_ms": None}
    assert up["metrics_url"] == "http://127.0.0.1:9/metrics"

    # PATCH: omitted settings stay, null removes.
    resp = client.patch(f"/api/v1/upstreams/{up['id']}", json={"timeout_ms": 5}, headers=H).json()
    assert resp["health_check"]["interval_ms"] == 5000 and resp["circuit_breaker"] is not None
    resp = client.patch(
        f"/api/v1/upstreams/{up['id']}",
        json={"health_check": None, "circuit_breaker": None, "metrics_url": None},
        headers=H,
    ).json()
    assert resp["health_check"] is None
    assert resp["circuit_breaker"] is None
    assert resp["metrics_url"] is None
    # An empty breaker object means "on, gateway defaults".
    resp = client.patch(
        f"/api/v1/upstreams/{up['id']}", json={"circuit_breaker": {}}, headers=H
    ).json()
    assert resp["circuit_breaker"] == {"failure_threshold": None, "open_ms": None}
    # Reads include the new fields.
    listed = client.get("/api/v1/upstreams?limit=500", headers=H).json()["items"]
    assert any(u["id"] == up["id"] and u["circuit_breaker"] for u in listed)


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"health_check": {"interval_ms": 999, "timeout_ms": 100}}, "interval_ms"),
        ({"health_check": {"interval_ms": 1000, "timeout_ms": 1000}}, "below interval_ms"),
        ({"health_check": {"interval_ms": 2000, "timeout_ms": 0}}, "timeout_ms"),
        (
            {"health_check": {"interval_ms": 2000, "timeout_ms": 10, "path": "health"}},
            "absolute path",
        ),
        (
            {"health_check": {"interval_ms": 2000, "timeout_ms": 10, "path": "/h?x=1"}},
            "without query",
        ),
        (
            {"health_check": {"interval_ms": 2000, "timeout_ms": 10, "unhealthy_threshold": 0}},
            "unhealthy_threshold",
        ),
        ({"circuit_breaker": {"failure_threshold": 0}}, "failure_threshold"),
        ({"circuit_breaker": {"open_ms": 0}}, "open_ms"),
        ({"metrics_url": "ftp://x/metrics"}, "http(s)"),
        ({"metrics_url": "http://u:p@x/metrics"}, "userinfo"),
        ({"metrics_url": "metrics"}, "http(s)"),
    ],
)
def test_upstream_validation(client, body, needle) -> None:
    _422(_upstream(client, **body), needle)
    up = _ok(_upstream(client))
    _422(client.patch(f"/api/v1/upstreams/{up['id']}", json=body, headers=H), needle)


# --------------------------------------------------------------------------- routers


def _out(ids: list[str], weight: int = 1) -> list[dict]:
    return [{"model_id": i, "weight": weight, "params": {}} for i in ids]


def test_router_p2_roundtrip_and_audit(client) -> None:
    upstream = _ok(_upstream(client))["id"]
    a, b, c, d = (_model(client, upstream) for _ in range(4))
    router = _ok(
        _router(
            client,
            strategy="vllm_load",
            retry={"max_attempts": 3, "retry_on_status": [503, 500], "total_timeout_ms": 9000},
            fallback_model_ids=[d["id"], c["id"]],
            traffic_split=[
                _split("stable", [a["id"]], weight=90),
                _split("canary", [b["id"], a["id"]], weight=10),
            ],
            sticky="header",
            sticky_header="X-Session-Id",
        )
    )
    assert router["strategy"] == "vllm_load"
    assert router["members"] == []
    assert router["retry"] == {
        "max_attempts": 3,
        "retry_on_status": [500, 503],
        "total_timeout_ms": 9000,
    }
    assert router["fallback_model_ids"] == [d["id"], c["id"]]  # order kept
    assert router["traffic_split"] == [
        {"name": "stable", "weight": 90, "members": _out([a["id"]])},
        {"name": "canary", "weight": 10, "members": _out([b["id"], a["id"]])},
    ]
    assert (router["sticky"], router["sticky_header"]) == ("header", "x-session-id")
    assert _ok(client.get(f"/api/v1/routers/{router['id']}", headers=H), 200) == router

    # PATCH: omitted fields are kept; fallbacks keep needing a retry policy; leaving
    # header stickiness drops the header.
    _422(_patch(client, router["id"], retry=None), "at least 2 attempts")
    _422(_patch(client, router["id"], retry={"max_attempts": 1}), "at least 2 attempts")
    patched = _ok(_patch(client, router["id"], retry={"max_attempts": 2}, sticky="team"), 200)
    assert patched["retry"]["max_attempts"] == 2
    assert (patched["sticky"], patched["sticky_header"]) == ("team", None)
    assert patched["traffic_split"] == router["traffic_split"]
    assert patched["fallback_model_ids"] == router["fallback_model_ids"]
    patched = _ok(
        _patch(
            client,
            router["id"],
            traffic_split=[],
            sticky="none",
            members=_members(a["id"]),
            fallback_model_ids=[],
            retry={},
            strategy="lowest_latency",
        ),
        200,
    )
    assert patched["traffic_split"] == [] and patched["fallback_model_ids"] == []
    assert patched["retry"] == {"max_attempts": None, "retry_on_status": [], "total_timeout_ms": 0}
    assert patched["strategy"] == "lowest_latency"


async def test_router_writes_audit_and_recompile(client, committed) -> None:
    outbox_before = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    backup = _model(client)
    router = _ok(_router(client, fallback_model_ids=[backup["id"]], retry={"max_attempts": 2}))
    _ok(_patch(client, router["id"], retry={"max_attempts": 3}), 200)
    outbox_after = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    # Two models + two upstreams + one router + one router update.
    assert outbox_after == outbox_before + 6
    rows = (
        await committed.scalars(select(AuditLog).where(AuditLog.resource_id == router["id"]))
    ).all()
    actions = sorted(r.action for r in rows)
    assert actions == ["router.create", "router.update"]
    create = next(r for r in rows if r.action == "router.create")
    assert create.details["settings"]["fallback_model_ids"] == [backup["id"]]
    assert create.details["settings"]["retry"]["max_attempts"] == 2


async def test_router_writes_need_superuser(client, committed) -> None:
    router = _ok(_router(client))
    _, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get(f"/api/v1/routers/{router['id']}", headers=auth(secret)).status_code == 200
    assert (
        client.patch(
            f"/api/v1/routers/{router['id']}", json={"retry": {}}, headers=auth(secret)
        ).status_code
        == 403
    )
    up = _ok(_upstream(client))
    assert (
        client.patch(
            f"/api/v1/upstreams/{up['id']}",
            json={"circuit_breaker": {}},
            headers=auth(secret),
        ).status_code
        == 403
    )


def _split(name: str, model_ids: list[str], weight: int = 1, member_weight: int = 1) -> dict:
    return {"name": name, "weight": weight, "members": _members(*model_ids, weight=member_weight)}


def test_router_validation_rules(client) -> None:
    dep = _model(client)
    d = dep["id"]
    unknown = str(uuid.uuid4())
    cases = [
        # retry
        ({"retry": {"max_attempts": 6}}, "max_attempts"),
        ({"retry": {"max_attempts": 0}}, "max_attempts"),
        ({"retry": {"retry_on_status": [399]}}, "400..599"),
        ({"retry": {"retry_on_status": [600]}}, "400..599"),
        ({"retry": {"retry_on_status": [503, 503]}}, "duplicates"),
        ({"retry": {"total_timeout_ms": -1}}, "total_timeout_ms"),
        # fallbacks
        ({"fallback_model_ids": [unknown], "retry": {}}, "not models"),
        # splits
        ({"traffic_split": [_split("a", [unknown])]}, "not models"),
        ({"traffic_split": [_split("a", [])]}, "members"),
        ({"traffic_split": [_split("a", [d, d])]}, "twice"),
        ({"traffic_split": [_split("bad name", [d])]}, "[A-Za-z0-9_.-]"),
        ({"traffic_split": [_split("x" * 33, [d])]}, "1..32"),
        ({"traffic_split": [_split("", [d])]}, "1..32"),
        ({"traffic_split": [_split("a", [d]), _split("a", [d])]}, "unique"),
        ({"traffic_split": [_split("a", [d], weight=0)]}, "sum to more than 0"),
        ({"traffic_split": [_split("a", [d], weight=-1)]}, "weight"),
        ({"traffic_split": [_split(f"s{i}", [d]) for i in range(17)]}, "at most 16"),
        # sticky
        (
            {"traffic_split": [_split("a", [d])], "sticky": "header"},
            "sticky_header: required",
        ),
        (
            {"traffic_split": [_split("a", [d])], "sticky": "team", "sticky_header": "x-a"},
            "only used when sticky is 'header'",
        ),
        ({"traffic_split": [], "sticky": "key"}, "sticky needs a traffic_split"),
        (
            {"traffic_split": [_split("a", [d])], "sticky": "header", "sticky_header": "x a"},
            "valid HTTP header name",
        ),
        # strategy / models
        ({"strategy": "Not-A-Name"}, "strategy"),
        ({"members": []}, "at least one model or define a traffic_split"),
        ({"members": _members(d, d)}, "duplicate"),
    ]
    router = _ok(_router(client, members=_members(d)))
    for body, needle in cases:
        splits = bool(body.get("traffic_split"))
        members = {"members": []} if splits else {}
        create = {"members": [] if splits else _members(d), **body}
        _422(_router(client, **create), needle)
        _422(_patch(client, router["id"], **members, **body), needle)
    # 16 splits is the limit, not an error; names use the gateway's charset.
    ok = _router(
        client,
        traffic_split=[_split(f"S-{i}.x_", [d]) for i in range(16)],
        sticky="key",
    )
    assert ok.status_code == 201, ok.text
    # Nulls for list fields are not "remove".
    _422(_patch(client, router["id"], traffic_split=None), "cannot be null")
    _422(_patch(client, router["id"], fallback_model_ids=None), "cannot be null")


def test_members_and_a_split_are_mutually_exclusive(client) -> None:
    d = _model(client)["id"]
    _422(
        _router(client, members=_members(d), traffic_split=[_split("a", [d])]),
        "members go under each split",
    )
    router = _ok(_router(client, members=_members(d)))
    _422(_patch(client, router["id"], traffic_split=[_split("a", [d])]), "under each split")
    _ok(_patch(client, router["id"], traffic_split=[_split("a", [d])], members=[]), 200)


def test_weighted_splits_need_positive_weight(client) -> None:
    d = _model(client)["id"]
    body = {"traffic_split": [_split("a", [d]), _split("b", [d], member_weight=0)]}
    _422(_router(client, **body), "positive total weight")
    router = _ok(_router(client, strategy="least_in_flight", **body))
    # Switching such a router to weighted is refused too.
    _422(_patch(client, router["id"], strategy="weighted"), "positive total weight")


async def test_concurrent_delete_and_reference_are_serialized(
    client, api_engine, api_settings, monkeypatch
) -> None:
    """Scope lock first: a router may not pick up a model being deleted, or be missed by it."""
    model = _model(client)
    model_id = uuid.UUID(model["id"])
    first_locked = asyncio.Event()
    second_attempted = asyncio.Event()
    original_lock = routing.lock_scope
    authz = RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), client.app.state.authorizer)
    async with (
        AsyncSession(api_engine, expire_on_commit=False) as first,
        AsyncSession(api_engine, expire_on_commit=False) as second,
    ):

        async def lock(session, settings):
            if session is second:
                await first_locked.wait()
                second_attempted.set()
            await original_lock(session, settings)
            session.info["scope_locked"] = True
            if session is first:
                first_locked.set()
                await second_attempted.wait()

        monkeypatch.setattr(routing, "lock_scope", lock)
        # Assert the ordering directly, so losing the early lock fails without a wait.
        original_get = first.get

        async def guarded_get(name, ident, **kw):
            assert first.info.get("scope_locked"), "scope must be locked before row read"
            return await original_get(name, ident, **kw)

        monkeypatch.setattr(first, "get", guarded_get)
        name = f"late-{suffix()}"

        async def add_router():
            try:
                return await routing.create_router(
                    RouterCreate(name=name, members=[RouterMemberIn(model_id=model_id)]),
                    authz,
                    second,
                    api_settings,
                )
            except ApiError as exc:
                assert exc.status == 422
                return exc

        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            deleted = tasks.create_task(routing.delete_model(model_id, authz, first, api_settings))
            added = tasks.create_task(add_router())
    assert first_locked.is_set() and second_attempted.is_set()
    assert deleted.result().status_code == 204
    assert isinstance(added.result(), ApiError)
    async with AsyncSession(api_engine) as check:
        assert await check.get(Model, model_id) is None
        assert await check.scalar(select(CatalogName.id).where(CatalogName.name == name)) is None


def test_delete_conflicts(client) -> None:
    dep = _model(client)
    split_only = _model(client)
    main = _ok(
        _router(
            client,
            members=[],
            fallback_model_ids=[dep["id"]],
            traffic_split=[_split("a", [split_only["id"]])],
        )
    )
    resp = client.delete(f"/api/v1/models/{dep['id']}", headers=H)
    assert resp.status_code == 409, resp.text
    assert main["name"] in resp.json()["error"]["message"]
    # A model used only by a split cannot be deleted either.
    resp = client.delete(f"/api/v1/models/{split_only['id']}", headers=H)
    assert resp.status_code == 409
    # Deleting the referencing router frees both.
    assert client.delete(f"/api/v1/routers/{main['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/models/{dep['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/models/{split_only['id']}", headers=H).status_code == 204


def test_fallbacks_need_two_attempts(client) -> None:
    backup = _model(client)
    _422(_router(client, fallback_model_ids=[backup["id"]], retry=None), "at least 2 attempts")
    resp = _router(client, fallback_model_ids=[backup["id"]], retry={"max_attempts": 1})
    _422(resp, "at least 2 attempts")
    router = _ok(_router(client))
    _422(_patch(client, router["id"], fallback_model_ids=[backup["id"]], retry=None), "2 attempts")
    _ok(_patch(client, router["id"], fallback_model_ids=[backup["id"]]), 200)

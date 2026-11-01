"""P2 routing API: health checks, circuit breakers, retries, fallbacks, traffic splits.

Every validation rule mirrors the gateway (snapshot.rs::validate / validate_split,
router.rs::Router::compile), plus the control-plane rule that fallbacks never form a cycle.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api import routing
from lumewarp_control_plane.api.schemas import ModelUpdate
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import AuditLog, ConfigOutbox, Model
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


def _router(client, **body):
    if "model_ids" not in body and "traffic_split" not in body:
        body["model_ids"] = [_model(client)["id"]]
    if body.get("fallback_router_ids"):
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
    if body.get("fallback_router_ids"):
        body.setdefault("retry", {})
    return client.patch(f"/api/v1/routers/{router_id}", json=body, headers=H)


# --------------------------------------------------------------------------- upstreams


def test_custom_strategy_router_create_patch_and_clear(client):
    router = _ok(
        _router(
            client,
            strategy="custom",
            custom_strategy="header_pin",
            strategy_params={"headers": ["x-pin"], "pin": 2},
        )
    )
    assert router["strategy"] == "custom"
    assert router["custom_strategy"] == "header_pin"
    assert router["strategy_params"]["pin"] == 2
    updated = _ok(_patch(client, router["id"], strategy_params={"pin": 3}), 200)
    assert updated["strategy_params"] == {"pin": 3}
    assert updated["custom_strategy"] == "header_pin"
    assert (
        _ok(_patch(client, router["id"], name=f"renamed-{suffix()}"), 200)["strategy"] == "custom"
    )
    cleared = _ok(_patch(client, router["id"], strategy="least_in_flight"), 200)
    assert cleared["custom_strategy"] is None and cleared["strategy_params"] == {}
    assert cleared["strategy"] == "least_in_flight"


@pytest.mark.parametrize(
    "body",
    [
        {"strategy": "custom"},
        {"strategy": "custom", "custom_strategy": "Bad"},
        {"strategy": "custom", "custom_strategy": "a-b"},
        {"strategy": "custom", "custom_strategy": "a" * 64},
        {"strategy": "custom", "custom_strategy": "ok", "strategy_params": []},
        {"strategy": "custom", "custom_strategy": "ok", "strategy_params": {"x": "a" * 16384}},
        {
            "strategy": "custom",
            "custom_strategy": "ok",
            "strategy_params": {"headers": ["bad name"]},
        },
        {"custom_strategy": "ok"},
        {"strategy_params": {"x": 1}},
    ],
)
def test_custom_strategy_router_validation(client, body):
    assert _router(client, **body).status_code == 422


@pytest.mark.parametrize(
    "header",
    ["authorization", "Authorization", "proxy-authorization", "cookie", "Set-Cookie", "X-API-Key"],
)
def test_custom_strategy_cannot_receive_credential_headers(client, header):
    assert (
        _router(
            client,
            strategy="custom",
            custom_strategy="header_pin",
            strategy_params={"headers": [header]},
        ).status_code
        == 422
    )
    router = _ok(_router(client))
    assert (
        _patch(
            client,
            router["id"],
            strategy="custom",
            custom_strategy="header_pin",
            strategy_params={"headers": [header]},
        ).status_code
        == 422
    )


def test_custom_strategy_patch_requires_name_and_non_null_params(client):
    router = _ok(_router(client))
    assert _patch(client, router["id"], strategy="custom").status_code == 422
    router = _ok(_patch(client, router["id"], strategy="custom", custom_strategy="ok"), 200)
    assert _patch(client, router["id"], custom_strategy=None).status_code == 422
    assert _patch(client, router["id"], strategy_params=None).status_code == 422


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


def test_router_p2_roundtrip_and_audit(client) -> None:
    upstream = _ok(_upstream(client))["id"]
    a, b, c = (_model(client, upstream) for _ in range(3))
    backup = _ok(_router(client, model_ids=[c["id"]]))
    backup2 = _ok(_router(client, model_ids=[c["id"]]))
    router = _ok(
        _router(
            client,
            strategy="vllm_load",
            model_ids=[],
            retry={"max_attempts": 3, "retry_on_status": [503, 500], "total_timeout_ms": 9000},
            fallback_router_ids=[backup2["id"], backup["id"]],
            traffic_split=[
                {"name": "stable", "weight": 90, "model_ids": [a["id"]]},
                {"name": "canary", "weight": 10, "model_ids": [b["id"], a["id"]]},
            ],
            sticky="header",
            sticky_header="X-Session-Id",
        )
    )
    assert router["strategy"] == "vllm_load"
    assert router["models"] == []
    assert router["retry"] == {
        "max_attempts": 3,
        "retry_on_status": [500, 503],
        "total_timeout_ms": 9000,
    }
    assert router["fallback_router_ids"] == [backup2["id"], backup["id"]]  # order kept
    assert router["traffic_split"] == [
        {"name": "stable", "weight": 90, "model_ids": [a["id"]]},
        {"name": "canary", "weight": 10, "model_ids": [b["id"], a["id"]]},
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
    assert patched["fallback_router_ids"] == router["fallback_router_ids"]
    patched = _ok(
        _patch(
            client,
            router["id"],
            traffic_split=[],
            sticky="none",
            model_ids=[a["id"]],
            fallback_router_ids=[],
            retry={},
            strategy="lowest_latency",
        ),
        200,
    )
    assert patched["traffic_split"] == [] and patched["fallback_router_ids"] == []
    assert patched["retry"] == {"max_attempts": None, "retry_on_status": [], "total_timeout_ms": 0}
    assert patched["strategy"] == "lowest_latency"


async def test_router_writes_audit_and_recompile(client, committed) -> None:
    outbox_before = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    backup = _ok(_router(client))
    router = _ok(_router(client, fallback_router_ids=[backup["id"]], retry={"max_attempts": 2}))
    _ok(_patch(client, router["id"], retry={"max_attempts": 3}), 200)
    outbox_after = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    # Two models + two upstreams + two routers + one router update.
    assert outbox_after == outbox_before + 7
    rows = (
        await committed.scalars(select(AuditLog).where(AuditLog.resource_id == router["id"]))
    ).all()
    actions = sorted(r.action for r in rows)
    assert actions == ["router.create", "router.update"]
    create = next(r for r in rows if r.action == "router.create")
    assert create.details["settings"]["fallback_router_ids"] == [backup["id"]]
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


def _split(name: str, model_ids: list[str], weight: int = 1) -> dict:
    return {"name": name, "weight": weight, "model_ids": model_ids}


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
        ({"fallback_router_ids": [unknown], "retry": {}}, "unknown routers"),
        # splits
        ({"traffic_split": [_split("a", [unknown])]}, "unknown models"),
        ({"traffic_split": [_split("a", [])]}, "model_ids"),
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
        ({"strategy": "fastest"}, "strategy"),
        ({"model_ids": []}, "at least one model or define a traffic_split"),
        ({"model_ids": [d, d]}, "duplicate"),
    ]
    router = _ok(_router(client, model_ids=[d]))
    for body, needle in cases:
        create = {"model_ids": [d], **body}
        _422(_router(client, **create), needle)
        _422(_patch(client, router["id"], **body), needle)
    # 16 splits is the limit, not an error; names use the gateway's charset.
    ok = _router(
        client,
        traffic_split=[_split(f"S-{i}.x_", [d]) for i in range(16)],
        sticky="key",
    )
    assert ok.status_code == 201, ok.text
    # Nulls for list fields are not "remove".
    _422(_patch(client, router["id"], traffic_split=None), "cannot be null")
    _422(_patch(client, router["id"], fallback_router_ids=None), "cannot be null")


def test_weighted_splits_need_positive_weight(client) -> None:
    zero = _model(client, weight=0)
    one = _model(client)
    body = {"traffic_split": [_split("a", [one["id"]]), _split("b", [zero["id"]])]}
    _422(_router(client, **body), "positive total weight")
    router = _ok(_router(client, strategy="least_in_flight", **body))
    # Switching such a router to weighted is refused too.
    _422(_patch(client, router["id"], strategy="weighted"), "positive total weight")
    # And a weight change that would starve a weighted split.
    weighted = _ok(_router(client, traffic_split=[_split("a", [one["id"]])]))
    resp = client.patch(f"/api/v1/models/{one['id']}", json={"weight": 0}, headers=H)
    _422(resp, f"router {weighted['name']!r}")


@pytest.mark.parametrize("split", [False, True])
async def test_concurrent_weight_zeroing_preserves_pool(
    client, api_engine, api_settings, monkeypatch, split
) -> None:
    a, b = _model(client), _model(client)
    ids = [a["id"], b["id"]]
    router = _ok(
        _router(
            client,
            **({"traffic_split": [_split("combined", ids)]} if split else {"model_ids": ids}),
        )
    )
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
        for session in (first, second):
            original_get = session.get

            async def guarded_get(name, ident, *, session=session, get=original_get):
                assert session.info.get("scope_locked"), "scope must be locked before row read"
                return await get(name, ident)

            monkeypatch.setattr(session, "get", guarded_get)

        async def zero(session, model_id):
            try:
                return await routing.update_model(
                    uuid.UUID(model_id),
                    ModelUpdate(weight=0),
                    authz,
                    session,
                    api_settings,
                )
            except ApiError as exc:
                assert exc.status == 422
                return exc

        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            left = tasks.create_task(zero(first, ids[0]))
            right = tasks.create_task(zero(second, ids[1]))
    assert first_locked.is_set() and second_attempted.is_set()
    assert not isinstance(left.result(), ApiError)
    assert isinstance(right.result(), ApiError)
    async with AsyncSession(api_engine) as check:
        weights = (
            await check.scalars(
                select(Model.weight).where(Model.id.in_([uuid.UUID(i) for i in ids]))
            )
        ).all()
        assert sorted(weights) == [0, 1]
        compiled = await compile_scope(check, api_settings.scope, require_local_membership=True)
        assert router["name"] in {r.name for r in compiled.snapshot.routers}


def test_fallback_self_and_cycles(client) -> None:
    a = _ok(_router(client))
    b = _ok(_router(client, fallback_router_ids=[a["id"]]))
    c = _ok(_router(client, fallback_router_ids=[b["id"]]))
    _422(_patch(client, a["id"], fallback_router_ids=[a["id"]]), "itself")
    # a -> c -> b -> a
    resp = _patch(client, a["id"], fallback_router_ids=[c["id"]])
    _422(resp, "cycle")
    assert f"{a['name']} -> {c['name']} -> {b['name']} -> {a['name']}" in resp.text
    _422(_patch(client, a["id"], fallback_router_ids=[b["id"]]), "cycle")
    # Duplicates are refused; a DAG with shared fallbacks is fine.
    _422(_patch(client, c["id"], fallback_router_ids=[a["id"], a["id"]]), "duplicate")
    _ok(_patch(client, c["id"], fallback_router_ids=[b["id"], a["id"]]), 200)


def test_delete_conflicts(client) -> None:
    dep = _model(client)
    split_only = _model(client)
    backup = _ok(_router(client, model_ids=[dep["id"]]))
    main = _ok(
        _router(
            client,
            model_ids=[dep["id"]],
            fallback_router_ids=[backup["id"]],
            traffic_split=[_split("a", [split_only["id"]])],
        )
    )
    resp = client.delete(f"/api/v1/routers/{backup['id']}", headers=H)
    assert resp.status_code == 409, resp.text
    assert main["name"] in resp.json()["error"]["message"]
    # A model used only by a split cannot be deleted either.
    resp = client.delete(f"/api/v1/models/{split_only['id']}", headers=H)
    assert resp.status_code == 409
    # Deleting the referencing router frees both.
    assert client.delete(f"/api/v1/routers/{main['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/routers/{backup['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/models/{split_only['id']}", headers=H).status_code == 204


def test_fallbacks_need_two_attempts(client) -> None:
    backup = _ok(_router(client))
    _422(_router(client, fallback_router_ids=[backup["id"]], retry=None), "at least 2 attempts")
    resp = _router(client, fallback_router_ids=[backup["id"]], retry={"max_attempts": 1})
    _422(resp, "at least 2 attempts")
    router = _ok(_router(client))
    _422(_patch(client, router["id"], fallback_router_ids=[backup["id"]], retry=None), "2 attempts")
    _ok(_patch(client, router["id"], fallback_router_ids=[backup["id"]]), 200)

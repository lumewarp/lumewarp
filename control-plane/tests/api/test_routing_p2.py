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
from lumewarp_control_plane.api.schemas import DeploymentUpdate
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import AuditLog, ConfigOutbox, Deployment
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests.api.helpers import ADMIN_AUTH as BOOTSTRAP_AUTH
from tests.api.helpers import auth, suffix, user_with_token

H = BOOTSTRAP_AUTH


def _upstream(client, **body):
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1", **body}
    return client.post("/api/v1/upstreams", json=body, headers=H)


def _deployment(client, upstream_id: str | None = None, **body) -> dict:
    if upstream_id is None:
        upstream_id = _upstream(client).json()["id"]
    body = {"name": f"dep-{suffix()}", "upstream_id": upstream_id, "upstream_model": "m", **body}
    resp = client.post("/api/v1/deployments", json=body, headers=H)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _route(client, **body):
    if "deployment_ids" not in body and "traffic_split" not in body:
        body["deployment_ids"] = [_deployment(client)["id"]]
    if body.get("fallback_route_ids"):
        # Fallbacks need at least two attempts.
        body.setdefault("retry", {})
    body = {"model": f"alias-{suffix()}", **body}
    return client.post("/api/v1/routes", json=body, headers=H)


def _ok(resp, code: int = 201) -> dict:
    assert resp.status_code == code, resp.text
    return resp.json()


def _422(resp, needle: str) -> None:
    assert resp.status_code == 422, resp.text
    assert needle in resp.json()["error"]["message"], resp.json()


def _patch(client, route_id: str, **body):
    if body.get("fallback_route_ids"):
        body.setdefault("retry", {})
    return client.patch(f"/api/v1/routes/{route_id}", json=body, headers=H)


# --------------------------------------------------------------------------- upstreams


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


# --------------------------------------------------------------------------- routes


def test_route_p2_roundtrip_and_audit(client) -> None:
    upstream = _ok(_upstream(client))["id"]
    a, b, c = (_deployment(client, upstream) for _ in range(3))
    backup = _ok(_route(client, deployment_ids=[c["id"]]))
    backup2 = _ok(_route(client, deployment_ids=[c["id"]]))
    route = _ok(
        _route(
            client,
            strategy="vllm_load",
            deployment_ids=[],
            retry={"max_attempts": 3, "retry_on_status": [503, 500], "total_timeout_ms": 9000},
            fallback_route_ids=[backup2["id"], backup["id"]],
            traffic_split=[
                {"name": "stable", "weight": 90, "deployment_ids": [a["id"]]},
                {"name": "canary", "weight": 10, "deployment_ids": [b["id"], a["id"]]},
            ],
            sticky="header",
            sticky_header="X-Session-Id",
        )
    )
    assert route["strategy"] == "vllm_load"
    assert route["deployments"] == []
    assert route["retry"] == {
        "max_attempts": 3,
        "retry_on_status": [500, 503],
        "total_timeout_ms": 9000,
    }
    assert route["fallback_route_ids"] == [backup2["id"], backup["id"]]  # order kept
    assert route["traffic_split"] == [
        {"name": "stable", "weight": 90, "deployment_ids": [a["id"]]},
        {"name": "canary", "weight": 10, "deployment_ids": [b["id"], a["id"]]},
    ]
    assert (route["sticky"], route["sticky_header"]) == ("header", "x-session-id")
    assert _ok(client.get(f"/api/v1/routes/{route['id']}", headers=H), 200) == route

    # PATCH: omitted fields are kept; fallbacks keep needing a retry policy; leaving
    # header stickiness drops the header.
    _422(_patch(client, route["id"], retry=None), "at least 2 attempts")
    _422(_patch(client, route["id"], retry={"max_attempts": 1}), "at least 2 attempts")
    patched = _ok(_patch(client, route["id"], retry={"max_attempts": 2}, sticky="team"), 200)
    assert patched["retry"]["max_attempts"] == 2
    assert (patched["sticky"], patched["sticky_header"]) == ("team", None)
    assert patched["traffic_split"] == route["traffic_split"]
    assert patched["fallback_route_ids"] == route["fallback_route_ids"]
    patched = _ok(
        _patch(
            client,
            route["id"],
            traffic_split=[],
            sticky="none",
            deployment_ids=[a["id"]],
            fallback_route_ids=[],
            retry={},
            strategy="lowest_latency",
        ),
        200,
    )
    assert patched["traffic_split"] == [] and patched["fallback_route_ids"] == []
    assert patched["retry"] == {"max_attempts": None, "retry_on_status": [], "total_timeout_ms": 0}
    assert patched["strategy"] == "lowest_latency"


async def test_route_writes_audit_and_recompile(client, committed) -> None:
    outbox_before = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    backup = _ok(_route(client))
    route = _ok(_route(client, fallback_route_ids=[backup["id"]], retry={"max_attempts": 2}))
    _ok(_patch(client, route["id"], retry={"max_attempts": 3}), 200)
    outbox_after = await committed.scalar(select(func.count()).select_from(ConfigOutbox))
    # Two deployments + two upstreams + two routes + one route update.
    assert outbox_after == outbox_before + 7
    rows = (
        await committed.scalars(select(AuditLog).where(AuditLog.resource_id == route["id"]))
    ).all()
    actions = sorted(r.action for r in rows)
    assert actions == ["route.create", "route.update"]
    create = next(r for r in rows if r.action == "route.create")
    assert create.details["settings"]["fallback_route_ids"] == [backup["id"]]
    assert create.details["settings"]["retry"]["max_attempts"] == 2


async def test_route_writes_need_superuser(client, committed) -> None:
    route = _ok(_route(client))
    _, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get(f"/api/v1/routes/{route['id']}", headers=auth(secret)).status_code == 200
    assert (
        client.patch(
            f"/api/v1/routes/{route['id']}", json={"retry": {}}, headers=auth(secret)
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


def _split(name: str, deployment_ids: list[str], weight: int = 1) -> dict:
    return {"name": name, "weight": weight, "deployment_ids": deployment_ids}


def test_route_validation_rules(client) -> None:
    dep = _deployment(client)
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
        ({"fallback_route_ids": [unknown], "retry": {}}, "unknown routes"),
        # splits
        ({"traffic_split": [_split("a", [unknown])]}, "unknown deployments"),
        ({"traffic_split": [_split("a", [])]}, "deployment_ids"),
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
        # strategy / deployments
        ({"strategy": "fastest"}, "strategy"),
        ({"deployment_ids": []}, "at least one deployment or define a traffic_split"),
        ({"deployment_ids": [d, d]}, "duplicate"),
    ]
    route = _ok(_route(client, deployment_ids=[d]))
    for body, needle in cases:
        create = {"deployment_ids": [d], **body}
        _422(_route(client, **create), needle)
        _422(_patch(client, route["id"], **body), needle)
    # 16 splits is the limit, not an error; names use the gateway's charset.
    ok = _route(
        client,
        traffic_split=[_split(f"S-{i}.x_", [d]) for i in range(16)],
        sticky="key",
    )
    assert ok.status_code == 201, ok.text
    # Nulls for list fields are not "remove".
    _422(_patch(client, route["id"], traffic_split=None), "cannot be null")
    _422(_patch(client, route["id"], fallback_route_ids=None), "cannot be null")


def test_weighted_splits_need_positive_weight(client) -> None:
    zero = _deployment(client, weight=0)
    one = _deployment(client)
    body = {"traffic_split": [_split("a", [one["id"]]), _split("b", [zero["id"]])]}
    _422(_route(client, **body), "positive total weight")
    route = _ok(_route(client, strategy="least_in_flight", **body))
    # Switching such a route to weighted is refused too.
    _422(_patch(client, route["id"], strategy="weighted"), "positive total weight")
    # And a weight change that would starve a weighted split.
    weighted = _ok(_route(client, traffic_split=[_split("a", [one["id"]])]))
    resp = client.patch(f"/api/v1/deployments/{one['id']}", json={"weight": 0}, headers=H)
    _422(resp, f"route {weighted['model']!r}")


@pytest.mark.parametrize("split", [False, True])
async def test_concurrent_weight_zeroing_preserves_pool(
    client, api_engine, api_settings, monkeypatch, split
) -> None:
    a, b = _deployment(client), _deployment(client)
    ids = [a["id"], b["id"]]
    route = _ok(
        _route(
            client,
            **({"traffic_split": [_split("combined", ids)]} if split else {"deployment_ids": ids}),
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

            async def guarded_get(model, ident, *, session=session, get=original_get):
                assert session.info.get("scope_locked"), "scope must be locked before row read"
                return await get(model, ident)

            monkeypatch.setattr(session, "get", guarded_get)

        async def zero(session, deployment_id):
            try:
                return await routing.update_deployment(
                    uuid.UUID(deployment_id),
                    DeploymentUpdate(weight=0),
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
                select(Deployment.weight).where(Deployment.id.in_([uuid.UUID(i) for i in ids]))
            )
        ).all()
        assert sorted(weights) == [0, 1]
        compiled = await compile_scope(
            check, api_settings.scope, router_plugins=[], require_local_membership=True
        )
        assert route["model"] in {r.model for r in compiled.snapshot.routes}


def test_fallback_self_and_cycles(client) -> None:
    a = _ok(_route(client))
    b = _ok(_route(client, fallback_route_ids=[a["id"]]))
    c = _ok(_route(client, fallback_route_ids=[b["id"]]))
    _422(_patch(client, a["id"], fallback_route_ids=[a["id"]]), "itself")
    # a -> c -> b -> a
    resp = _patch(client, a["id"], fallback_route_ids=[c["id"]])
    _422(resp, "cycle")
    assert f"{a['model']} -> {c['model']} -> {b['model']} -> {a['model']}" in resp.text
    _422(_patch(client, a["id"], fallback_route_ids=[b["id"]]), "cycle")
    # Duplicates are refused; a DAG with shared fallbacks is fine.
    _422(_patch(client, c["id"], fallback_route_ids=[a["id"], a["id"]]), "duplicate")
    _ok(_patch(client, c["id"], fallback_route_ids=[b["id"], a["id"]]), 200)


def test_delete_conflicts(client) -> None:
    dep = _deployment(client)
    split_only = _deployment(client)
    backup = _ok(_route(client, deployment_ids=[dep["id"]]))
    main = _ok(
        _route(
            client,
            deployment_ids=[dep["id"]],
            fallback_route_ids=[backup["id"]],
            traffic_split=[_split("a", [split_only["id"]])],
        )
    )
    resp = client.delete(f"/api/v1/routes/{backup['id']}", headers=H)
    assert resp.status_code == 409, resp.text
    assert main["model"] in resp.json()["error"]["message"]
    # A deployment used only by a split cannot be deleted either.
    resp = client.delete(f"/api/v1/deployments/{split_only['id']}", headers=H)
    assert resp.status_code == 409
    # Deleting the referencing route frees both.
    assert client.delete(f"/api/v1/routes/{main['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/routes/{backup['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/v1/deployments/{split_only['id']}", headers=H).status_code == 204


def test_fallbacks_need_two_attempts(client) -> None:
    backup = _ok(_route(client))
    _422(_route(client, fallback_route_ids=[backup["id"]], retry=None), "at least 2 attempts")
    resp = _route(client, fallback_route_ids=[backup["id"]], retry={"max_attempts": 1})
    _422(resp, "at least 2 attempts")
    route = _ok(_route(client))
    _422(_patch(client, route["id"], fallback_route_ids=[backup["id"]], retry=None), "2 attempts")
    _ok(_patch(client, route["id"], fallback_route_ids=[backup["id"]]), 200)

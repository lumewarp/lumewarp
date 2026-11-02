"""Strategies: GET /strategies and what a router write accepts (docs/router-strategies.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import AuditLog, GatewaySession, GatewayToken
from tests.api.helpers import ADMIN_AUTH, auth, report_strategies, suffix, user_with_token

H = ADMIN_AUTH
PREFIX = "strategies-test-"
PARAMS = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 0, "maximum": 10}},
    "additionalProperties": False,
}
MEMBER = {
    "type": "object",
    "properties": {"priority": {"type": "integer", "minimum": 1}},
    "required": ["priority"],
    "additionalProperties": False,
}
FAILOVER = (PARAMS, MEMBER)


@pytest.fixture
async def report(committed: AsyncSession, api_scope: str) -> AsyncIterator:
    """Report as a gateway; every gateway it made is gone after the test."""

    async def report_as(gateway: str, strategies: dict, **kwargs) -> None:
        await report_strategies(committed, api_scope, f"{PREFIX}{gateway}", strategies, **kwargs)

    yield report_as
    await committed.rollback()
    await committed.execute(delete(GatewayToken).where(GatewayToken.gateway_id.like(f"{PREFIX}%")))
    await committed.execute(
        delete(GatewaySession).where(GatewaySession.gateway_id.like(f"{PREFIX}%"))
    )
    await committed.commit()


def _name(strategy: str = "failover") -> str:
    return f"{strategy}_{suffix().replace('-', '_').lower()}"


def _model(client) -> str:
    upstream = client.post(
        "/api/v1/upstreams",
        json={"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"},
        headers=H,
    ).json()
    resp = client.post(
        "/api/v1/models",
        json={"name": f"m-{suffix()}", "upstream_id": upstream["id"], "upstream_model": "m"},
        headers=H,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create(client, strategy: str, members: list[dict] | None = None, **body):
    if members is None and "traffic_split" not in body:
        members = [{"model_id": _model(client), "params": {"priority": 1}}]
    return client.post(
        "/api/v1/routers",
        json={"name": f"r-{suffix()}", "strategy": strategy, "members": members or [], **body},
        headers=H,
    )


def _patch(client, router_id: str, **body):
    return client.patch(f"/api/v1/routers/{router_id}", json=body, headers=H)


def _422(resp, *needles: str) -> str:
    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["message"]
    for needle in needles:
        assert needle in message, message
    return message


def _strategy(client, name: str) -> dict | None:
    resp = client.get("/api/v1/strategies", headers=H)
    assert resp.status_code == 200, resp.text
    return next((item for item in resp.json()["items"] if item["name"] == name), None)


async def test_built_ins_are_listed_and_usable_with_no_gateway_connected(client, report):
    items = client.get("/api/v1/strategies", headers=H).json()["items"]
    builtin = {item["name"]: item for item in items if item["builtin"]}
    assert set(builtin) == {"weighted", "least_in_flight", "lowest_latency", "vllm_load"}
    assert builtin["weighted"]["uses_weight"] is True
    assert builtin["least_in_flight"]["uses_weight"] is False
    assert builtin["vllm_load"]["needs"] == ["vllm_load"]
    assert builtin["weighted"]["params_schema"]["additionalProperties"] is False
    model = _model(client)
    for name in builtin:
        resp = _create(client, name, [{"model_id": model}])
        assert resp.status_code == 201, resp.text
        assert resp.json()["config_invalid"] is False


def test_built_ins_refuse_settings_they_do_not_have(client):
    model = _model(client)
    _422(_create(client, "weighted", [{"model_id": model}], params={"x": 1}), "takes no params")
    _422(_create(client, "weighted", [{"model_id": model, "params": {"x": 1}}]), "members[0]")


def test_a_strategy_no_gateway_reports_is_422(client):
    name = _name("nobody")
    _422(_create(client, name), "no gateway reports", name)
    # force is for gateways that lack it, not for a strategy that does not exist.
    _422(_create(client, name, force=True), "no gateway reports")


async def test_settings_are_validated_against_the_schema(client, report):
    name = _name()
    await report("a", {name: FAILOVER})
    model = _model(client)
    good = [{"model_id": model, "params": {"priority": 1}}]
    assert _create(client, name, good, params={"limit": 3}).status_code == 201
    _422(_create(client, name, good, params={"limit": "3"}), "params.limit", "integer")
    _422(_create(client, name, good, params={"limit": 11}), "params.limit", "at most 10")
    _422(_create(client, name, good, params={"nope": 1}), "nope")
    _422(_create(client, name, [{"model_id": model}]), "members[0].params", "priority")
    _422(
        _create(client, name, [{"model_id": model, "params": {"priority": 0}}]),
        "members[0].params.priority",
    )
    split = {"name": "a", "weight": 1, "members": [{"model_id": model, "params": {"priority": 1}}]}
    bad = {"name": "b", "weight": 1, "members": [{"model_id": model, "params": {"priority": "x"}}]}
    assert _create(client, name, traffic_split=[split]).status_code == 201
    _422(_create(client, name, traffic_split=[split, bad]), "traffic_split[1].members[0].params")

    router = _create(client, name, good).json()
    _422(_patch(client, router["id"], params={"limit": 99}), "params.limit")
    _422(_patch(client, router["id"], members=[{"model_id": model}]), "members[0].params")
    _422(_patch(client, router["id"], strategy="weighted"), "members[0]")
    assert _patch(client, router["id"], params={"limit": 1}).status_code == 200


async def test_headers_are_reserved_and_credentials_stay_blocked(client, report):
    name = _name()
    await report("a", {name: FAILOVER})
    # additionalProperties: false does not apply: the gateway removes `headers` first.
    resp = _create(client, name, params={"headers": ["x-pin"], "limit": 1})
    assert resp.status_code == 201, resp.text
    assert resp.json()["params"]["headers"] == ["x-pin"]
    _422(_create(client, name, params={"headers": ["authorization"]}), "headers")

    widget = _name()
    schema = {
        "type": "object",
        "properties": {
            "copy": {
                "type": "array",
                "items": {"type": "string"},
                "x-lumewarp-widget": "header-list",
            }
        },
    }
    await report("b", {widget: (schema, {"type": "object"})})
    assert _create(client, widget, params={"copy": ["x-team"]}).status_code == 201
    _422(_create(client, widget, params={"copy": ["Cookie"]}), "params.copy")


async def test_a_connected_gateway_lacking_it_needs_force_and_audit(
    client, report, committed: AsyncSession
):
    name = _name()
    await report("has-it", {name: FAILOVER}, connected=True)
    await report("lacks-it-1", {}, connected=True)
    await report("lacks-it-2", {}, connected=True)
    message = _422(_create(client, name), f"{PREFIX}lacks-it-1", f"{PREFIX}lacks-it-2", "force")
    assert f"{PREFIX}has-it" not in message

    forced = _create(client, name, force=True)
    assert forced.status_code == 201, forced.text
    router = forced.json()
    rows = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == router["id"], AuditLog.action == "router.force_save"
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["strategy"] == name
    assert rows[0].details["missing_gateways"] == [f"{PREFIX}lacks-it-1", f"{PREFIX}lacks-it-2"]

    # force does not excuse settings that fail the schema of the gateways that have it.
    _422(_create(client, name, force=True, params={"limit": 99}), "params.limit")

    # Update: same rule, same audit. An unrelated change is not a save of the strategy.
    plain = _create(client, "least_in_flight", [{"model_id": _model(client)}]).json()
    # Built-ins are advertised by these gateways, so nothing is missing and nothing audited.
    assert _patch(client, plain["id"], force=True, sticky="none").status_code == 200
    members = [{"model_id": _model(client), "params": {"priority": 1}}]
    _422(_patch(client, plain["id"], strategy=name, members=members), "lacks-it-1")
    resp = _patch(client, plain["id"], strategy=name, members=members, force=True)
    assert resp.status_code == 200, resp.text
    assert _patch(client, plain["id"], name=f"renamed-{suffix()}").status_code == 200
    rows = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == plain["id"], AuditLog.action == "router.force_save"
            )
        )
    ).all()
    assert len(rows) == 1


async def test_a_built_in_a_connected_gateway_lacks_also_needs_force(client, report):
    await report("old-build", {}, connected=True, capabilities=["strategy:weighted"])
    model = _model(client)
    assert _create(client, "weighted", [{"model_id": model}]).status_code == 201
    _422(_create(client, "vllm_load", [{"model_id": model}]), "old-build")
    assert _create(client, "vllm_load", [{"model_id": model}], force=True).status_code == 201


async def test_force_is_silent_when_nothing_is_missing(client, report, committed: AsyncSession):
    name = _name()
    await report("a", {name: FAILOVER}, connected=True)
    router = _create(client, name, force=True).json()
    rows = await committed.scalars(select(AuditLog).where(AuditLog.resource_id == router["id"]))
    assert [row.action for row in rows] == ["router.create"]


async def test_rolling_upgrade_accepts_only_what_passes_every_connected_schema(client, report):
    name = _name()
    low = {"type": "object", "properties": {"n": {"type": "integer", "maximum": 5}}}
    high = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 3}}}
    member = {"type": "object"}
    await report("low", {name: (low, member)}, connected=True)
    await report("high", {name: (high, member)}, connected=True)
    assert _create(client, name, params={"n": 4}).status_code == 201
    _422(_create(client, name, params={"n": 6}), "at most 5")
    _422(_create(client, name, params={"n": 2}), "at least 3")
    item = _strategy(client, name)
    assert {g["gateway_id"] for g in item["gateways"]} == {f"{PREFIX}low", f"{PREFIX}high"}
    assert item["missing_gateways"] == []


async def test_revoked_and_long_unseen_gateways_no_longer_count(client, report):
    revoked, stale, recent = _name("revoked"), _name("stale"), _name("recent")
    await report("revoked", {revoked: FAILOVER}, revoked=True)
    # Revoked but its stream not yet closed: it counts neither as support nor as missing.
    await report("revoked-live", {revoked: FAILOVER}, connected=True, revoked=True)
    old = datetime.now(UTC) - timedelta(days=31)
    await report("stale", {stale: FAILOVER}, last_seen_at=old)
    edge = datetime.now(UTC) - timedelta(days=29)
    await report("recent", {recent: FAILOVER}, last_seen_at=edge)

    assert _strategy(client, revoked) is None
    assert _strategy(client, stale) is None
    _422(_create(client, revoked), "no gateway reports")
    _422(_create(client, stale), "no gateway reports")

    item = _strategy(client, recent)
    assert item["params_schema"] == PARAMS
    assert item["gateways"][0]["connected"] is False
    assert item["missing_gateways"] == []
    # Offline is enough to edit, and its schema still validates.
    assert _create(client, recent).status_code == 201
    _422(_create(client, recent, params={"limit": 99}), "params.limit")


async def test_connected_for_longer_than_the_window_still_counts(client, report):
    name = _name()
    long_ago = datetime.now(UTC) - timedelta(days=45)
    await report("steady", {name: FAILOVER}, connected=True, last_seen_at=long_ago)
    assert _create(client, name).status_code == 201


async def test_forms_use_the_newest_connected_report(client, report, committed):
    name = _name()
    older = {"type": "object", "title": "older"}
    newer = {"type": "object", "title": "newer"}
    await report("older", {name: (older, {"type": "object"})})
    await report("newer", {name: (newer, {"type": "object"})}, connected=True)
    assert _strategy(client, name)["params_schema"] == newer


async def test_capability_without_a_report_counts_but_cannot_validate(client, report):
    name = _name()
    await report("old", {}, connected=True, capabilities=[f"strategy:{name}"])
    item = _strategy(client, name)
    assert item["params_schema"] is None
    assert item["gateways"][0]["digest"] is None
    assert _create(client, name, params={"anything": 1}).status_code == 201


@pytest.mark.parametrize(
    "schema",
    [
        json.dumps({"type": "object", "description": "x" * (17 * 1024)}),
        '{"type":"object","properties":{"a":{"type":"string","pattern":"^(a+)+$"}}}',
        '{"$ref":"http://169.254.169.254/latest"}',
        "not json",
    ],
    ids=["oversized", "regex", "remote-ref", "not-json"],
)
async def test_an_unusable_schema_means_cannot_validate(client, report, schema):
    name = _name()
    await report("a", {name: (schema, {"type": "object"})})
    item = _strategy(client, name)
    assert item["params_schema"] is None
    assert item["member_schema"] == {"type": "object"}
    assert item["schema_problems"]
    assert (
        _create(client, name, params={"a": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!"}).status_code == 201
    )


async def test_stored_settings_invalid_after_upgrade_are_flagged_and_kept(client, report):
    name = _name()
    await report("a", {name: FAILOVER})
    router = _create(client, name, params={"limit": 9}).json()
    assert router["config_invalid"] is False
    assert router["config_problems"] == []

    stricter = dict(PARAMS, properties={"limit": {"type": "integer", "maximum": 5}})
    await report("a", {name: (stricter, MEMBER)})
    got = client.get(f"/api/v1/routers/{router['id']}", headers=H).json()
    assert got["config_invalid"] is True
    assert "params.limit" in got["config_problems"][0]
    assert got["params"] == {"limit": 9}, "the data is not altered"
    listed = next(
        r
        for r in client.get("/api/v1/routers", params={"limit": 200}, headers=H).json()["items"]
        if r["id"] == router["id"]
    )
    assert listed["config_invalid"] is True

    # What does not touch the settings still saves; changing them must fix them.
    renamed = _patch(client, router["id"], name=f"renamed-{suffix()}")
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["config_invalid"] is True
    _422(_patch(client, router["id"], params={"limit": 6}), "params.limit")
    fixed = _patch(client, router["id"], params={"limit": 5})
    assert fixed.status_code == 200
    assert fixed.json()["config_invalid"] is False


async def test_a_router_whose_strategy_disappeared_is_flagged(client, report):
    name = _name()
    await report("a", {name: FAILOVER})
    router = _create(client, name).json()
    await report("a", {})
    got = client.get(f"/api/v1/routers/{router['id']}", headers=H).json()
    assert got["config_invalid"] is True
    assert "no gateway reports" in got["config_problems"][0]


async def test_strategies_needs_routing_read(client, committed: AsyncSession):
    assert client.get("/api/v1/strategies").status_code == 401
    _, token = await user_with_token(committed)
    await committed.commit()
    resp = client.get("/api/v1/strategies", headers=auth(token))
    assert resp.status_code == 200

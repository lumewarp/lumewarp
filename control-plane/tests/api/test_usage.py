"""Owner grouping, usage filters, readable teams and monthly totals."""

import uuid
from datetime import UTC, datetime

import pytest

from lumewarp_control_plane.models import (
    CatalogName,
    RequestOutcome,
    ScopeType,
    TeamRole,
    TrafficKind,
    UsageEvent,
    UsageTotal,
)
from lumewarp_control_plane.usage import ingest
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, usage_total, user_with_token
from tests.factories import (
    make_alias,
    make_key,
    make_member,
    make_model,
    make_org,
    make_org_admin,
    make_router,
    make_service_account,
    make_team,
)
from tests.usage.test_catalog_provenance import CREATED, RENAMED, key_for, rename_at, wire

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
TS = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)


def query(client, headers=ADMIN_AUTH, **params):
    return client.get(
        "/api/v1/usage",
        params={"from": START.isoformat(), "to": END.isoformat(), **params},
        headers=headers,
    )


def window(client, start: datetime, end: datetime, headers=ADMIN_AUTH, **params):
    return client.get(
        "/api/v1/usage",
        params={"from": start.isoformat(), "to": end.isoformat(), **params},
        headers=headers,
    )


def called_event(
    key,
    *,
    requested: str,
    ts: datetime = TS,
    alias: str = "",
    router: str = "",
    model: str = "served",
    alias_id: uuid.UUID | None = None,
    router_id: uuid.UUID | None = None,
    model_id: uuid.UUID | None = None,
    **kwargs,
) -> UsageEvent:
    """A stored row that names what the client called and its resolved catalog ids."""
    event = usage_event(key, ts=ts, requested=requested, **kwargs)
    event.alias = alias
    event.router = router
    event.model = model
    event.alias_id = alias_id
    event.router_id = router_id
    event.model_id = model_id
    return event


async def test_grouping_filters_and_cursor_pagination(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    user, _ = await user_with_token(committed)
    account = await make_service_account(committed, team)
    user_key = await make_key(committed, team, user)
    sa_key = await make_key(committed, team, account)
    for _ in range(3):
        committed.add(usage_event(user_key, ts=TS, requested="alpha"))
    committed.add(
        usage_event(user_key, ts=TS, requested="beta", prompt_tokens=1, completion_tokens=1)
    )
    committed.add(usage_event(sa_key, ts=TS, requested="gamma", cost_known=False))
    # The end of the range is exclusive.
    committed.add(usage_event(sa_key, ts=END, requested="excluded"))
    await committed.commit()
    response = query(client, group_by="day,requested", team_id=str(team.id))
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {r["requested"] for r in rows} == {"alpha", "beta", "gamma"}
    assert {r["date"] for r in rows} == {"2026-03-10"}
    alpha = next(r for r in rows if r["requested"] == "alpha")
    assert {
        k: alpha[k]
        for k in ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "cost_nano_usd")
    } == {
        "requests": 3,
        "prompt_tokens": 30,
        "completion_tokens": 15,
        "total_tokens": 45,
        "cost_nano_usd": 3000,
    }
    owners = query(client, group_by="owner", team_id=str(team.id))
    assert owners.status_code == 200, owners.text
    assert {(r["owner_kind"], r["owner_id"]): r["requests"] for r in owners.json()["items"]} == {
        ("user", str(user.id)): 4,
        ("service_account", str(account.id)): 1,
    }
    by_key = query(client, group_by="key", team_id=str(team.id))
    assert by_key.status_code == 200, by_key.text
    key_rows = {row["key"]["id"]: row["key"] for row in by_key.json()["items"]}
    assert key_rows[str(sa_key.id)] == {
        "id": str(sa_key.id),
        "name": sa_key.name,
        "display_prefix": sa_key.display_prefix,
        "team_id": str(team.id),
        "team_name": team.name,
        "org_id": str(org.id),
        "org_name": org.name,
        "owner_kind": "service_account",
        "user_id": None,
        "service_account_id": str(account.id),
        "service_account_name": account.name,
        "lifecycle": "active",
    }
    assert key_rows[str(user_key.id)]["owner_kind"] == "user"
    assert key_rows[str(user_key.id)]["service_account_name"] is None
    # The key dimension is the only grouping that carries a ref.
    assert all(row["key"] is None for row in rows)
    for filters, models in (
        ({"user_id": str(user.id)}, {"alpha", "beta"}),
        ({"service_account_id": str(account.id)}, {"gamma"}),
        ({"key_id": str(sa_key.id)}, {"gamma"}),
        ({"requested": "beta"}, {"beta"}),
    ):
        filtered = query(client, group_by="requested", team_id=str(team.id), **filters)
        assert filtered.status_code == 200, filtered.text
        assert {r["requested"] for r in filtered.json()["items"]} == models
    for groups in ("requested", "day,requested", "owner", "owner,key"):
        first = query(client, group_by=groups, team_id=str(team.id), limit=1).json()
        assert first["next_cursor"] is not None
        rest = query(
            client, group_by=groups, team_id=str(team.id), cursor=first["next_cursor"], limit=50
        )
        assert rest.status_code == 200, rest.text
        combined = first["items"] + rest.json()["items"]
        whole = query(client, group_by=groups, team_id=str(team.id)).json()["items"]
        assert combined == whole
    gamma = next(r for r in rows if r["requested"] == "gamma")
    assert gamma["unknown_cost_count"] == 1
    default = query(client, team_id=str(team.id))
    assert default.json()["items"] == rows


async def test_monthly_team_totals_fast_path(client, committed, api_scope) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, account)
    committed.add(usage_event(key, ts=TS))
    committed.add(
        usage_total(
            ScopeType.TEAM,
            team.id,
            window_start=START,
            requests=7,
            prompt_tokens=70,
            completion_tokens=30,
            cost_nano_usd=7000,
            unknown_cost_count=1,
        )
    )
    # Owner totals must not leak into the team aggregate.
    committed.add(
        usage_total(ScopeType.SERVICE_ACCOUNT, account.id, window_start=START, requests=99)
    )
    await committed.flush()
    total = await committed.get(UsageTotal, (api_scope, ScopeType.TEAM, team.id, START))
    total.audio_ms = 9876
    await committed.commit()
    from sqlalchemy import event

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = query(client, group_by="team", team_id=str(team.id))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("usage_events" in statement for statement in statements)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    row = response.json()["items"][0]
    assert (row["team_id"], row["requests"], row["total_tokens"]) == (str(team.id), 7, 100)
    assert row["cost_nano_usd"] == 7000
    assert row["unknown_cost_count"] == 1
    assert row["audio_ms"] == 9876
    # The totals table stores no durations or attempt detail: those amounts are 0/null.
    assert row["fallback_count"] == 0 and row["retried_count"] == 0
    assert row["p50_duration_ms"] is None
    assert row["p95_duration_ms"] is None
    assert row["p99_duration_ms"] is None
    events = query(client, group_by="team", team_id=str(team.id), requested="mock")
    assert events.json()["items"][0]["requests"] == 1


async def test_usage_visibility_and_filter_rules(client, committed) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    owner, token = await user_with_token(committed)
    await make_member(committed, team, owner)
    own = await make_key(committed, team, owner)
    account = await make_service_account(committed, other)
    foreign = await make_key(committed, other, account)
    committed.add(usage_event(own, ts=TS, requested="own"))
    committed.add(usage_event(foreign, ts=TS, requested="foreign"))
    await committed.commit()
    headers = auth(token)
    visible = query(client, headers, group_by="requested")
    assert visible.status_code == 200
    assert [r["requested"] for r in visible.json()["items"]] == ["own"]
    by_key = query(client, headers, group_by="requested", key_id=str(own.id))
    assert by_key.json()["items"] == visible.json()["items"]
    for filters in (
        {"team_id": str(other.id)},
        {"team_id": str(uuid.uuid4())},
        {"key_id": str(foreign.id)},
        {"key_id": str(uuid.uuid4())},
        {"key_id": str(own.id), "team_id": str(other.id)},
    ):
        assert query(client, headers, **filters).status_code == 404
    for filters in (
        {"user_id": str(owner.id)},
        {"service_account_id": str(account.id)},
        {"team_id": str(team.id), "user_id": str(owner.id), "service_account_id": str(account.id)},
    ):
        assert query(client, headers, **filters).status_code == 422
    outsider, outsider_token = await user_with_token(committed)
    await committed.commit()
    assert query(client, auth(outsider_token)).json()["items"] == []


async def test_a_plain_member_sees_whole_team_totals_but_not_other_owners(
    client, committed
) -> None:
    team = await make_team(committed)
    member, token = await user_with_token(committed)
    colleague, _ = await user_with_token(committed)
    admin, admin_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    own = await make_key(committed, team, member)
    other = await make_key(committed, team, colleague)
    account = await make_service_account(committed, team)
    sa_key = await make_key(committed, team, account)
    for key in (own, own, other, sa_key):
        committed.add(usage_event(key, ts=TS))
    await committed.commit()
    headers = auth(token)

    def requests_for(**params):
        response = query(client, headers, **params)
        assert response.status_code == 200, response.text
        return sum(row["requests"] for row in response.json()["items"])

    # A shape that is a team total (no owner/key split, no owner/key filter) is the whole
    # team's, so it includes the colleague's and the service account's requests.
    for params in (
        {},
        {"group_by": "requested"},
        {"group_by": "day"},
        {"group_by": "team"},
        {"group_by": "team", "team_id": str(team.id)},
        {"group_by": "endpoint", "team_id": str(team.id)},
    ):
        assert requests_for(**params) == 4, params

    # Nothing that names where a request was served is ever populated for a member.
    for row in query(client, headers, group_by="requested").json()["items"]:
        assert row["model"] is None and row["model_id"] is None
        assert row["alias"] is None and row["alias_id"] is None
        assert row["router"] is None and row["router_id"] is None
        assert row["upstream_model"] is None and row["upstream_models"] == []
        assert row["served_by"] == []
        assert row["via_aliases"] == [] and row["via_routers"] == []

    # A per-owner or per-key shape keeps the old predicate: in a team the member does not
    # administer, only their own user-owned keys are visible.
    assert requests_for(group_by="owner") == 2
    assert requests_for(group_by="key") == 2
    assert requests_for(group_by="owner,requested") == 2
    assert requests_for(group_by="key,requested") == 2
    for params in (
        {"group_by": "requested", "team_id": str(team.id), "user_id": str(colleague.id)},
        {
            "group_by": "requested",
            "team_id": str(team.id),
            "service_account_id": str(account.id),
        },
    ):
        assert requests_for(**params) == 0, params
    assert requests_for(group_by="requested", team_id=str(team.id), user_id=str(member.id)) == 2
    people = query(client, headers, group_by="owner").json()["items"]
    assert {(row["owner_kind"], row["owner_id"]) for row in people} == {("user", str(member.id))}
    keys = query(client, headers, group_by="key").json()["items"]
    assert {row["key"]["id"] for row in keys} == {str(own.id)}

    # Another member's key cannot be selected at all.
    forbidden = query(client, headers, group_by="key", key_id=str(other.id))
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["error"]["code"] == "forbidden"
    forbidden = query(client, headers, group_by="key", key_id=str(sa_key.id))
    assert forbidden.status_code == 403, forbidden.text
    # Own rows through the key filter still work.
    assert requests_for(group_by="key", key_id=str(own.id)) == 2

    # A team admin keeps the whole-team view.
    def admin_requests(**params):
        response = query(client, auth(admin_token), **params)
        assert response.status_code == 200, response.text
        return sum(row["requests"] for row in response.json()["items"])

    assert admin_requests(group_by="team", team_id=str(team.id)) == 4
    team_rows = query(client, auth(admin_token), group_by="owner", team_id=str(team.id)).json()
    assert sum(row["requests"] for row in team_rows["items"]) == 4
    assert {row["owner_kind"] for row in team_rows["items"]} == {"user", "service_account"}
    # Installation admin unchanged: whole-team rows, no owner predicate.
    overall = query(client, group_by="owner", team_id=str(team.id)).json()["items"]
    assert sum(row["requests"] for row in overall) == 4


async def test_catalog_filters_grouping_rename_and_pagination(client, committed, api_scope):
    key = await key_for(committed)
    suffix = uuid.uuid4().hex
    original = f"old-{suffix}"
    current = f"new-{suffix}"
    model = await make_model(committed, original, created_at=CREATED)
    pool = await make_router(committed, f"pool-{suffix}", members=[model], created_at=CREATED)
    alias = await make_alias(committed, f"nick-{suffix}", pool)
    alias.created_at = CREATED
    await committed.flush()
    first = wire(key, model=original, upstream_model="upstream-before")
    await rename_at(committed, model, current)
    second = wire(
        key,
        model=current,
        alias=f"nick-{suffix}",
        router=f"pool-{suffix}",
        upstream_model="upstream-after",
        timestamp=RENAMED,
    )
    # Delayed pre-rename event and post-rename event enter through real ingest.
    assert await ingest(committed, "gateway", [first, second], zone=api_scope) == 2
    old_alias = f"nick-{suffix}"
    old_router = f"pool-{suffix}"
    await rename_at(committed, alias, f"nick-new-{suffix}")
    await rename_at(committed, pool, f"pool-new-{suffix}")
    await committed.commit()
    base = {"team_id": str(key.team_id)}
    for filters, expected in (
        ({"model": current}, 2),
        ({"model_id": str(model.id)}, 2),
        ({"upstream_model": "upstream-before"}, 1),
        ({"alias": f"nick-new-{suffix}"}, 1),
        ({"alias_id": str(alias.id)}, 1),
        ({"router": f"pool-new-{suffix}"}, 1),
        ({"router_id": str(pool.id)}, 1),
        ({"alias_missing": "true"}, 1),
        ({"router_missing": "true"}, 1),
        ({"alias_missing": "false"}, 1),
        ({"router_missing": "false"}, 1),
        ({"model": original}, 0),
    ):
        response = query(client, group_by="", **base, **filters)
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["requests"] == expected
    grouped = query(client, group_by="model", **base).json()["items"]
    assert len(grouped) == 1
    assert (grouped[0]["model"], grouped[0]["model_id"], grouped[0]["requests"]) == (
        current,
        str(model.id),
        2,
    )
    assert grouped[0]["upstream_models"] == ["upstream-after", "upstream-before"]
    assert grouped[0]["via_aliases"] == [{"id": str(alias.id), "name": f"nick-new-{suffix}"}]
    assert grouped[0]["via_routers"] == [{"id": str(pool.id), "name": f"pool-new-{suffix}"}]
    assert old_alias != grouped[0]["via_aliases"][0]["name"]
    assert old_router != grouped[0]["via_routers"][0]["name"]
    for groups in ("alias", "router", "upstream_model", "model,alias,router"):
        whole = query(client, group_by=groups, **base).json()["items"]
        first_page = query(client, group_by=groups, limit=1, **base).json()
        assert first_page["next_cursor"] is not None
        rest = query(client, group_by=groups, cursor=first_page["next_cursor"], **base).json()
        assert first_page["items"] + rest["items"] == whole
        assert all(row["served_by"] == [{"id": str(model.id), "name": current}] for row in whole)


async def test_literal_none_distinct_from_missing_and_deleted_names(client, committed, api_scope):
    key = await key_for(committed)
    model = await make_model(committed, f"served-{uuid.uuid4().hex}", created_at=CREATED)
    alias = await make_alias(committed, "none", model)
    alias.created_at = CREATED
    await committed.flush()
    assert (
        await ingest(
            committed,
            "gateway",
            [
                wire(key, model=model.name),
                wire(key, model=model.name, alias="none"),
            ],
            zone=api_scope,
        )
        == 2
    )
    await committed.commit()
    base = {"team_id": str(key.team_id), "group_by": "alias"}
    missing = query(client, alias_missing="true", **base).json()["items"]
    named = query(client, alias="none", **base).json()["items"]
    assert [(r["alias"], r["requests"]) for r in missing] == [("", 1)]
    assert [(r["alias"], r["requests"]) for r in named] == [("none", 1)]
    await committed.delete(await committed.get(CatalogName, alias.id))
    await committed.commit()
    deleted = query(client, alias="none", **base).json()["items"]
    assert (deleted[0]["alias"], deleted[0]["alias_id"]) == ("none", None)
    pool = await make_router(committed, "none", members=[model], created_at=CREATED)
    assert (
        await ingest(
            committed, "gateway", [wire(key, model=model.name, router="none")], zone=api_scope
        )
        == 1
    )
    await committed.commit()
    base = {"team_id": str(key.team_id), "group_by": "router"}
    named = query(client, router="none", **base).json()["items"]
    missing = query(client, router_missing="true", **base).json()["items"]
    assert [(r["router"], r["router_id"], r["requests"]) for r in named] == [
        ("none", str(pool.id), 1)
    ]
    assert [(r["router"], r["requests"]) for r in missing] == [("", 2)]


async def test_routing_detail_is_installation_admin_only(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    member, member_token = await user_with_token(committed)
    team_admin, team_admin_token = await user_with_token(committed)
    org_admin, org_admin_token = await user_with_token(committed)
    _, installation_token = await user_with_token(committed, installation_admin=True)
    await make_member(committed, team, member)
    await make_member(committed, team, team_admin, role=TeamRole.ADMIN)
    await make_org_admin(committed, org, org_admin)
    await committed.commit()

    routing_groups = ("model", "upstream_model", "alias", "router", "fallback")
    routing_filters = (
        {"model": "some-model"},
        {"upstream_model": "served-name"},
        {"alias": "some-alias"},
        {"router": "some-router"},
        {"model_id": str(uuid.uuid4())},
        {"alias_id": str(uuid.uuid4())},
        {"router_id": str(uuid.uuid4())},
        {"alias_missing": "true"},
        {"router_missing": "false"},
    )
    for headers in (auth(member_token), auth(team_admin_token), auth(org_admin_token)):
        for group in routing_groups:
            response = query(client, headers, group_by=group, team_id=str(team.id))
            assert response.status_code == 403, (group, response.text)
            assert response.json()["error"]["code"] == "routing_detail_forbidden"
        for filters in routing_filters:
            response = query(client, headers, group_by="requested", team_id=str(team.id), **filters)
            assert response.status_code == 403, (filters, response.text)
            assert response.json()["error"]["code"] == "routing_detail_forbidden"

    # An installation admin keeps the full routing view.
    for group in routing_groups:
        response = query(client, auth(installation_token), group_by=group, team_id=str(team.id))
        assert response.status_code == 200, (group, response.text)
    for filters in routing_filters:
        response = query(
            client, auth(installation_token), group_by="requested", team_id=str(team.id), **filters
        )
        assert response.status_code == 200, (filters, response.text)


async def test_mcp_and_a2a_events_are_filtered_and_grouped_by_kind(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    committed.add(usage_event(key, ts=TS, requested="chat"))
    committed.add(
        usage_event(key, ts=TS, kind=TrafficKind.MCP, target="s1", method="tools/call", tool="echo")
    )
    committed.add(usage_event(key, ts=TS, kind=TrafficKind.MCP, target="s1", method="tools/list"))
    committed.add(
        usage_event(key, ts=TS, kind=TrafficKind.A2A, target="agent", method="message/send")
    )
    # The pre-aggregated total only ever counted the LLM request.
    committed.add(
        usage_total(
            ScopeType.TEAM,
            team.id,
            window_start=START,
            requests=1,
            prompt_tokens=10,
            completion_tokens=5,
            cost_nano_usd=1000,
        )
    )
    await committed.commit()

    by_kind = query(client, group_by="kind", team_id=str(team.id))
    assert by_kind.status_code == 200, by_kind.text
    assert {row["kind"]: row["requests"] for row in by_kind.json()["items"]} == {
        "llm": 1,
        "mcp": 2,
        "a2a": 1,
    }
    # Nothing an MCP row carries may leak into an LLM row's totals.
    llm = next(row for row in by_kind.json()["items"] if row["kind"] == "llm")
    assert (llm["prompt_tokens"], llm["cost_nano_usd"]) == (10, 1000)

    mcp = query(client, group_by="method,tool", team_id=str(team.id), kind="mcp")
    assert mcp.status_code == 200, mcp.text
    assert {(row["method"], row["tool"], row["requests"]) for row in mcp.json()["items"]} == {
        ("tools/call", "echo", 1),
        ("tools/list", "", 1),
    }
    by_target = query(client, group_by="target", team_id=str(team.id), method="message/send")
    assert [(row["target"], row["requests"]) for row in by_target.json()["items"]] == [("agent", 1)]
    only_echo = query(client, group_by="kind,tool", team_id=str(team.id), tool="echo")
    assert [(row["kind"], row["requests"]) for row in only_echo.json()["items"]] == [("mcp", 1)]
    # The team totals fast path holds LLM requests only: four events, one aggregated request.
    totals = query(client, group_by="team", team_id=str(team.id))
    assert [(row["kind"], row["requests"]) for row in totals.json()["items"]] == [("llm", 1)]
    # Asking for LLM explicitly is the same question, so it is served from the same place.
    llm_totals = query(client, group_by="team", team_id=str(team.id), kind="llm")
    assert [row["requests"] for row in llm_totals.json()["items"]] == [1]
    # Any other kind cannot be served from it, so the same question reads raw events.
    mcp_requests = query(client, group_by="team", team_id=str(team.id), kind="mcp")
    assert [row["requests"] for row in mcp_requests.json()["items"]] == [2]
    assert query(client, team_id=str(team.id), kind="shim").status_code == 422


async def test_errors_and_duration_are_aggregated_per_group(client, committed) -> None:
    """The MCP and A2A tabs read errors and duration instead of tokens: both are per group."""
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    ok = usage_event(
        key, ts=TS, kind=TrafficKind.MCP, target="s1", method="tools/call", tool="echo"
    )
    ok.duration_ms = 5
    upstream_failed = usage_event(
        key, ts=TS, kind=TrafficKind.MCP, target="s1", method="tools/call", tool="echo"
    )
    upstream_failed.duration_ms = 25
    upstream_failed.status = 502
    upstream_failed.outcome = RequestOutcome.UPSTREAM_ERROR
    # A2A errors are JSON-RPC errors in a 200: the outcome, not the status, says it failed.
    protocol_failed = usage_event(
        key, ts=TS, kind=TrafficKind.A2A, target="agent", method="SendMessage"
    )
    protocol_failed.duration_ms = 70
    protocol_failed.outcome = RequestOutcome.UPSTREAM_ERROR
    # A client that hung up is neither a success nor an error.
    cancelled = usage_event(key, ts=TS, kind=TrafficKind.MCP, target="s1", method="tools/list")
    cancelled.duration_ms = 100
    cancelled.status = 499
    cancelled.outcome = RequestOutcome.CLIENT_CANCELLED
    committed.add_all([ok, upstream_failed, protocol_failed, cancelled])
    await committed.commit()

    def amounts(response):
        assert response.status_code == 200, response.text
        return {
            (row["target"], row["method"], row["tool"]): (
                row["requests"],
                row["error_count"],
                row["total_duration_ms"],
            )
            for row in response.json()["items"]
        }

    mcp = amounts(query(client, group_by="target,method,tool", team_id=str(team.id), kind="mcp"))
    assert mcp == {
        ("s1", "tools/call", "echo"): (2, 1, 30),
        ("s1", "tools/list", ""): (1, 0, 100),
    }
    a2a = amounts(query(client, group_by="target,method", team_id=str(team.id), kind="a2a"))
    assert a2a == {("agent", "SendMessage", None): (1, 1, 70)}
    # An LLM row still reports the LLM amounts; the two counters start at zero.
    committed.add(usage_event(key, ts=TS, requested="chat"))
    await committed.commit()
    llm = query(client, group_by="kind", team_id=str(team.id), kind="llm").json()["items"]
    assert [(row["kind"], row["requests"], row["error_count"]) for row in llm] == [("llm", 1, 0)]


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "org"},
        {"group_by": "project"},
        {"group_by": "nope"},
        {"group_by": "owner", "cursor": "invalid"},
        {"from": "2026-03-01T00:00:00"},
        {"from": END.isoformat(), "to": START.isoformat()},
    ],
)
def test_usage_query_validation(client, params) -> None:
    assert query(client, **params).status_code == 422


async def test_called_dimension_and_zone_grouping(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    model_name = f"m-{uuid.uuid4().hex}"
    pool_name = f"pool-{uuid.uuid4().hex}"
    alias_name = f"nick-{uuid.uuid4().hex}"
    unknown = f"missing-{uuid.uuid4().hex}"
    model = await make_model(committed, model_name)
    pool = await make_router(committed, pool_name, members=[model])
    alias = await make_alias(committed, alias_name, pool)
    committed.add_all(
        [
            called_event(key, requested=model_name, model=model_name, model_id=model.id),
            called_event(
                key,
                requested=alias_name,
                alias=alias_name,
                router=pool_name,
                model=model_name,
                alias_id=alias.id,
                router_id=pool.id,
                model_id=model.id,
            ),
            called_event(
                key,
                requested=pool_name,
                router=pool_name,
                model=model_name,
                router_id=pool.id,
                model_id=model.id,
            ),
            called_event(key, requested=unknown, model=""),
        ]
    )
    await committed.commit()

    response = query(client, group_by="zone,called", team_id=str(team.id))
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    # One row per called item: the server does not add a second zone column.
    assert len(rows) == 4
    assert {row["zone"] for row in rows} == {key.zone}
    by_called = {row["called"]: row for row in rows}
    assert (
        by_called[model_name]["called_kind"],
        by_called[model_name]["called_names"],
    ) == ("model", [model_name])
    assert by_called[alias_name]["called_kind"] == "alias"
    assert by_called[pool_name]["called_kind"] == "router"
    assert (
        by_called[unknown]["called_kind"],
        by_called[unknown]["called_names"],
    ) == ("unknown", [unknown])

    # `called` is not routing detail: a plain member groups by it and sees the team totals.
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await committed.commit()
    member_rows = query(client, auth(member_token), group_by="called", team_id=str(team.id))
    assert member_rows.status_code == 200, member_rows.text
    assert {row["called"] for row in member_rows.json()["items"]} == {
        model_name,
        alias_name,
        pool_name,
        unknown,
    }


async def test_called_merges_a_rename_but_requested_splits(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    model_name = f"m-{uuid.uuid4().hex}"
    old_name = f"old-{uuid.uuid4().hex}"
    new_name = f"new-{uuid.uuid4().hex}"
    model = await make_model(committed, model_name)
    alias = await make_alias(committed, old_name, model)
    committed.add(
        called_event(
            key,
            requested=old_name,
            alias=old_name,
            model=model_name,
            alias_id=alias.id,
            model_id=model.id,
        )
    )
    await rename_at(committed, alias, new_name)
    committed.add(
        called_event(
            key,
            requested=new_name,
            alias=new_name,
            model=model_name,
            alias_id=alias.id,
            model_id=model.id,
        )
    )
    await committed.commit()

    called_rows = query(client, group_by="called", team_id=str(team.id)).json()["items"]
    assert len(called_rows) == 1
    row = called_rows[0]
    assert (row["called"], row["called_kind"], row["requests"]) == (new_name, "alias", 2)
    assert row["called_names"] == sorted([old_name, new_name])

    requested_rows = query(client, group_by="requested", team_id=str(team.id)).json()["items"]
    assert {item["requested"]: item["requests"] for item in requested_rows} == {
        old_name: 1,
        new_name: 1,
    }
    # The current name selects both spellings; the old spelling no longer names a live item.
    assert (
        query(client, group_by="called", called=new_name, team_id=str(team.id)).json()["items"][0][
            "requests"
        ]
        == 2
    )
    assert (
        query(client, group_by="called", called=old_name, team_id=str(team.id)).json()["items"]
        == []
    )


async def test_status_class_and_outcome_buckets(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    for status, outcome in (
        (200, RequestOutcome.COMPLETED),
        (201, RequestOutcome.COMPLETED),
        (404, RequestOutcome.REJECTED),
        (429, RequestOutcome.REJECTED),
        (502, RequestOutcome.UPSTREAM_ERROR),
        (302, RequestOutcome.COMPLETED),
    ):
        event = usage_event(key, ts=TS, requested=f"s{status}")
        event.status = status
        event.outcome = outcome
        committed.add(event)
    await committed.commit()

    by_status = query(client, group_by="status_class", team_id=str(team.id)).json()["items"]
    assert {row["status_class"]: row["requests"] for row in by_status} == {
        "2xx": 2,
        "4xx": 2,
        "5xx": 1,
        "other": 1,
    }
    by_outcome = query(client, group_by="outcome", team_id=str(team.id)).json()["items"]
    assert {row["outcome"]: row["requests"] for row in by_outcome} == {
        "completed": 3,
        "rejected": 2,
        "upstream_error": 1,
    }


async def test_percentiles_fallback_and_retry_amounts(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    for index, duration in enumerate((10, 20, 30, 40, 100)):
        event = usage_event(key, ts=TS, requested="p")
        event.duration_ms = duration
        event.fallback = index == 4
        event.attempts = 3 if index == 4 else 1
        committed.add(event)
    await committed.commit()

    row = query(client, group_by="requested", team_id=str(team.id)).json()["items"][0]
    assert row["requests"] == 5
    assert row["total_duration_ms"] == 200
    assert row["fallback_count"] == 1
    assert row["retried_count"] == 1
    assert row["p50_duration_ms"] == pytest.approx(30)
    assert row["p95_duration_ms"] == pytest.approx(88)
    assert row["p99_duration_ms"] == pytest.approx(97.6)


async def test_new_dimension_cursors(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    for status, outcome, fallback in (
        (200, RequestOutcome.COMPLETED, False),
        (404, RequestOutcome.REJECTED, True),
        (502, RequestOutcome.UPSTREAM_ERROR, True),
    ):
        event = usage_event(key, ts=TS, requested=f"s{status}")
        event.status = status
        event.outcome = outcome
        event.fallback = fallback
        committed.add(event)
    await committed.commit()
    for groups in ("status_class", "outcome", "fallback"):
        whole = query(client, group_by=groups, team_id=str(team.id)).json()["items"]
        first = query(client, group_by=groups, team_id=str(team.id), limit=1).json()
        assert first["next_cursor"] is not None, groups
        rest = query(
            client,
            group_by=groups,
            team_id=str(team.id),
            limit=50,
            cursor=first["next_cursor"],
        ).json()
        assert first["items"] + rest["items"] == whole, groups


async def test_compare_previous_returns_the_prior_range_totals(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    march = usage_event(
        key, ts=TS, requested="march", prompt_tokens=10, completion_tokens=5, cost_nano_usd=1000
    )
    march.duration_ms = 20
    february = usage_event(
        key,
        ts=datetime(2026, 2, 15, tzinfo=UTC),
        requested="february",
        prompt_tokens=7,
        completion_tokens=3,
        cost_nano_usd=700,
    )
    february.duration_ms = 40
    committed.add_all([march, february])
    await committed.commit()

    body = query(client, group_by="requested", compare="previous", team_id=str(team.id)).json()
    assert [row["requested"] for row in body["items"]] == ["march"]
    previous = body["previous"]
    assert previous["requests"] == 1
    assert previous["prompt_tokens"] == 7
    assert previous["completion_tokens"] == 3
    assert previous["total_tokens"] == 10
    assert previous["cost_nano_usd"] == 700
    assert previous["fallback_count"] == 0
    assert previous["retried_count"] == 0
    assert previous["p50_duration_ms"] == pytest.approx(40)
    assert query(client, group_by="requested", team_id=str(team.id)).json()["previous"] is None


async def test_day_bucket_follows_tz(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    committed.add(usage_event(key, ts=datetime(2026, 11, 22, 17, 30, tzinfo=UTC), requested="late"))
    await committed.commit()
    start = datetime(2026, 11, 22, tzinfo=UTC)
    end = datetime(2026, 11, 24, tzinfo=UTC)

    utc = window(client, start, end, group_by="day", tz="UTC", team_id=str(team.id))
    assert utc.status_code == 200, utc.text
    assert [row["date"] for row in utc.json()["items"]] == ["2026-11-22"]

    # 17:30Z is 01:30 the next day in Asia/Taipei.
    taipei = window(client, start, end, group_by="day", tz="Asia/Taipei", team_id=str(team.id))
    assert taipei.status_code == 200, taipei.text
    assert [row["date"] for row in taipei.json()["items"]] == ["2026-11-23"]

    bad = window(client, start, end, group_by="day", tz="Not/AZone", team_id=str(team.id))
    assert bad.status_code == 422, bad.text
    assert "tz" in bad.text


async def test_hour_bucket_is_aware_and_merges_the_same_hour(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    committed.add_all(
        [
            usage_event(key, ts=datetime(2026, 3, 10, 10, 5, tzinfo=UTC), requested="a"),
            usage_event(key, ts=datetime(2026, 3, 10, 10, 55, tzinfo=UTC), requested="b"),
            usage_event(key, ts=datetime(2026, 3, 10, 11, 5, tzinfo=UTC), requested="c"),
        ]
    )
    await committed.commit()

    rows = query(client, group_by="hour", team_id=str(team.id)).json()["items"]
    assert [row["requests"] for row in rows] == [2, 1]
    hours = [datetime.fromisoformat(row["hour"]) for row in rows]
    assert all(hour.tzinfo is not None and hour.minute == 0 and hour.second == 0 for hour in hours)
    assert hours == [datetime(2026, 3, 10, 10, tzinfo=UTC), datetime(2026, 3, 10, 11, tzinfo=UTC)]

    # A half-hour zone starts its local hour on a :30 UTC instant.
    kolkata = query(client, group_by="hour", tz="Asia/Kolkata", team_id=str(team.id)).json()[
        "items"
    ]
    assert [row["requests"] for row in kolkata] == [1, 2]
    assert [datetime.fromisoformat(row["hour"]) for row in kolkata] == [
        datetime(2026, 3, 10, 9, 30, tzinfo=UTC),
        datetime(2026, 3, 10, 10, 30, tzinfo=UTC),
    ]


async def test_hour_bucket_keeps_both_fall_back_hours(client, committed) -> None:
    # 2026-11-01 01:30 happens twice in New York (EDT, then EST): two hours, two buckets.
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    committed.add_all(
        [
            usage_event(key, ts=datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
            usage_event(key, ts=datetime(2026, 11, 1, 6, 30, tzinfo=UTC)),
        ]
    )
    await committed.commit()

    rows = window(
        client,
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        group_by="hour",
        tz="America/New_York",
        team_id=str(team.id),
    ).json()["items"]
    assert [(datetime.fromisoformat(row["hour"]), row["requests"]) for row in rows] == [
        (datetime(2026, 11, 1, 5, tzinfo=UTC), 1),
        (datetime(2026, 11, 1, 6, tzinfo=UTC), 1),
    ]


async def test_hour_range_limit_and_cursor(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    committed.add_all(
        [
            usage_event(key, ts=datetime(2026, 3, 10, 10, tzinfo=UTC)),
            usage_event(key, ts=datetime(2026, 3, 10, 12, tzinfo=UTC)),
        ]
    )
    await committed.commit()

    whole = query(client, group_by="hour", team_id=str(team.id)).json()["items"]
    first = query(client, group_by="hour", team_id=str(team.id), limit=1).json()
    assert first["next_cursor"] is not None
    rest = query(client, group_by="hour", team_id=str(team.id), cursor=first["next_cursor"]).json()[
        "items"
    ]
    assert first["items"] + rest == whole

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 2, tzinfo=UTC)
    too_long = window(client, start, end, group_by="hour", team_id=str(team.id))
    assert too_long.status_code == 422, too_long.text
    assert "31 days" in too_long.text


async def test_merged_groups_with_unequal_upstream_lists_do_not_fail(client, committed) -> None:
    """A deleted id and a name-only row share a label; their upstream lists differ in length."""
    team = await make_team(committed)
    key = await make_key(committed, team, await make_service_account(committed, team))
    name = f"ghost-{uuid.uuid4().hex}"
    rows = [
        (uuid.uuid4(), "up-a"),  # an id no longer in the catalog
        (None, "up-a"),
        (None, "up-b"),
    ]
    for model_id, upstream in rows:
        event = called_event(key, requested=name, model=name, model_id=model_id)
        event.upstream_model = upstream
        committed.add(event)
    await committed.commit()

    for group_by in ("model", "day,model"):
        response = query(client, group_by=group_by, team_id=str(team.id))
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert sum(item["requests"] for item in items) == 3
        assert sorted({u for item in items for u in item["upstream_models"]}) == ["up-a", "up-b"]

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
    UsageTotal,
)
from lumewarp_control_plane.usage import ingest
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, usage_total, user_with_token
from tests.factories import (
    make_alias,
    make_key,
    make_member,
    make_model,
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


async def test_grouping_filters_and_cursor_pagination(client, committed) -> None:
    team = await make_team(committed)
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


async def test_monthly_team_totals_fast_path(client, committed) -> None:
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
    total = await committed.get(UsageTotal, (ScopeType.TEAM, team.id, START))
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


async def test_a_plain_member_sees_only_their_own_rows(client, committed) -> None:
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

    # The owner predicate is server-side: the team's other members and the service
    # account are not visible, and no filter or grouping widens it.
    def requests_for(**params):
        response = query(client, headers, **params)
        assert response.status_code == 200, response.text
        return sum(row["requests"] for row in response.json()["items"])

    assert requests_for() == 2
    assert requests_for(group_by="owner") == 2
    assert requests_for(group_by="team") == 2
    assert requests_for(group_by="team", team_id=str(team.id)) == 2
    assert requests_for(group_by="owner", team_id=str(team.id)) == 2
    assert requests_for(group_by="key") == 2
    people = query(client, headers, group_by="owner").json()["items"]
    assert {(row["owner_kind"], row["owner_id"]) for row in people} == {("user", str(member.id))}
    # Filters for someone else return nothing instead of their rows.
    assert requests_for(group_by="owner", team_id=str(team.id), user_id=str(colleague.id)) == 0
    assert (
        requests_for(group_by="owner", team_id=str(team.id), service_account_id=str(account.id))
        == 0
    )
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


async def test_catalog_filters_grouping_rename_and_pagination(client, committed):
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
    assert await ingest(committed, "gateway", [first, second]) == 2
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


async def test_literal_none_distinct_from_missing_and_deleted_names(client, committed):
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
    assert await ingest(committed, "gateway", [wire(key, model=model.name, router="none")]) == 1
    await committed.commit()
    base = {"team_id": str(key.team_id), "group_by": "router"}
    named = query(client, router="none", **base).json()["items"]
    missing = query(client, router_missing="true", **base).json()["items"]
    assert [(r["router"], r["router_id"], r["requests"]) for r in named] == [
        ("none", str(pool.id), 1)
    ]
    assert [(r["router"], r["requests"]) for r in missing] == [("", 2)]


@pytest.mark.parametrize("dimension", ["model", "upstream_model", "alias", "router"])
async def test_new_dimensions_keep_permission_scope(client, committed, dimension):
    team = await make_team(committed)
    owner, token = await user_with_token(committed)
    await make_member(committed, team, owner)
    own = await make_key(committed, team, owner)
    foreign = await key_for(committed)
    unique = uuid.uuid4().hex
    for key, name in ((own, f"own-{unique}"), (foreign, f"foreign-{unique}")):
        event = wire(key, **{dimension: name})
        assert await ingest(committed, "gateway", [event]) == 1
    await committed.commit()
    response = query(client, auth(token), group_by=dimension)
    assert response.status_code == 200, response.text
    assert [(r[dimension], r["requests"]) for r in response.json()["items"]] == [
        (f"own-{unique}", 1)
    ]
    hidden = query(client, auth(token), group_by=dimension, **{dimension: f"foreign-{unique}"})
    assert hidden.json()["items"] == []
    forbidden = query(client, auth(token), group_by=dimension, team_id=str(foreign.team_id))
    assert forbidden.status_code == 404


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

"""The request log: permission scope, filters, cursor pagination and Playground attribution."""

import uuid
from datetime import UTC, datetime, timedelta

from lumewarp_control_plane.models import (
    OrgAdmin,
    PlaygroundRequest,
    RequestOutcome,
    TeamRole,
    TrafficKind,
)
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_org,
    make_service_account,
    make_team,
    make_user,
)

NOW = datetime.now(UTC)
START = NOW - timedelta(hours=2)
END = NOW + timedelta(hours=2)
TS = NOW - timedelta(hours=1)


def query(client, headers=ADMIN_AUTH, **params):
    return client.get(
        "/api/v1/requests",
        params={"from": START.isoformat(), "to": END.isoformat(), **params},
        headers=headers,
    )


def items(client, headers=ADMIN_AUTH, **params) -> list[dict]:
    response = query(client, headers, **params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def names(client, headers=ADMIN_AUTH, **params) -> set[str]:
    return {row["requested"] for row in items(client, headers, **params)}


def request_ids(client, headers=ADMIN_AUTH, **params) -> set[str]:
    return {row["request_id"] for row in items(client, headers, **params)}


async def test_scope_by_role_and_out_of_scope_filters(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    other_team = await make_team(committed)
    member, member_token = await user_with_token(committed)
    colleague, colleague_token = await user_with_token(committed)
    team_admin, team_admin_token = await user_with_token(committed)
    org_admin, org_admin_token = await user_with_token(committed)
    installation_admin, installation_admin_token = await user_with_token(
        committed, installation_admin=True
    )
    outsider, outsider_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_member(committed, team, colleague)
    await make_member(committed, team, team_admin, role=TeamRole.ADMIN)
    committed.add(OrgAdmin(org_id=org.id, user_id=org_admin.id))
    member_key = await make_key(committed, team, member)
    colleague_key = await make_key(committed, team, colleague)
    account = await make_service_account(committed, team)
    service_key = await make_key(committed, team, account)
    foreign_key = await make_key(committed, other_team, await make_user(committed))
    for key, requested in (
        (member_key, "member"),
        (colleague_key, "colleague"),
        (service_key, "service"),
        (foreign_key, "foreign"),
    ):
        committed.add(usage_event(key, ts=TS, requested=requested))
    await committed.commit()

    assert names(client, auth(member_token)) == {"member"}
    assert names(client, auth(colleague_token)) == {"colleague"}
    assert names(client, auth(team_admin_token)) == {"member", "colleague", "service"}
    # The org admin administers the whole team without being a direct member.
    assert names(client, auth(org_admin_token)) == {"member", "colleague", "service"}
    # The installation admin's scope is every team; constrain it here so the shared test
    # database's other rows do not matter.
    assert names(client, ADMIN_AUTH, team_id=str(team.id)) == {
        "member",
        "colleague",
        "service",
    }
    assert names(client, auth(installation_admin_token), team_id=str(team.id)) == {
        "member",
        "colleague",
        "service",
    }
    assert names(client, auth(outsider_token)) == set()

    # Same parent checks and answers as `/usage`.
    for params, status in (
        ({"team_id": str(other_team.id)}, 404),
        ({"key_id": str(colleague_key.id)}, 403),
        ({"key_id": str(service_key.id)}, 403),
        ({"key_id": str(uuid.uuid4())}, 404),
        ({"team_id": str(other_team.id), "key_id": str(member_key.id)}, 404),
        ({"team_id": str(uuid.uuid4())}, 404),
        ({"user_id": str(member.id)}, 422),
        (
            {
                "team_id": str(team.id),
                "user_id": str(member.id),
                "service_account_id": str(account.id),
            },
            422,
        ),
    ):
        response = query(client, auth(member_token), **params)
        assert response.status_code == status, (params, response.text)

    # The owner filters narrow within a readable team, and cannot widen a member's scope.
    assert names(client, auth(member_token), team_id=str(team.id)) == {"member"}
    assert names(client, auth(member_token), team_id=str(team.id), user_id=str(colleague.id)) == (
        set()
    )
    assert (
        names(client, auth(member_token), team_id=str(team.id), service_account_id=str(account.id))
        == set()
    )
    assert names(client, auth(member_token), team_id=str(team.id), key_id=str(member_key.id)) == {
        "member"
    }
    assert names(
        client, auth(team_admin_token), team_id=str(team.id), user_id=str(colleague.id)
    ) == {"colleague"}
    assert names(
        client,
        auth(team_admin_token),
        team_id=str(team.id),
        service_account_id=str(account.id),
    ) == {"service"}
    # Another org is not visible to this org's admin.
    assert query(client, auth(org_admin_token), team_id=str(other_team.id)).status_code == 404


async def test_each_filter_and_the_default_kind(client, committed) -> None:
    team = await make_team(committed)
    owner, _ = await user_with_token(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner)
    account = await make_service_account(committed, team)
    service_key = await make_key(committed, team, account)
    model_id, alias_id, router_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    alpha = usage_event(key, ts=TS, requested="alpha")
    alpha.model = "served-a"
    alpha.model_id = model_id
    alpha.alias = "nick-a"
    alpha.alias_id = alias_id
    alpha.router = "pool-a"
    alpha.router_id = router_id
    alpha.fallback = True
    alpha.upstream = "vllm-0"
    alpha.upstream_model = "meta/llama"
    alpha.attempts = 2
    beta = usage_event(key, ts=TS, requested="beta")
    beta.model = "served-b"
    beta.status = 502
    beta.outcome = RequestOutcome.UPSTREAM_ERROR
    beta.stream = True
    gamma = usage_event(service_key, ts=TS, requested="gamma")
    mcp = usage_event(key, ts=TS, kind=TrafficKind.MCP, target="srv", method="tools/call")
    a2a = usage_event(key, ts=TS, kind=TrafficKind.A2A, target="agent", method="message/send")
    committed.add_all([alpha, beta, gamma, mcp, a2a])
    await committed.commit()
    base = {"team_id": str(team.id)}

    # LLM is the default kind: MCP and A2A rows need an explicit kind.
    assert names(client, **base) == {"alpha", "beta", "gamma"}
    assert request_ids(client, **base, kind="mcp") == {str(mcp.request_id)}
    assert request_ids(client, **base, kind="a2a") == {str(a2a.request_id)}
    assert request_ids(client, **base, kind="llm") == {
        str(alpha.request_id),
        str(beta.request_id),
        str(gamma.request_id),
    }

    for params, expected in (
        ({"requested": "alpha"}, {str(alpha.request_id)}),
        ({"model": "served-a"}, {str(alpha.request_id)}),
        ({"alias": "nick-a"}, {str(alpha.request_id)}),
        ({"router": "pool-a"}, {str(alpha.request_id)}),
        ({"status": 502}, {str(beta.request_id)}),
        ({"outcome": "upstream_error"}, {str(beta.request_id)}),
        ({"request_id": str(alpha.request_id)}, {str(alpha.request_id)}),
        ({"key_id": str(service_key.id)}, {str(gamma.request_id)}),
        ({"user_id": str(owner.id)}, {str(alpha.request_id), str(beta.request_id)}),
        (
            {"service_account_id": str(account.id)},
            {str(gamma.request_id)},
        ),
    ):
        assert request_ids(client, **base, **params) == expected, params

    # The row carries the whole name-resolution chain and the typed amounts.
    row = items(client, **base, request_id=str(alpha.request_id))[0]
    assert (
        row["requested"],
        row["alias"],
        row["alias_id"],
        row["router"],
        row["router_id"],
        row["fallback"],
        row["model"],
        row["model_id"],
        row["upstream"],
        row["upstream_model"],
        row["attempts"],
    ) == (
        "alpha",
        "nick-a",
        str(alias_id),
        "pool-a",
        str(router_id),
        True,
        "served-a",
        str(model_id),
        "vllm-0",
        "meta/llama",
        2,
    )
    assert (row["prompt_tokens"], row["completion_tokens"], row["cost_nano_usd"]) == (10, 5, 1000)
    assert (row["status"], row["outcome"], row["stream"]) == (200, "completed", False)
    assert row["duration_ms"] == 10
    assert (row["kind"], row["team_id"], row["owner_kind"], row["owner_id"]) == (
        "llm",
        str(team.id),
        "user",
        str(owner.id),
    )

    # The range is half-open. `from`/`to` are query aliases, so they pass as a dict.
    assert request_ids(client, **base, **{"from": (TS + timedelta(minutes=1)).isoformat()}) == (
        set()
    )
    assert request_ids(client, **base, **{"to": TS.isoformat()}) == set()
    assert query(
        client, **base, **{"from": END.isoformat(), "to": START.isoformat()}
    ).status_code == (422)


async def test_default_range_is_the_last_24_hours(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_user(committed))
    now = datetime.now(UTC)
    old = usage_event(key, ts=now - timedelta(days=2), requested="old")
    fresh = usage_event(key, ts=now - timedelta(minutes=5), requested="fresh")
    committed.add_all([old, fresh])
    await committed.commit()
    response = client.get("/api/v1/requests", params={"team_id": str(team.id)}, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    assert {row["requested"] for row in response.json()["items"]} == {"fresh"}
    # An explicit range reads the older row too.
    earlier = (now - timedelta(days=3)).isoformat()
    assert names(client, **{"from": earlier}, team_id=str(team.id)) == {"old", "fresh"}


async def test_cursor_is_stable_with_equal_timestamps(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_user(committed))
    committed.add_all([usage_event(key, ts=TS, requested=f"r{index}") for index in range(5)])
    await committed.commit()
    base = {"team_id": str(team.id)}
    whole = items(client, **base, limit=500)
    assert len(whole) == 5
    seen: list[str] = []
    cursor = None
    for _ in range(100):
        page = query(client, **base, limit=2, **({"cursor": cursor} if cursor else {}))
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["items"]) <= 2
        seen.extend(row["request_id"] for row in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("pagination did not terminate")
    assert seen == [row["request_id"] for row in whole]
    assert len(seen) == len(set(seen))
    assert query(client, **base, cursor="not-a-cursor").status_code == 422


async def test_playground_user_is_joined_by_request_id(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    plain = usage_event(key, ts=TS, requested="plain")
    played = usage_event(key, ts=TS, requested="played")
    committed.add_all([plain, played])
    committed.add(PlaygroundRequest(request_id=played.request_id, user_id=user.id, key_id=key.id))
    await committed.commit()

    rows = items(client, auth(token), team_id=str(team.id))
    by_name = {row["requested"]: row for row in rows}
    assert by_name["played"]["playground_user_id"] == str(user.id)
    assert by_name["plain"]["playground_user_id"] is None
    # The installation admin sees the same join.
    admin_row = items(client, ADMIN_AUTH, request_id=str(played.request_id))[0]
    assert admin_row["playground_user_id"] == str(user.id)

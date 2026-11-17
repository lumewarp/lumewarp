"""`GET /usage/leaderboard`: scope, ranking and anonymisation for every role and board."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lumewarp_control_plane.models import TeamRole, TrafficKind
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_org,
    make_org_admin,
    make_service_account,
    make_team,
    make_user,
)

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
TS = datetime(2026, 3, 15, 12, tzinfo=UTC)


def leaderboard(client, headers=ADMIN_AUTH, **params):
    return client.get(
        "/api/v1/usage/leaderboard",
        params={"from": START.isoformat(), "to": END.isoformat(), **params},
        headers=headers,
    )


def board(client, headers=ADMIN_AUTH, **params) -> list[dict]:
    response = leaderboard(client, headers, **params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def all_tokens(rows: list[dict]) -> list[int]:
    return [row["total_tokens"] for row in rows]


async def make_world(session):
    """One readable team with four keys, plus a foreign team that must never show up."""
    org = await make_org(session)
    team = await make_team(session, org=org)
    other_team = await make_team(session)
    member, member_token = await user_with_token(session)
    colleague, colleague_token = await user_with_token(session)
    team_admin, team_admin_token = await user_with_token(session)
    org_admin, org_admin_token = await user_with_token(session)
    _, installation_token = await user_with_token(session, installation_admin=True)
    await make_member(session, team, member)
    await make_member(session, team, colleague)
    await make_member(session, team, team_admin, role=TeamRole.ADMIN)
    await make_org_admin(session, org, org_admin)
    account = await make_service_account(session, team, name=f"worker-{uuid.uuid4().hex[:8]}")
    member_key = await make_key(session, team, member, name="member-key")
    colleague_key = await make_key(session, team, colleague, name="colleague-key")
    sa_key = await make_key(session, team, account, name="service-key")
    admin_key = await make_key(session, team, team_admin, name="admin-key")
    foreign_key = await make_key(session, other_team, await make_user(session))
    events = (
        # total_tokens, then cost, decides the ranking.
        (member_key, "shared", 100, 100, 2000),
        (member_key, "m-model", 10, 5, 1000),
        (colleague_key, "c-model", 30, 20, 500),
        (sa_key, "s-model", 20, 10, 300),
        (admin_key, "t-model", 6, 4, 200),
        (foreign_key, "foreign-model", 5000, 5000, 99999),
    )
    for key, requested, prompt, completion, cost in events:
        session.add(
            usage_event(
                key,
                ts=TS,
                requested=requested,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_nano_usd=cost,
            )
        )
    # Not an LLM request: the default `kind=llm` board must not count it.
    session.add(
        usage_event(member_key, ts=TS, kind=TrafficKind.MCP, target="srv", method="tools/call")
    )
    await session.commit()
    return SimpleNamespace(
        org=org,
        team=team,
        other_team=other_team,
        member=member,
        member_token=member_token,
        colleague=colleague,
        colleague_token=colleague_token,
        team_admin=team_admin,
        team_admin_token=team_admin_token,
        org_admin=org_admin,
        org_admin_token=org_admin_token,
        installation_token=installation_token,
        account=account,
        member_key=member_key,
        colleague_key=colleague_key,
        sa_key=sa_key,
        admin_key=admin_key,
        foreign_key=foreign_key,
    )


async def test_requested_board_ranks_called_names_for_every_role(client, committed) -> None:
    world = await make_world(committed)
    expected = ["shared", "c-model", "s-model", "m-model", "t-model"]
    for headers in (
        auth(world.member_token),
        auth(world.team_admin_token),
        auth(world.org_admin_token),
        auth(world.installation_token),
    ):
        rows = board(client, headers, by="requested", team_id=str(world.team.id))
        assert [row["requested"] for row in rows] == expected
        assert [row["rank"] for row in rows] == [1, 2, 3, 4, 5]
        assert all_tokens(rows) == [200, 50, 30, 15, 10]
        assert rows[0]["requests"] == 1
        assert all(
            row["team_id"] is None
            and row["key"] is None
            and row["owner_kind"] is None
            and row["owner_id"] is None
            and row["is_me"] is False
            for row in rows
        )
        assert "foreign-model" not in {row["requested"] for row in rows}

    # The member's readable teams are their own team only: no other team leaks in.
    assert "foreign-model" not in {
        row["requested"] for row in board(client, auth(world.member_token), by="requested")
    }
    assert (
        leaderboard(
            client, auth(world.member_token), by="requested", team_id=str(world.other_team.id)
        ).status_code
        == 404
    )


async def test_requested_board_ties_on_tokens_break_by_spend(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_user(committed))
    committed.add(usage_event(key, ts=TS, requested="cheap", prompt_tokens=5, cost_nano_usd=100))
    committed.add(usage_event(key, ts=TS, requested="pricey", prompt_tokens=5, cost_nano_usd=900))
    await committed.commit()
    rows = board(client, by="requested", team_id=str(team.id))
    assert [row["requested"] for row in rows] == ["pricey", "cheap"]
    assert all(row["unknown_cost_count"] == 0 for row in rows)


async def test_key_board_names_only_readable_keys(client, committed) -> None:
    world = await make_world(committed)
    ranked_ids = [world.member_key.id, world.colleague_key.id, world.sa_key.id, world.admin_key.id]

    member_response = leaderboard(
        client, auth(world.member_token), by="key", team_id=str(world.team.id)
    )
    assert member_response.status_code == 200, member_response.text
    rows = member_response.json()["items"]
    assert [row["rank"] for row in rows] == [1, 2, 3, 4]
    assert all_tokens(rows) == [215, 50, 30, 10]
    assert [row["key"]["id"] if row["key"] else None for row in rows] == [
        str(world.member_key.id),
        None,
        None,
        None,
    ]
    assert [row["label"] for row in rows] == ["member-key", "Key #2", "Key #3", "Key #4"]
    assert all(row["owner_kind"] is None and row["owner_id"] is None for row in rows)
    for reference in (world.colleague_key, world.sa_key, world.admin_key):
        assert str(reference.id) not in member_response.text

    for headers in (
        auth(world.team_admin_token),
        auth(world.org_admin_token),
        auth(world.installation_token),
    ):
        rows = board(client, headers, by="key", team_id=str(world.team.id))
        assert [row["key"]["id"] for row in rows] == [str(key_id) for key_id in ranked_ids]
        assert [row["label"] for row in rows] == [
            "member-key",
            "colleague-key",
            "service-key",
            "admin-key",
        ]
        assert [row["team_id"] for row in rows] == [str(world.team.id)] * 4


async def test_owner_board_anonymises_unreadable_members(client, committed) -> None:
    world = await make_world(committed)

    member_response = leaderboard(
        client, auth(world.member_token), by="owner", team_id=str(world.team.id)
    )
    assert member_response.status_code == 200, member_response.text
    rows = member_response.json()["items"]
    assert all_tokens(rows) == [215, 50, 30, 10]
    assert (rows[0]["owner_kind"], rows[0]["owner_id"], rows[0]["is_me"]) == (
        "user",
        str(world.member.id),
        True,
    )
    assert rows[0]["label"] == world.member.display_name
    for rank, row in enumerate(rows[1:], start=2):
        assert row["owner_kind"] is None and row["owner_id"] is None and row["is_me"] is False
        assert row["label"] == f"Member #{rank}"
    for reference in (world.colleague, world.account, world.team_admin):
        assert str(reference.id) not in member_response.text

    rows = board(client, auth(world.team_admin_token), by="owner", team_id=str(world.team.id))
    assert [row["owner_id"] for row in rows] == [
        str(world.member.id),
        str(world.colleague.id),
        str(world.account.id),
        str(world.team_admin.id),
    ]
    assert [row["owner_kind"] for row in rows] == [
        "user",
        "user",
        "service_account",
        "user",
    ]
    assert [row["is_me"] for row in rows] == [False, False, False, True]
    assert rows[2]["label"] == world.account.name
    assert rows[3]["label"] == world.team_admin.display_name

    # Inherited org admins read the whole team too, and the installation admin every team.
    for headers in (auth(world.org_admin_token), auth(world.installation_token)):
        rows = board(client, headers, by="owner", team_id=str(world.team.id))
        assert [row["owner_id"] for row in rows] == [
            str(world.member.id),
            str(world.colleague.id),
            str(world.account.id),
            str(world.team_admin.id),
        ]
        assert all(row["is_me"] is False for row in rows)


async def test_board_defaults_to_llm_and_kind_selects_the_protocol(client, committed) -> None:
    team = await make_team(committed)
    key = await make_key(committed, team, await make_user(committed))
    committed.add(usage_event(key, ts=TS, requested="chat", prompt_tokens=7))
    committed.add(usage_event(key, ts=TS, kind=TrafficKind.MCP, target="srv", method="tools/call"))
    await committed.commit()
    assert [row["requested"] for row in board(client, by="requested", team_id=str(team.id))] == [
        "chat"
    ]
    mcp = board(client, by="requested", team_id=str(team.id), kind="mcp")
    assert [(row["requested"], row["requests"], row["total_tokens"]) for row in mcp] == [("", 1, 0)]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"by": "nope"},
        {"by": "requested", "limit": 0},
        {"by": "requested", "limit": 51},
        {"by": "requested", "from": END.isoformat(), "to": START.isoformat()},
        {"by": "requested", "team_id": str(uuid.uuid4())},
    ],
)
async def test_leaderboard_query_validation(client, params) -> None:
    response = leaderboard(client, **params)
    assert response.status_code in (404, 422), response.text

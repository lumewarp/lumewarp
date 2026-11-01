"""Owner grouping, usage filters, readable teams and monthly totals."""

import uuid
from datetime import UTC, datetime

import pytest

from lumewarp_control_plane.models import ScopeType
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, usage_total, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team

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
    await committed.commit()
    response = query(client, group_by="team", team_id=str(team.id))
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    row = response.json()["items"][0]
    assert (row["team_id"], row["requests"], row["total_tokens"]) == (str(team.id), 7, 100)
    assert row["cost_nano_usd"] == 7000
    assert row["unknown_cost_count"] == 1
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

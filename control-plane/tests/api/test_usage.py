"""Usage filters, grouping, pagination, the totals fast path and scope RBAC."""

from __future__ import annotations

from datetime import UTC, datetime

from lumewarp_control_plane.models import ScopeType
from tests.api.helpers import (
    BOOTSTRAP_AUTH,
    auth,
    grant,
    key_row,
    org_tree,
    usage_event,
    usage_total,
    user_with_token,
)


def _months() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(start.year, start.month + 1, 1, tzinfo=UTC)
    return start, end


def _query(client, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.get(f"/api/v1/usage?{query}", headers=BOOTSTRAP_AUTH)


async def test_events_grouping_filters_and_pagination(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="usage")
    org2, team2, project2 = await org_tree(committed, prefix="usage2")
    key1 = await key_row(committed, project, name="one")
    key2 = await key_row(committed, project2, name="two")
    now = datetime.now(UTC)
    for _index in range(3):
        committed.add(
            usage_event(key1, ts=now, model="alpha", prompt_tokens=10, completion_tokens=5)
        )
    committed.add(usage_event(key1, ts=now, model="beta", prompt_tokens=1, completion_tokens=1))
    committed.add(usage_event(key2, ts=now, model="gamma", prompt_tokens=2, completion_tokens=2))
    await committed.commit()

    resp = _query(client, group_by="day,model")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["items"]
    assert {row["model"] for row in rows} == {"alpha", "beta", "gamma"}
    alpha = next(row for row in rows if row["model"] == "alpha")
    assert alpha["requests"] == 3
    assert alpha["prompt_tokens"] == 30
    assert alpha["completion_tokens"] == 15
    assert alpha["total_tokens"] == 45
    assert alpha["cost_nano_usd"] == 3000

    resp = _query(client, group_by="model", model="beta")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["model"] == "beta"

    # Group by team and filter to one team.
    resp = _query(client, group_by="team", team_id=team2.id)
    assert resp.status_code == 200
    assert [row["team_id"] for row in resp.json()["items"]] == [str(team2.id)]

    # Cursor pagination over grouped rows.
    first = _query(client, group_by="model", limit=1)
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"]
    second = _query(client, group_by="model", limit=10, cursor=body["next_cursor"])
    assert second.status_code == 200
    ids = [row["model"] for row in body["items"] + second.json()["items"]]
    assert sorted(ids) == ["alpha", "beta", "gamma"]

    day_first = _query(client, group_by="day,model", limit=1)
    assert day_first.status_code == 200, day_first.text
    assert day_first.json()["next_cursor"]
    day_second = _query(
        client,
        group_by="day,model",
        limit=10,
        cursor=day_first.json()["next_cursor"],
    )
    assert day_second.status_code == 200, day_second.text
    assert {row["model"] for row in day_first.json()["items"] + day_second.json()["items"]} == {
        "alpha",
        "beta",
        "gamma",
    }

    assert _query(client, group_by="nope").status_code == 422
    assert _query(client, group_by="model", cursor="not-a-cursor").status_code == 422


async def test_totals_fast_path(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="totals")
    key = await key_row(committed, project)
    start, end = _months()
    committed.add(usage_event(key, ts=datetime.now(UTC), model="alpha"))
    committed.add(
        usage_total(
            ScopeType.ORG,
            org.id,
            window_start=start,
            requests=7,
            prompt_tokens=70,
            completion_tokens=30,
            cost_nano_usd=7000,
            unknown_cost_count=1,
        )
    )
    await committed.commit()

    resp = client.get(
        "/api/v1/usage",
        params={
            "group_by": "org",
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["org_id"] == str(org.id)
    assert item["requests"] == 7
    assert item["prompt_tokens"] == 70
    assert item["completion_tokens"] == 30
    assert item["cost_nano_usd"] == 7000
    assert item["unknown_cost_count"] == 1


async def test_usage_rbac_scope(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="usage-rbac")
    other_org, other_team, other_project = await org_tree(committed, prefix="usage-other")
    key = await key_row(committed, project)
    other_key = await key_row(committed, other_project)
    now = datetime.now(UTC)
    committed.add(usage_event(key, ts=now, model="alpha"))
    committed.add(usage_event(other_key, ts=now, model="other"))
    user, secret = await user_with_token(committed)
    await grant(committed, user, org, team=team, role="member")
    await committed.commit()

    own = client.get("/api/v1/usage?group_by=model", headers=auth(secret))
    assert own.status_code == 200
    assert [row["model"] for row in own.json()["items"]] == ["alpha"]

    own_team = client.get(f"/api/v1/usage?group_by=model&team_id={team.id}", headers=auth(secret))
    assert own_team.status_code == 200
    assert [row["model"] for row in own_team.json()["items"]] == ["alpha"]

    # Org-wide usage is for org Owner/Admin, not a team member.
    assert client.get(f"/api/v1/usage?org_id={org.id}", headers=auth(secret)).status_code == 403
    # A team member cannot see another team's usage.
    assert (
        client.get(f"/api/v1/usage?team_id={other_team.id}", headers=auth(secret)).status_code
        == 404
    )
    # Parent hierarchy mismatch in query parameters is a 404, not a leak.
    assert (
        client.get(
            f"/api/v1/usage?key_id={key.id}&team_id={other_team.id}", headers=auth(secret)
        ).status_code
        == 404
    )

"""Per-zone limits and budgets: resolver, outputs, compilation, usage filters, Playground.

Limits and budgets are one row per owner per zone (`zone_limits`), so the same team can
allow 5 rpm in one zone and 50 in another, and its keys are compiled against their own
zone only. Usage is stamped with the reporting gateway token's zone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import CatalogName, ConfigOutbox, ScopeType, TeamRole, Zone
from lumewarp_control_plane.usage import ingest
from lumewarp_control_plane.windows import month_window
from tests.api.helpers import (
    ADMIN_AUTH,
    auth,
    publish_zones,
    put_member_defaults,
    put_zone_limits,
    usage_event,
    usage_total,
    user_with_token,
)
from tests.api.test_playground import SSEStream
from tests.factories import (
    make_key,
    make_member,
    make_model,
    make_org,
    make_router,
    make_service_account,
    make_team,
    make_user,
)

TS = datetime.now(UTC) - timedelta(hours=1)
WINDOW = {
    "from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    "to": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
}


async def create_zone(client, committed, **rules) -> dict:
    """Insert a zone row (zones come from env tokens, not the API) and set its rules."""
    zone_id = f"z-{uuid.uuid4().hex[:10]}"
    committed.add(Zone(id=zone_id, name=zone_id))
    await committed.commit()
    response = client.patch(
        f"/api/v1/zones/{zone_id}",
        json={"name": f"Zone {zone_id.upper()}", **rules},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def compile_zone_snapshot(api_engine, zone: str):
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        return (await compile_zone(session, zone, require_local_membership=True)).snapshot


def wire_key(snapshot, key_id: uuid.UUID) -> pb.ApiKey | None:
    return next((item for item in snapshot.api_keys if item.key_id == str(key_id)), None)


def usage_proto(key, *, cost: int) -> usage_pb2.UsageEvent:
    return usage_pb2.UsageEvent(
        request_id=str(uuid.uuid4()),
        timestamp_unix_ms=int(TS.timestamp() * 1000),
        key_id=str(key.id),
        team_id=str(key.team_id),
        requested="mock",
        model="dep",
        endpoint="chat_completions",
        status=200,
        prompt_tokens=10,
        completion_tokens=5,
        cost_nano_usd=cost,
        cost_known=True,
        duration_ms=10,
        config_version=1,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )


async def test_owner_outputs_carry_limits_and_member_defaults_per_zone(client, committed) -> None:
    zone = await create_zone(client, committed)
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    user = await make_user(committed)
    await make_member(committed, team, user)
    account = await make_service_account(committed, team)
    await committed.commit()

    put_zone_limits(client, "api-test", {"org_id": str(org.id)}, rpm_limit=100)
    put_zone_limits(client, zone["id"], {"org_id": str(org.id)}, rpm_limit=50)
    put_zone_limits(client, "api-test", {"team_id": str(team.id)}, tpm_limit=1_000)
    put_zone_limits(client, zone["id"], {"team_id": str(team.id)}, tpm_limit=2_000)
    put_member_defaults(client, "api-test", str(team.id), rpm_limit=10)
    put_member_defaults(client, zone["id"], str(team.id), rpm_limit=20)
    put_zone_limits(
        client,
        "api-test",
        {"team_id": str(team.id), "user_id": str(user.id)},
        rpm_limit=5,
    )
    put_zone_limits(
        client,
        zone["id"],
        {"team_id": str(team.id), "service_account_id": str(account.id)},
        budget_nano_usd=1_000,
    )

    org_out = client.get(f"/api/v1/orgs/{org.id}", headers=ADMIN_AUTH).json()
    assert org_out["limits_by_zone"]["api-test"]["rpm_limit"] == 100
    assert org_out["limits_by_zone"][zone["id"]]["rpm_limit"] == 50

    team_out = client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).json()
    assert team_out["limits_by_zone"]["api-test"]["tpm_limit"] == 1_000
    assert team_out["limits_by_zone"][zone["id"]]["tpm_limit"] == 2_000
    assert team_out["member_defaults_by_zone"]["api-test"]["rpm_limit"] == 10
    assert team_out["member_defaults_by_zone"][zone["id"]]["rpm_limit"] == 20

    rows = client.get(f"/api/v1/teams/{team.id}/members?limit=500", headers=ADMIN_AUTH).json()
    member = next(row for row in rows["items"] if row["user_id"] == str(user.id))
    assert member["limits_by_zone"]["api-test"]["rpm_limit"] == 5

    account_out = client.get(f"/api/v1/service-accounts/{account.id}", headers=ADMIN_AUTH).json()
    assert account_out["limits_by_zone"][zone["id"]]["budget_nano_usd"] == 1_000


async def test_per_zone_limits_compile_into_that_zones_keys(client, committed, api_engine) -> None:
    first = await create_zone(client, committed)
    second = await create_zone(client, committed)
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user)
    first_key = await make_key(committed, team, user, zone=first["id"])
    second_key = await make_key(committed, team, user, zone=second["id"])
    await committed.commit()
    put_zone_limits(client, first["id"], {"team_id": str(team.id)}, rpm_limit=5)
    put_zone_limits(client, second["id"], {"team_id": str(team.id)}, rpm_limit=50)

    one = await compile_zone_snapshot(api_engine, first["id"])
    scopes = {
        scope.id: scope.limit.requests_per_minute
        for scope in wire_key(one, first_key.id).policy.rate_limit_scopes
    }
    assert scopes[f"team:{team.id}"] == 5
    assert wire_key(one, second_key.id) is None

    two = await compile_zone_snapshot(api_engine, second["id"])
    scopes = {
        scope.id: scope.limit.requests_per_minute
        for scope in wire_key(two, second_key.id).policy.rate_limit_scopes
    }
    assert scopes[f"team:{team.id}"] == 50
    assert wire_key(two, first_key.id) is None


async def test_per_zone_budget_flip_blocks_only_that_zone(client, committed, api_engine) -> None:
    first = await create_zone(client, committed)
    second = await create_zone(client, committed)
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user)
    first_key = await make_key(committed, team, user, zone=first["id"])
    second_key = await make_key(committed, team, user, zone=second["id"])
    await committed.commit()
    put_zone_limits(client, first["id"], {"team_id": str(team.id)}, budget_nano_usd=100)

    async with AsyncSession(committed.bind) as session, session.begin():
        assert (
            await ingest(session, "gw-test", [usage_proto(first_key, cost=150)], zone=first["id"])
            == 1
        )

    async with AsyncSession(committed.bind) as session:
        flipped = await session.scalar(
            select(func.count())
            .select_from(ConfigOutbox)
            .where(
                ConfigOutbox.zone == first["id"],
                ConfigOutbox.reason == "usage budget exceeded",
            )
        )
        assert flipped == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.zone == second["id"],
                    ConfigOutbox.reason == "usage budget exceeded",
                )
            )
            == 0
        )

    one = await compile_zone_snapshot(api_engine, first["id"])
    assert wire_key(one, first_key.id).status == pb.KEY_STATUS_BUDGET_EXCEEDED
    two = await compile_zone_snapshot(api_engine, second["id"])
    assert wire_key(two, second_key.id).status == pb.KEY_STATUS_ACTIVE
    assert wire_key(two, first_key.id) is None


async def test_usage_leaderboard_requests_and_org_usage_filter_by_zone(client, committed) -> None:
    first = await create_zone(client, committed)
    second = await create_zone(client, committed)
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    user = await make_user(committed)
    await make_member(committed, team, user)
    first_key = await make_key(committed, team, user, zone=first["id"])
    second_key = await make_key(committed, team, user, zone=second["id"])
    committed.add(usage_event(first_key, ts=TS, requested="alpha"))
    committed.add(usage_event(first_key, ts=TS, requested="alpha"))
    committed.add(usage_event(second_key, ts=TS, requested="beta"))
    await committed.commit()

    usage = client.get(
        "/api/v1/usage",
        params={**WINDOW, "zone": first["id"], "group_by": "team", "team_id": str(team.id)},
        headers=ADMIN_AUTH,
    ).json()["items"]
    assert [(row["zone"], row["requests"]) for row in usage] == [(first["id"], 2)]

    board = client.get(
        "/api/v1/usage/leaderboard",
        params={"by": "requested", **WINDOW, "zone": second["id"]},
        headers=ADMIN_AUTH,
    ).json()["items"]
    assert {(row["zone"], row["label"], row["requests"]) for row in board} == {
        (second["id"], "beta", 1)
    }

    requests = client.get(
        "/api/v1/requests",
        params={"zone": second["id"], **WINDOW},
        headers=ADMIN_AUTH,
    ).json()["items"]
    assert {row["zone"] for row in requests} == {second["id"]}

    org_usage = client.get(
        f"/api/v1/orgs/{org.id}/usage",
        params={"zone": first["id"], **WINDOW},
        headers=ADMIN_AUTH,
    ).json()
    assert org_usage["zone"] == first["id"]
    assert org_usage["requests"] >= 2


async def test_users_me_reports_limits_and_rules_per_zone(client, committed) -> None:
    zone = await create_zone(
        client,
        committed,
        name="Dev",
        playground_allowed=False,
        key_max_lifetime_seconds=600,
        key_min_creator_role="team_admin",
    )
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    put_zone_limits(client, "api-test", {"team_id": str(team.id)}, rpm_limit=10)
    put_zone_limits(client, zone["id"], {"team_id": str(team.id)}, rpm_limit=40)

    me = client.get("/api/v1/users/me", headers=auth(token)).json()
    row = next(item for item in me["teams"] if item["team_id"] == str(team.id))
    assert set(row["by_zone"]) >= {"api-test", zone["id"]}
    assert row["limits"]["rpm_limit"] == 10
    dev = row["by_zone"][zone["id"]]
    assert dev["name"] == "Dev"
    assert dev["limits"]["rpm_limit"] == 40
    assert dev["key_rules"]["playground_allowed"] is False
    assert dev["key_rules"]["key_max_lifetime_seconds"] == 600
    assert dev["key_rules"]["key_min_creator_role"] == "team_admin"
    assert dev["key_rules"]["key_max_per_member"] is None
    assert dev["key_count"] == 0
    assert "budget" in dev and "key_rules" in dev


async def test_playground_offers_and_forwards_only_a_zones_playable_keys(
    client, committed, monkeypatch
) -> None:
    disallowed = await create_zone(
        client, committed, playground_allowed=False, playground_gateway_url="http://gw-a:4000"
    )
    missing_url = await create_zone(client, committed, playground_allowed=True)
    allowed = await create_zone(
        client, committed, playground_allowed=True, playground_gateway_url="http://gw-b:4000"
    )
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    disallowed_key = await make_key(committed, team, user, zone=disallowed["id"])
    missing_key = await make_key(committed, team, user, zone=missing_url["id"])
    allowed_key = await make_key(committed, team, user, zone=allowed["id"])
    cookie = await sessions.create_session(committed, user.id, 3600)
    await committed.commit()
    headers = auth("session:" + cookie)

    body = client.get("/api/v1/playground", headers=headers).json()
    by_id = {key["id"]: key for key in body["keys"]}
    assert str(allowed_key.id) in by_id
    assert by_id[str(allowed_key.id)]["zone"] == allowed["id"]
    assert str(disallowed_key.id) not in by_id
    assert str(missing_key.id) not in by_id

    refused = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(disallowed_key.id)},
        json={"model": "mock"},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "playground_not_allowed"
    disabled = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(missing_key.id)},
        json={"model": "mock"},
    )
    assert disabled.status_code == 503, disabled.text
    assert disabled.json()["error"]["code"] == "playground_disabled"

    forwarded: dict[str, str] = {}

    async def send(request, **kwargs):
        forwarded["url"] = str(request.url)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSEStream()
        )

    monkeypatch.setattr(client.app.state.playground_client, "send", send)
    ok = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(allowed_key.id)},
        json={"model": "mock"},
    )
    assert ok.status_code == 200, ok.text
    assert forwarded["url"] == "http://gw-b:4000/v1/chat/completions"


async def test_playground_lists_only_names_served_in_the_keys_zone(
    client, committed, api_engine
) -> None:
    """A key is offered only what its zone's gateways serve: a model deployed in another
    zone (and a router over it) is not listed for it."""
    here = await create_zone(
        client, committed, playground_allowed=True, playground_gateway_url="http://gw-a:4000"
    )
    there = await create_zone(
        client, committed, playground_allowed=True, playground_gateway_url="http://gw-b:4000"
    )
    deployed = await make_model(committed, zone=here["id"])
    router = await make_router(committed, members=[deployed])
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    key_here = await make_key(committed, team, user, zone=here["id"])
    key_there = await make_key(committed, team, user, zone=there["id"])
    cookie = await sessions.create_session(committed, user.id, 3600)
    await committed.commit()
    await publish_zones(api_engine, here["id"], there["id"])

    body = client.get("/api/v1/playground", headers=auth("session:" + cookie)).json()
    models = {key["id"]: set(key["models"]) for key in body["keys"]}
    names = set(
        await committed.scalars(
            select(CatalogName.name).where(CatalogName.id.in_([deployed.name_id, router.name_id]))
        )
    )
    assert len(names) == 2
    assert names <= models[str(key_here.id)], models
    assert not names & models[str(key_there.id)], models


async def test_team_totals_pagination_is_per_zone(client, committed) -> None:
    """The totals fast path groups by zone and team, so its cursor must carry both."""
    first = await create_zone(client, committed)
    second = await create_zone(client, committed)
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    start, end = month_window(datetime.now(UTC))
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=start, requests=3, zone=first["id"])
    )
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=start, requests=7, zone=second["id"])
    )
    await committed.commit()
    params = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "group_by": "team",
        "team_id": str(team.id),
    }
    whole = client.get("/api/v1/usage", params=params, headers=ADMIN_AUTH).json()["items"]
    assert {row["zone"]: row["requests"] for row in whole} == {
        first["id"]: 3,
        second["id"]: 7,
    }
    page = client.get("/api/v1/usage", params={**params, "limit": 1}, headers=ADMIN_AUTH).json()
    assert page["next_cursor"]
    rest = client.get(
        "/api/v1/usage",
        params={**params, "cursor": page["next_cursor"]},
        headers=ADMIN_AUTH,
    ).json()["items"]
    assert page["items"] + rest == whole

"""The Alertmanager receiver and the `/status` view."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import Alert, AlertTarget, Incident, ModelAccess
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import ADMIN_AUTH, auth, user_with_token
from tests.factories import (
    grant,
    make_alias,
    make_member,
    make_model,
    make_router,
    make_team,
    make_upstream,
    zone,
)

TOKEN = "alertmanager-test-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
UNSET_END = "0001-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
async def clean_alerts(api_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with AsyncSession(api_engine) as session, session.begin():
        await session.execute(delete(AlertTarget))
        await session.execute(delete(Alert))
        await session.execute(delete(Incident))


@pytest.fixture
def am_client(api_settings: Settings) -> AsyncIterator[TestClient]:
    settings = api_settings.model_copy(update={"alertmanager_token": SecretStr(TOKEN)})
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def alert(
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    status: str = "firing",
    starts_at: datetime | None = None,
    ends_at: str | None = None,
    fingerprint: str | None = None,
) -> dict:
    starts = starts_at or datetime.now(UTC) - timedelta(minutes=5)
    return {
        "status": status,
        "labels": {"alertname": "TestAlert", **(labels or {})},
        "annotations": annotations or {},
        "startsAt": starts.isoformat().replace("+00:00", "Z"),
        "endsAt": ends_at or UNSET_END,
        "generatorURL": "http://prometheus/graph?g0.expr=up",
        "fingerprint": fingerprint or uuid.uuid4().hex[:16],
    }


def payload(*alerts: dict) -> dict:
    return {"version": "4", "status": "firing", "alerts": list(alerts)}


def post(client: TestClient, *alerts: dict, headers: dict | None = None):
    return client.post(
        "/api/v1/alerts/alertmanager",
        json=payload(*alerts),
        headers=BEARER if headers is None else headers,
    )


def incidents(client: TestClient, alertname: str | None = None, **params) -> list[dict]:
    """Incidents, optionally only those with exactly this alertname (filtered here; the API
    takes a substring `q`)."""
    if alertname is not None:
        params["q"] = alertname
    response = client.get("/api/v1/incidents", params=params, headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    if alertname is not None:
        items = [item for item in items if item["alertname"] == alertname]
    return items


def status_items(client: TestClient, headers: dict) -> list[dict]:
    response = client.get("/api/v1/status", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_unset_token_is_404(client) -> None:
    response = post(client, alert(), headers=BEARER)
    assert response.status_code == 404


def test_missing_and_wrong_token_is_401(am_client) -> None:
    assert post(am_client, alert(), headers={}).status_code == 401
    assert post(am_client, alert(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert post(am_client, alert(), headers={"Authorization": "Basic abc"}).status_code == 401
    # A session never authenticates the receiver.
    assert post(am_client, alert(), headers=ADMIN_AUTH).status_code == 401


def test_invalid_payload_is_rejected(am_client) -> None:
    bad = alert()
    bad["status"] = "weird"
    assert post(am_client, bad).status_code == 422


def test_replay_is_idempotent_and_resolve_sets_ends_at(am_client) -> None:
    starts = datetime.now(UTC) - timedelta(minutes=30)
    fingerprint = uuid.uuid4().hex[:16]
    firing = alert(starts_at=starts, fingerprint=fingerprint, labels={"zone": ""})
    for _ in range(3):
        response = post(am_client, firing)
        assert response.status_code == 200, response.text
        assert response.json() == {"received": 1}
    rows = incidents(am_client, alertname="TestAlert")
    assert len(rows) == 1
    assert rows[0]["status"] == "firing"
    assert rows[0]["ends_at"] is None
    assert rows[0]["zone"] is None  # an empty zone label means every zone

    ends = starts + timedelta(minutes=20)
    resolved = {**firing, "status": "resolved", "endsAt": ends.isoformat().replace("+00:00", "Z")}
    assert post(am_client, resolved).status_code == 200
    # A stale firing redelivery must not reopen the resolved alert.
    assert post(am_client, firing).status_code == 200
    rows = incidents(am_client, alertname="TestAlert")
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"
    assert rows[0]["duration_seconds"] == 20 * 60
    assert datetime.fromisoformat(rows[0]["ends_at"]) == ends.replace(microsecond=ends.microsecond)

    # A new start of the same fingerprint is a new incident.
    again = alert(fingerprint=fingerprint, starts_at=datetime.now(UTC))
    assert post(am_client, again).status_code == 200
    assert len(incidents(am_client, alertname="TestAlert")) == 2


async def test_target_resolution(am_client, committed) -> None:
    zone_a = am_client.app.state.settings.zone
    await zone(committed, "alert-zone-b")
    upstream_a = await make_upstream(committed, "shared-up", zone=zone_a)
    upstream_b = await make_upstream(committed, "shared-up", zone="alert-zone-b")
    suffix = uuid.uuid4().hex[:8]
    model = await make_model(committed, f"res-model-{suffix}", upstream_a)
    other = await make_model(committed, f"res-other-{suffix}", upstream_a)
    router = await make_router(committed, f"res-router-{suffix}", [other])
    await make_alias(committed, f"res-alias-{suffix}", model)
    await committed.commit()
    model_name = f"res-model-{suffix}"

    def targets_of(row: dict) -> set[tuple[str, str]]:
        return {(item["kind"], item["name"]) for item in row["targets"]}

    cases = {
        "requested": (
            {"requested": f"res-alias-{suffix}", "model": model_name},
            {("alias", f"res-alias-{suffix}"), ("model", model_name)},
        ),
        "router": ({"requested": f"res-router-{suffix}"}, {("router", f"res-router-{suffix}")}),
        "upstream_all_zones": (
            {"upstream": "shared-up"},
            {("upstream", "shared-up")},
        ),
        "upstream_zone": ({"upstream": "shared-up", "zone": zone_a}, {("upstream", "shared-up")}),
        "unresolved": ({"requested": f"no-such-{suffix}", "upstream": "no-up"}, set()),
    }
    for case, (labels, _) in cases.items():
        assert post(am_client, alert(labels={**labels, "alertname": case})).status_code == 200
    for case, (_, expected) in cases.items():
        rows = incidents(am_client, alertname=case)
        assert len(rows) == 1, case
        assert targets_of(rows[0]) == expected, case

    # An upstream with no zone label resolves in every zone that has it; a zone label limits it.
    async with committed.begin():
        ids = {
            case: set(
                await committed.scalars(
                    select(AlertTarget.target_id)
                    .join(Alert, Alert.id == AlertTarget.alert_id)
                    .where(Alert.alertname == case)
                )
            )
            for case in ("upstream_all_zones", "upstream_zone")
        }
    assert ids["upstream_all_zones"] == {upstream_a.id, upstream_b.id}
    assert ids["upstream_zone"] == {upstream_a.id}
    assert router.id is not None


async def test_replay_rewrites_targets(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    name = f"late-model-{suffix}"
    firing = alert(labels={"requested": name})
    assert post(am_client, firing).status_code == 200
    assert incidents(am_client, alertname="TestAlert")[0]["targets"] == []
    await make_model(committed, name)
    await committed.commit()
    assert post(am_client, firing).status_code == 200
    rows = incidents(am_client, alertname="TestAlert")
    assert len(rows) == 1
    assert [item["name"] for item in rows[0]["targets"]] == [name]


async def test_status_by_grant_and_visibility(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    granted = f"st-granted-{suffix}"
    hidden = f"st-hidden-{suffix}"
    team = await make_team(committed, model_access=ModelAccess.LIST)
    other_team = await make_team(committed, model_access=ModelAccess.LIST)
    member, member_token = await user_with_token(committed)
    stranger, stranger_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_member(committed, other_team, stranger)
    await make_model(committed, granted)
    await make_model(committed, hidden)
    await grant(committed, team, [granted])
    await committed.commit()

    am = {"lumewarp_summary": "slow"}
    assert (
        post(
            am_client,
            alert(labels={"requested": granted, "lumewarp_status": "down"}, annotations=am),
            alert(
                labels={"requested": hidden, "lumewarp_status": "down", "alertname": "H"},
                annotations=am,
            ),
            # No lumewarp_status: admin side only.
            alert(labels={"requested": granted, "alertname": "AdminOnly"}),
            # Resolved alerts never show.
            alert(
                labels={"requested": granted, "lumewarp_status": "down", "alertname": "Old"},
                status="resolved",
                ends_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        ).status_code
        == 200
    )

    items = status_items(am_client, auth(member_token))
    assert [(item["name"], item["state"], item["summary"]) for item in items] == [
        (granted, "down", "slow")
    ]
    assert items[0].get("incident_id") is None
    assert items[0]["zone"] == am_client.app.state.settings.zone
    assert status_items(am_client, auth(stranger_token)) == []


async def test_status_upstream_target_maps_to_deployed_models(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    upstream = await make_upstream(committed, f"st-up-{suffix}")
    on = f"st-on-{suffix}"
    off = f"st-off-{suffix}"
    team = await make_team(committed, model_access=ModelAccess.LIST)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_model(committed, on, upstream)
    await make_model(committed, off)
    await grant(committed, team, [on, off])
    await committed.commit()

    assert (
        post(
            am_client, alert(labels={"upstream": f"st-up-{suffix}", "lumewarp_status": "degraded"})
        ).status_code
        == 200
    )
    items = status_items(am_client, auth(member_token))
    assert [item["name"] for item in items] == [on]
    assert "st-up" not in str(items)


async def test_status_zone_scoped_alert_only_that_zone(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    name = f"st-zone-{suffix}"
    team = await make_team(committed, model_access=ModelAccess.LIST)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_model(committed, name)
    await grant(committed, team, [name])
    await committed.commit()
    other_zone = "elsewhere"
    await zone(committed, other_zone)
    await committed.commit()

    post(
        am_client, alert(labels={"requested": name, "lumewarp_status": "down", "zone": other_zone})
    )
    assert status_items(am_client, auth(member_token)) == []
    post(
        am_client,
        alert(
            labels={
                "requested": name,
                "lumewarp_status": "down",
                "zone": am_client.app.state.settings.zone,
                "alertname": "Z",
            }
        ),
    )
    assert len(status_items(am_client, auth(member_token))) == 1


def test_status_requires_sign_in(client) -> None:
    assert client.get("/api/v1/status").status_code == 401


async def _member_with(committed, names: list[str]):
    team = await make_team(committed, model_access=ModelAccess.LIST)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await grant(committed, team, names)
    await committed.commit()
    return auth(member_token)


async def test_status_dedupes_per_zone_and_name(am_client, committed) -> None:
    name = f"st-dup-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    headers = await _member_with(committed, [name])
    now = datetime.now(UTC)
    post(
        am_client,
        alert(
            labels={"model": name, "lumewarp_status": "congested", "alertname": "Q"},
            annotations={"lumewarp_summary": "queue"},
            starts_at=now - timedelta(minutes=30),
        ),
        alert(
            labels={"model": name, "lumewarp_status": "down", "alertname": "D1"},
            annotations={"lumewarp_summary": "late down"},
            starts_at=now - timedelta(minutes=5),
        ),
        alert(
            labels={"model": name, "lumewarp_status": "down", "alertname": "D2"},
            annotations={"lumewarp_summary": "early down"},
            starts_at=now - timedelta(minutes=10),
        ),
    )
    items = status_items(am_client, headers)
    assert len(items) == 1
    assert (items[0]["state"], items[0]["summary"]) == ("down", "early down")
    assert datetime.fromisoformat(items[0]["since"]) == (now - timedelta(minutes=10))

    assert items[0].get("incident_id") is None
    ids = {row["alertname"]: row["id"] for row in incidents(am_client)}
    admin = [item for item in status_items(am_client, ADMIN_AUTH) if item["name"] == name]
    assert [item["incident_id"] for item in admin] == [ids["D2"]]


async def test_alias_and_router_callers_see_a_model_alert(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    model = await make_model(committed, f"st-m-{suffix}")
    other = await make_model(committed, f"st-o-{suffix}")
    alias = await make_alias(committed, f"st-alias-{suffix}", model)
    pool = await make_router(committed, f"st-pool-{suffix}", [model, other])
    via_router = await make_alias(committed, f"st-ralias-{suffix}", pool)
    alias_user = await _member_with(committed, [alias.name])
    router_user = await _member_with(committed, [pool.name])
    router_alias_user = await _member_with(committed, [via_router.name])
    unrelated = await _member_with(committed, [other.name])
    await committed.commit()

    post(am_client, alert(labels={"model": model.name, "lumewarp_status": "down"}))
    assert [i["name"] for i in status_items(am_client, alias_user)] == [alias.name]
    assert [i["name"] for i in status_items(am_client, router_user)] == [pool.name]
    assert [i["name"] for i in status_items(am_client, router_alias_user)] == [via_router.name]
    # `other` is not the alerted model: only a name that serves `model` is affected.
    assert all(i["name"] != other.name for i in status_items(am_client, unrelated))


async def test_alias_of_a_router_follows_an_upstream_alert(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    upstream = await make_upstream(committed, f"st-ru-{suffix}")
    model = await make_model(committed, f"st-rm-{suffix}", upstream)
    pool = await make_router(committed, f"st-rp-{suffix}", [model])
    headers = await _member_with(committed, [pool.name])
    post(am_client, alert(labels={"upstream": upstream.name, "lumewarp_status": "degraded"}))
    assert [i["name"] for i in status_items(am_client, headers)] == [pool.name]


async def test_callability_mirrors_the_compiler(am_client, committed) -> None:
    from lumewarp_control_plane.models import RouterSplit, RouterSplitMember

    suffix = uuid.uuid4().hex[:8]
    live = await make_model(committed, f"st-live-{suffix}")
    gone = await make_model(committed, f"st-gone-{suffix}", deploy=False)
    # Only a fallback is deployed: the compiler omits the router.
    fallback_only = await make_router(committed, f"st-fb-{suffix}", [gone], fallbacks=[live])
    # A split router needs every split deployed.
    split = await make_router(committed, f"st-split-{suffix}")
    for position, member in enumerate((live, gone)):
        row = RouterSplit(router_id=split.id, name=f"s{position}", weight=1, position=position)
        committed.add(row)
        await committed.flush()
        committed.add(RouterSplitMember(split_id=row.id, model_id=member.id, weight=1, position=0))
    split_alias = await make_alias(committed, f"st-sa-{suffix}", split)
    # Control: a router with a deployed primary is callable.
    ok = await make_router(committed, f"st-ok-{suffix}", [live], fallbacks=[gone])
    headers = await _member_with(
        committed, [fallback_only.name, split.name, split_alias.name, ok.name]
    )
    post(am_client, alert(labels={"model": live.name, "lumewarp_status": "down"}))
    assert [i["name"] for i in status_items(am_client, headers)] == [ok.name]


def test_invalid_body_does_not_beat_the_token_check(client, am_client) -> None:
    junk = {"content": b"{not json", "headers": {"content-type": "application/json"}}
    assert client.post("/api/v1/alerts/alertmanager", **junk).status_code == 404
    bad = {**junk, "headers": {**junk["headers"], "Authorization": "Bearer nope"}}
    assert am_client.post("/api/v1/alerts/alertmanager", **bad).status_code == 401
    ok = {**junk, "headers": {**junk["headers"], **BEARER}}
    assert am_client.post("/api/v1/alerts/alertmanager", **ok).status_code == 422
    wrong_version = {**payload(alert()), "version": "3"}
    assert (
        am_client.post(
            "/api/v1/alerts/alertmanager", json=wrong_version, headers=BEARER
        ).status_code
        == 422
    )


def test_oversized_chunked_body_is_refused(am_client) -> None:
    def chunks():
        for _ in range(3):
            yield b" " * (512 * 1024)

    response = am_client.post(
        "/api/v1/alerts/alertmanager",
        content=chunks(),
        headers={**BEARER, "content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_alias_target_alert_reaches_its_own_callers(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    model = await make_model(committed, f"st-tm-{suffix}")
    alias = await make_alias(committed, f"st-direct-{suffix}", model)
    headers = await _member_with(committed, [alias.name])
    zone_id = am_client.app.state.settings.zone
    post(
        am_client,
        alert(labels={"requested": alias.name, "lumewarp_status": "down", "zone": zone_id}),
    )
    assert [i["name"] for i in status_items(am_client, headers)] == [alias.name]

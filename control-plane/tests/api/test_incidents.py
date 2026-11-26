# ruff: noqa: F811
"""Incident history, grouping of alerts into incidents, affected names and traffic."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumewarp_control_plane.alerts import IncomingAlert, receive
from lumewarp_control_plane.models import Alert, Incident, ModelDeployment
from tests.api.helpers import ADMIN_AUTH, auth, usage_event, user_with_token
from tests.api.test_alerts import (  # noqa: F401, F811  (fixtures)
    BEARER,
    alert,
    am_client,
    clean_alerts,
    incidents,
    post,
)
from tests.factories import (
    make_alias,
    make_key,
    make_member,
    make_model,
    make_router,
    make_team,
    make_upstream,
    make_user,
    zone,
)

API = "/api/v1"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def detail(client, incident_id: str) -> dict:
    response = client.get(f"{API}/incidents/{incident_id}", headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_incidents_are_installation_admin_only(am_client) -> None:
    assert am_client.get(f"{API}/incidents").status_code == 401


async def test_non_admin_is_refused(am_client, committed) -> None:
    _, token = await user_with_token(committed)
    await committed.commit()
    for path in ("/incidents", f"/incidents/{uuid.uuid4()}"):
        response = am_client.get(f"{API}{path}", headers=auth(token))
        assert response.status_code == 403, path


def test_list_filters_and_keyset_pagination(am_client) -> None:
    zone = am_client.app.state.settings.zone
    base = datetime.now(UTC) - timedelta(hours=3)
    alerts = []
    for index in range(5):
        alerts.append(
            alert(
                labels={"alertname": f"Page{index % 2}", "zone": zone if index < 3 else "other"},
                starts_at=base + timedelta(minutes=index * 10),
                status="resolved" if index == 0 else "firing",
                ends_at=iso(base + timedelta(minutes=5)) if index == 0 else None,
            )
        )
    assert post(am_client, *alerts).status_code == 200

    everything = incidents(am_client)
    assert [row["starts_at"] for row in everything] == sorted(
        (row["starts_at"] for row in everything), reverse=True
    )
    seen = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        response = am_client.get(f"{API}/incidents", params=params, headers=ADMIN_AUTH)
        assert response.status_code == 200
        body = response.json()
        seen += [row["id"] for row in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [row["id"] for row in everything]
    assert len(set(seen)) == 5

    assert len(incidents(am_client, status="resolved")) == 1
    assert len(incidents(am_client, alertname="Page1")) == 2
    # `zone` includes the zone's alerts plus alerts without one.
    assert len(incidents(am_client, zone=zone)) == 3
    window = incidents(
        am_client,
        **{"from": iso(base + timedelta(minutes=15)), "to": iso(base + timedelta(minutes=35))},
    )
    assert len(window) == 2
    bad = am_client.get(f"{API}/incidents", params={"cursor": "garbage"}, headers=ADMIN_AUTH)
    assert bad.status_code == 422


async def test_detail_traffic_related_and_window(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    name = f"inc-model-{suffix}"
    other = f"inc-other-{suffix}"
    await make_model(committed, name)
    await make_model(committed, other)
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    zone = am_client.app.state.settings.zone

    # On a whole minute, so the first three events share one per-minute bucket.
    starts = (datetime.now(UTC) - timedelta(minutes=40)).replace(second=0, microsecond=0)
    ends = starts + timedelta(minutes=20)
    inside = starts + timedelta(minutes=2)

    def event(requested, ts, status=200, **fields):
        row = usage_event(key, ts=ts, requested=requested, prompt_tokens=10, completion_tokens=5)
        row.status = status
        for field, value in fields.items():
            setattr(row, field, value)
        return row

    committed.add_all(
        [
            event(name, inside, ttft_ms=100, prompt_tokens=100),
            event(
                name,
                inside + timedelta(seconds=10),
                fallback=True,
                attempts=2,
                ttft_ms=300,
                prompt_tokens=300,
            ),
            event(
                name, inside + timedelta(seconds=20), status=503, error_code="no_healthy_upstream"
            ),
            event(name, inside + timedelta(minutes=3), status=429, error_code="rate_limited"),
            event(other, inside),  # another name
            event(name, starts - timedelta(minutes=30)),  # before the window
            event(name, ends + timedelta(minutes=5)),  # after the window
        ]
    )
    await committed.commit()

    first = alert(
        labels={"alertname": "Detail", "requested": name, "zone": zone},
        starts_at=starts,
        status="resolved",
        ends_at=iso(ends),
        fingerprint="detail-a",
    )
    sibling = alert(
        labels={"alertname": "Sibling", "model": name},
        starts_at=starts + timedelta(minutes=5),
        fingerprint="detail-b",
    )
    unrelated = alert(
        labels={"alertname": "Far", "requested": name},
        starts_at=starts - timedelta(hours=5),
        status="resolved",
        ends_at=iso(starts - timedelta(hours=4)),
        fingerprint="detail-c",
    )
    assert post(am_client, first, sibling, unrelated).status_code == 200
    incident_id = incidents(am_client, alertname="Detail")[0]["id"]

    response = am_client.get(f"{API}/incidents/{incident_id}", headers=ADMIN_AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["alertname"] == "Detail"
    assert body["alert_count"] == 1 and body["gateways"] == []
    assert [row["status"] for row in body["alerts"]] == ["resolved"]
    assert body["affected"] == [{"zone": zone, "name": name}]
    assert [row["alertname"] for row in body["related"]] == ["Sibling"]
    traffic = body["traffic"]
    assert traffic["clipped"] is False
    assert traffic["requests"] == 4
    assert traffic["failed_count"] == 2
    assert sum(item["requests"] for item in traffic["per_minute"]) == 4
    assert sum(item["failed"] for item in traffic["per_minute"]) == 2
    assert sum(item["fallback"] for item in traffic["per_minute"]) == 1
    # The first minute: two streamed successes, a 503 with no usage (not a context sample).
    first_minute = next(item for item in traffic["per_minute"] if item["ttft_avg_ms"])
    assert first_minute["ttft_avg_ms"] == 200.0
    assert first_minute["ttft_p95_ms"] == pytest.approx(290.0)
    assert first_minute["context_avg_tokens"] == pytest.approx((100 + 300 + 10) / 3, abs=0.1)
    later = [item for item in traffic["per_minute"] if item is not first_minute]
    assert all(item["ttft_avg_ms"] is None and item["ttft_p95_ms"] is None for item in later)
    assert {item["code"]: item["count"] for item in traffic["errors_by_code"]} == {
        "no_healthy_upstream": 1,
        "rate_limited": 1,
    }
    assert {item["status_class"]: item["count"] for item in traffic["errors_by_status_class"]} == {
        "5xx": 1,
        "4xx": 1,
    }
    assert traffic["fallback_requests"] == 1
    assert traffic["fallback_share"] == pytest.approx(0.25)
    assert traffic["rescued_count"] == 1
    assert traffic["rescued_count"] == sum(item["fallback"] for item in traffic["per_minute"])
    assert {item["attempts"]: item["count"] for item in traffic["attempts"]} == {0: 3, 2: 1}
    assert traffic["teams"][0]["team_id"] == str(team.id)
    assert traffic["teams"][0]["requests"] == 4
    assert traffic["teams"][0]["prompt_tokens"] == 420
    assert traffic["teams"][0]["failed"] == 2
    assert traffic["keys"][0]["failed"] == 2
    assert traffic["keys"][0]["key_id"] == str(key.id)
    assert traffic["keys"][0]["key"]["name"] == key.name
    failed = traffic["failed_requests"]
    assert {row["error_code"] for row in failed} == {"no_healthy_upstream", "rate_limited"}
    assert all(row["requested"] == name for row in failed)

    assert am_client.get(f"{API}/incidents/{uuid.uuid4()}", headers=ADMIN_AUTH).status_code == 404


async def test_long_incident_is_clipped_to_24_hours(am_client) -> None:
    starts = datetime.now(UTC) - timedelta(days=5)
    post(am_client, alert(labels={"alertname": "Long"}, starts_at=starts))
    incident_id = incidents(am_client, alertname="Long")[0]["id"]
    body = am_client.get(f"{API}/incidents/{incident_id}", headers=ADMIN_AUTH).json()
    assert body["traffic"]["clipped"] is True
    start = datetime.fromisoformat(body["traffic"]["window_start"])
    end = datetime.fromisoformat(body["traffic"]["window_end"])
    assert end - start <= timedelta(hours=24, seconds=1)


async def test_truncated_streams_count_as_failures(am_client, committed) -> None:
    from lumewarp_control_plane.models import RequestOutcome

    name = f"inc-cut-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    starts = datetime.now(UTC) - timedelta(minutes=30)
    cut = usage_event(key, ts=starts + timedelta(minutes=2), requested=name)
    cut.status = 200
    cut.outcome = RequestOutcome.UPSTREAM_ERROR
    fine = usage_event(key, ts=starts + timedelta(minutes=3), requested=name)
    committed.add_all([cut, fine])
    await committed.commit()
    assert post(am_client, alert(labels={"requested": name}, starts_at=starts)).status_code == 200
    incident_id = incidents(am_client)[0]["id"]
    traffic = am_client.get(f"{API}/incidents/{incident_id}", headers=ADMIN_AUTH).json()["traffic"]
    assert [row["request_id"] for row in traffic["failed_requests"]] == [str(cut.request_id)]
    assert {item["status_class"]: item["count"] for item in traffic["errors_by_status_class"]} == {
        "2xx": 1
    }


def _gateway_alert(instance: str, starts: datetime, name: str, **kw) -> dict:
    return alert(
        labels={
            "alertname": "Grouped",
            "model": name,
            "instance": instance,
            **kw.pop("labels", {}),
        },
        starts_at=starts,
        fingerprint=f"grp-{name}-{instance}",
        **kw,
    )


async def test_gateways_reporting_one_outage_share_an_incident(am_client, committed) -> None:
    name = f"grp-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    await committed.commit()
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    gateways = [f"10.0.0.{index}:9466" for index in range(4)]
    starts = [base + timedelta(seconds=15 * index) for index in range(4)]
    # Separate deliveries, out of order, with staggered starts.
    for index in (2, 0, 3, 1):
        assert (
            post(am_client, _gateway_alert(gateways[index], starts[index], name)).status_code == 200
        )

    rows = incidents(am_client, alertname="Grouped")
    assert len(rows) == 1
    row = rows[0]
    assert row["alert_count"] == 4
    assert row["gateways"] == gateways
    assert row["status"] == "firing" and row["ends_at"] is None
    assert datetime.fromisoformat(row["starts_at"]) == base
    assert row["targets"] == [{"kind": "model", "name": name}]

    # One member resolved while the others fire: the incident still fires.
    ends = [base + timedelta(minutes=10 + index) for index in range(4)]

    def resolved(index: int) -> dict:
        return _gateway_alert(gateways[index], starts[index], name, status="resolved",
                              ends_at=iso(ends[index]))  # fmt: skip

    post(am_client, resolved(0))
    row = incidents(am_client, alertname="Grouped")[0]
    assert row["status"] == "firing" and row["ends_at"] is None
    # All resolved: the incident ends at the latest member end.
    post(am_client, resolved(3), resolved(1), resolved(2))
    row = incidents(am_client, alertname="Grouped")[0]
    assert row["status"] == "resolved"
    assert datetime.fromisoformat(row["ends_at"]) == ends[3]
    assert row["duration_seconds"] == int((ends[3] - base).total_seconds())

    # Replays change nothing.
    post(am_client, *(resolved(index) for index in range(4)))
    post(am_client, _gateway_alert(gateways[0], starts[0], name))  # stale firing
    rows = incidents(am_client, alertname="Grouped")
    assert len(rows) == 1 and rows[0]["alert_count"] == 4 and rows[0]["status"] == "resolved"

    body = detail(am_client, rows[0]["id"])
    assert [item["instance"] for item in body["alerts"]] == gateways
    assert all(item["status"] == "resolved" for item in body["alerts"])

    # A later firing that does not overlap opens a new incident.
    later = _gateway_alert(gateways[0], base + timedelta(minutes=20), name)
    post(am_client, later)
    rows = incidents(am_client, alertname="Grouped")
    assert len(rows) == 2
    assert [row["status"] for row in rows] == ["firing", "resolved"]
    assert rows[0]["alert_count"] == 1
    # The two incidents share the model, so each lists the other as related.
    assert [item["id"] for item in detail(am_client, rows[0]["id"])["related"]] == [rows[1]["id"]]


def test_unresolved_alerts_group_by_labels_without_instance(am_client) -> None:
    starts = datetime.now(UTC) - timedelta(minutes=10)
    common = {"alertname": "NoTarget", "job": "x"}
    post(
        am_client,
        alert(labels={**common, "instance": "a", "pod": "p1"}, starts_at=starts),
        alert(labels={**common, "instance": "b", "pod": "p2", "job": "y"}, starts_at=starts),
        alert(labels={**common, "instance": "c", "code": "503"}, starts_at=starts),
    )
    rows = incidents(am_client, alertname="NoTarget")
    assert sorted(row["alert_count"] for row in rows) == [1, 2]
    grouped = next(row for row in rows if row["alert_count"] == 2)
    assert grouped["gateways"] == ["a", "b"]


async def test_incident_takes_the_worst_member(am_client, committed) -> None:
    name = f"worst-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    await committed.commit()
    starts = datetime.now(UTC) - timedelta(minutes=10)
    post(
        am_client,
        _gateway_alert("g1", starts, name, labels={"severity": "warning",
                                                   "lumewarp_status": "degraded"}),
        _gateway_alert("g2", starts, name, labels={"severity": "critical",
                                                   "lumewarp_status": "down"}),
    )  # fmt: skip
    row = incidents(am_client, alertname="Grouped")[0]
    assert (row["severity"], row["lumewarp_status"]) == ("critical", "down")


async def test_zone_and_alertname_split_incidents(am_client, committed) -> None:
    name = f"split-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    await committed.commit()
    starts = datetime.now(UTC) - timedelta(minutes=10)
    post(
        am_client,
        _gateway_alert("g1", starts, name),
        _gateway_alert("g2", starts, name, labels={"zone": "default"}),
        _gateway_alert("g3", starts, name, labels={"alertname": "Other"}),
    )
    assert len(incidents(am_client, alertname="Grouped")) == 2
    assert len(incidents(am_client, alertname="Other")) == 1


def _incoming(fingerprint: str, instance: str, starts: datetime) -> IncomingAlert:
    return IncomingAlert(
        status="firing",
        labels={"alertname": "Concurrent", "instance": instance},
        annotations={},
        starts_at=starts,
        ends_at=None,
        generator_url=None,
        fingerprint=fingerprint,
    )


async def test_concurrent_deliveries_open_one_incident(am_client, api_engine: AsyncEngine) -> None:
    starts = datetime.now(UTC) - timedelta(minutes=5)

    async def deliver(index: int) -> None:
        async with AsyncSession(api_engine) as session, session.begin():
            await receive(session, [_incoming(f"conc-{index}", f"gw-{index}", starts)])

    await asyncio.gather(*(deliver(index) for index in range(4)))
    async with AsyncSession(api_engine) as session:
        count = await session.scalar(
            select(func.count()).select_from(Incident).where(Incident.alertname == "Concurrent")
        )
        members = await session.scalar(
            select(func.count()).select_from(Alert).where(Alert.alertname == "Concurrent")
        )
    assert (count, members) == (1, 4)


@pytest.mark.parametrize("existing", [False, True])
async def test_insert_conflict_keeps_existing_incidents(
    am_client, api_engine: AsyncEngine, monkeypatch, existing: bool
) -> None:
    """A concurrent first receive of the same alert wins the insert.

    Only an incident this delivery opened, and left empty, is dropped; an existing incident
    found for the group is kept with its members.
    """
    from lumewarp_control_plane.alerts import receiver as receiver_module

    starts = datetime.now(UTC) - timedelta(minutes=5)
    name = f"Conflict{int(existing)}"
    incoming = IncomingAlert(
        status="firing",
        labels={"alertname": name, "instance": "gw-race"},
        annotations={},
        starts_at=starts,
        ends_at=None,
        generator_url=None,
        fingerprint=f"conflict-{int(existing)}",
    )
    if existing:
        async with AsyncSession(api_engine) as session, session.begin():
            await receive(
                session,
                [
                    IncomingAlert(
                        status="firing",
                        labels={"alertname": name, "instance": "gw-first"},
                        annotations={},
                        starts_at=starts,
                        ends_at=None,
                        generator_url=None,
                        fingerprint=f"conflict-first-{int(existing)}",
                    )
                ],
            )
    original = receiver_module._find_or_open_incident
    winner = uuid.uuid4()

    async def racing(session, **kwargs):
        found = await original(session, **kwargs)
        # Another delivery inserts the same alert, attached elsewhere, before this upsert.
        session.add(
            Incident(
                id=winner,
                alertname=name,
                zone_id=None,
                group_key="winner",
                starts_at=starts,
                status="firing",
            )
        )
        await session.flush()
        session.add(
            Alert(
                incident_id=winner,
                fingerprint=incoming.fingerprint,
                starts_at=starts,
                status="firing",
                alertname=name,
                labels=incoming.labels,
                annotations={},
                first_received_at=starts,
                last_received_at=starts,
            )
        )
        await session.flush()
        return found

    monkeypatch.setattr(receiver_module, "_find_or_open_incident", racing)
    async with AsyncSession(api_engine) as session, session.begin():
        await receive(session, [incoming])
    async with AsyncSession(api_engine) as session:
        rows = (
            await session.execute(
                select(Incident.id, func.count(Alert.id))
                .outerjoin(Alert, Alert.incident_id == Incident.id)
                .where(Incident.alertname == name)
                .group_by(Incident.id)
            )
        ).all()
        attached = await session.scalar(
            select(Alert.incident_id).where(Alert.fingerprint == incoming.fingerprint)
        )
    assert attached == winner
    counts = {row[0]: row[1] for row in rows}
    assert counts.pop(winner) == 1
    # Without an existing incident the opened one was empty and is gone; with one, it stays.
    assert list(counts.values()) == ([1] if existing else [])


async def test_impact_and_search_filters(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    target = f"SearchTarget-{suffix}"
    await make_model(committed, target)
    await committed.commit()
    post(
        am_client,
        alert(
            labels={"alertname": f"VisibleAlert{suffix}", "lumewarp_status": "down"},
            annotations={"summary": f"Backend 50%_{suffix} broke"},
        ),
        alert(labels={"alertname": f"AdminAlert{suffix}"}),
        alert(labels={"alertname": f"ByTarget{suffix}", "model": target}),
    )

    def names(**params) -> set[str]:
        return {row["alertname"] for row in incidents(am_client, **params)}

    assert names(q=suffix, impact="user_visible") == {f"VisibleAlert{suffix}"}
    assert names(q=suffix, impact="admin_only") == {f"AdminAlert{suffix}", f"ByTarget{suffix}"}
    # Case-insensitive alertname, summary and target name; LIKE wildcards are literal.
    assert names(q=f"visiblealert{suffix}") == {f"VisibleAlert{suffix}"}
    assert names(q=f"50%_{suffix}") == {f"VisibleAlert{suffix}"}
    assert names(q=f"50xx{suffix}") == set()
    assert names(q=target.lower()) == {f"ByTarget{suffix}"}
    rows = incidents(am_client, q=f"VisibleAlert{suffix}")
    assert rows[0]["summary"] == f"Backend 50%_{suffix} broke"
    bad = am_client.get(f"{API}/incidents", params={"impact": "nope"}, headers=ADMIN_AUTH)
    assert bad.status_code == 422


async def test_affected_names_follow_the_targets(am_client, committed) -> None:
    suffix = uuid.uuid4().hex[:8]
    default_zone = am_client.app.state.settings.zone
    other_zone = f"aff-{suffix}"
    await zone(committed, other_zone)
    upstream = await make_upstream(committed, f"aff-up-{suffix}")
    model = await make_model(committed, f"aff-m-{suffix}", upstream)
    # Deployed in a second zone too, for the zone-less case.
    far = await make_upstream(committed, f"aff-far-{suffix}", zone=other_zone)
    committed.add(
        ModelDeployment(name_id=model.id, zone=other_zone, upstream_id=far.id, upstream_model="m")
    )
    other = await make_model(committed, f"aff-o-{suffix}")
    alias = await make_alias(committed, f"aff-a-{suffix}", model)
    pool = await make_router(committed, f"aff-r-{suffix}", [model, other])
    await committed.commit()

    def affected(labels: dict) -> set[tuple[str, str]]:
        alertname = f"Aff{uuid.uuid4().hex[:8]}"
        post(am_client, alert(labels={"alertname": alertname, **labels}))
        body = detail(am_client, incidents(am_client, alertname=alertname)[0]["id"])
        return {(item["zone"], item["name"]) for item in body["affected"]}

    in_default = {(default_zone, n) for n in (model.name, alias.name, pool.name)}
    assert affected({"upstream": upstream.name, "zone": default_zone}) == in_default
    assert affected({"model": model.name, "zone": default_zone}) == in_default
    # Zone-less: every live zone where the model is deployed.
    zoneless = affected({"model": model.name})
    assert in_default <= zoneless
    assert (other_zone, model.name) in zoneless
    # The router is callable there through its one deployed member.
    assert (other_zone, pool.name) in zoneless
    assert affected({"requested": alias.name, "zone": default_zone}) == {(default_zone, alias.name)}
    assert affected({"requested": pool.name, "zone": default_zone}) == {(default_zone, pool.name)}
    assert affected({"model": other.name, "zone": default_zone}) == {
        (default_zone, other.name),
        (default_zone, pool.name),
    }
    assert affected({"model": model.name, "zone": "no-such-zone"}) == set()


async def test_status_links_the_incident(am_client, committed) -> None:
    name = f"st-link-{uuid.uuid4().hex[:8]}"
    await make_model(committed, name)
    await committed.commit()
    starts = datetime.now(UTC) - timedelta(minutes=5)
    post(
        am_client,
        _gateway_alert("g1", starts, name, labels={"lumewarp_status": "down"}),
        _gateway_alert("g2", starts + timedelta(seconds=20), name,
                       labels={"lumewarp_status": "down"}),
    )  # fmt: skip
    incident = incidents(am_client, alertname="Grouped")[0]
    response = am_client.get(f"{API}/status", headers=ADMIN_AUTH)
    items = [item for item in response.json()["items"] if item["name"] == name]
    assert [item["incident_id"] for item in items] == [incident["id"]]

"""Review regressions: contracts, permissions, combined filters and SQL shape."""

import uuid

import pytest

from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.api.usage import _events_query
from lumewarp_control_plane.usage import ingest
from tests.api.helpers import auth, user_with_token
from tests.api.test_usage import END, START, query
from tests.factories import make_alias, make_key, make_member, make_model, make_router, make_team
from tests.usage.test_catalog_provenance import CREATED, key_for, wire


async def test_upstream_model_empty_mixed_and_single_contract(client, committed, api_scope):
    key = await key_for(committed)
    expected = {}
    for values in (("", ""), ("", "x"), ("x", "x")):
        model = await make_model(committed, f"m-{uuid.uuid4().hex}", created_at=CREATED)
        expected[str(model.id)] = (
            values[0] if len(set(values)) == 1 else None,
            sorted(set(values) - {""}),
        )
        assert (
            await ingest(
                committed,
                "gateway",
                [wire(key, model=model.name, upstream_model=value) for value in values],
                zone=api_scope,
            )
            == 2
        )
    await committed.commit()
    rows = query(client, group_by="model", team_id=str(key.team_id)).json()["items"]
    assert {
        row["model_id"]: (row["upstream_model"], row["upstream_models"]) for row in rows
    } == expected


@pytest.mark.parametrize(
    "group",
    ["day", "requested", "model", "upstream_model", "alias", "router", "team", "owner", "key", ""],
)
async def test_rejected_event_under_every_group(client, committed, api_scope, group):
    key = await key_for(committed)
    rejected = wire(
        key,
        requested="not-found",
        model="",
        status=404,
        outcome=usage_pb2.REQUEST_OUTCOME_REJECTED,
        usage_state=usage_pb2.USAGE_STATE_MISSING,
        cost_known=False,
        cost_nano_usd=0,
        prompt_tokens=0,
        completion_tokens=0,
    )
    assert await ingest(committed, "gateway", [rejected], zone=api_scope) == 1
    await committed.commit()
    response = query(client, group_by=group, team_id=str(key.team_id))
    assert response.status_code == 200, response.text
    (row,) = response.json()["items"]
    assert (row["requests"], row["cost_nano_usd"], row["unknown_cost_count"]) == (1, 0, 1)
    assert row["served_by"] == []
    if group in {"model", "alias", "router", "upstream_model"}:
        assert row[group] == ""
    if group == "requested":
        assert row["requested"] == "not-found"


async def test_routing_ids_and_missing_filters_are_refused_for_a_member(client, committed):
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await make_key(committed, team, user)
    await committed.commit()
    headers = auth(token)
    for group in ("model", "upstream_model", "alias", "router"):
        response = query(client, headers, group_by=group)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "routing_detail_forbidden"
    # Ids and the `_missing` filters name routing columns too: they are refused instead of
    # silently narrowing (and thereby revealing) the readable teams.
    for id_field in ("model_id", "alias_id", "router_id"):
        response = query(client, headers, group_by="requested", **{id_field: str(uuid.uuid4())})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "routing_detail_forbidden"
    for missing in ("alias_missing", "router_missing"):
        for value in ("true", "false"):
            response = query(client, headers, group_by="requested", **{missing: value})
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "routing_detail_forbidden"


@pytest.mark.parametrize("kind", ["alias", "router"])
async def test_name_and_missing_filters_are_anded(client, committed, api_scope, kind):
    key = await key_for(committed)
    model = await make_model(committed, f"m-{uuid.uuid4().hex}", created_at=CREATED)
    name = f"via-{uuid.uuid4().hex}"
    if kind == "alias":
        item = await make_alias(committed, name, model)
        item.created_at = CREATED
    else:
        await make_router(committed, name, created_at=CREATED)
    await committed.flush()
    assert (
        await ingest(
            committed, "gateway", [wire(key, model=model.name, **{kind: name})], zone=api_scope
        )
        == 1
    )
    await committed.commit()
    for missing, count in (("true", 0), ("false", 1)):
        rows = query(
            client,
            group_by=kind,
            team_id=str(key.team_id),
            **{kind: name, f"{kind}_missing": missing},
        ).json()["items"]
        assert sum(row["requests"] for row in rows) == count


def test_default_scope_queries_do_not_collect_catalog_refs():
    for groups in (["day", "requested"], ["team"], ["owner"], []):
        stmt, _, kinds, upstream = _events_query(groups, None, START, END, [])
        sql = str(stmt)
        assert "JOIN" not in sql and "DISTINCT" not in sql
        assert kinds == [] and not upstream
    stmt, _, kinds, upstream = _events_query(["alias"], None, START, END, [])
    assert kinds == ["model", "alias"] and not upstream
    assert "usage_reduced" in str(stmt)

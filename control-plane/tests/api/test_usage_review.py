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


async def test_upstream_model_empty_mixed_and_single_contract(client, committed):
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
async def test_rejected_event_under_every_group(client, committed, group):
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
    assert await ingest(committed, "gateway", [rejected]) == 1
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


async def test_ids_and_missing_filters_never_expand_member_scope(client, committed):
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    own = await make_key(committed, team, user)
    foreign = await key_for(committed)
    identities = []
    for key in (own, foreign):
        suffix = uuid.uuid4().hex
        model = await make_model(committed, f"m-{suffix}", created_at=CREATED)
        pool = await make_router(committed, f"r-{suffix}", created_at=CREATED)
        alias = await make_alias(committed, f"a-{suffix}", model)
        alias.created_at = CREATED
        await committed.flush()
        identities.append(
            {"model_id": str(model.id), "alias_id": str(alias.id), "router_id": str(pool.id)}
        )
        assert (
            await ingest(
                committed,
                "gateway",
                [
                    wire(key, model=model.name),
                    wire(key, model=model.name, alias=f"a-{suffix}", router=f"r-{suffix}"),
                ],
            )
            == 2
        )
    await committed.commit()
    headers = auth(token)
    for group in ("model", "alias", "router"):
        for id_field in ("model_id", "alias_id", "router_id"):
            visible = query(client, headers, group_by=group, **{id_field: identities[0][id_field]})
            assert visible.status_code == 200, visible.text
            assert sum(row["requests"] for row in visible.json()["items"]) == (
                2 if id_field == "model_id" else 1
            )
            hidden = query(client, headers, group_by=group, **{id_field: identities[1][id_field]})
            assert hidden.status_code == 200 and hidden.json()["items"] == []
        for missing in ("alias_missing", "router_missing"):
            for value in ("true", "false"):
                rows = query(client, headers, group_by=group, **{missing: value}).json()["items"]
                assert sum(row["requests"] for row in rows) == 1


@pytest.mark.parametrize("kind", ["alias", "router"])
async def test_name_and_missing_filters_are_anded(client, committed, kind):
    key = await key_for(committed)
    model = await make_model(committed, f"m-{uuid.uuid4().hex}", created_at=CREATED)
    name = f"via-{uuid.uuid4().hex}"
    if kind == "alias":
        item = await make_alias(committed, name, model)
        item.created_at = CREATED
    else:
        await make_router(committed, name, created_at=CREATED)
    await committed.flush()
    assert await ingest(committed, "gateway", [wire(key, model=model.name, **{kind: name})]) == 1
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

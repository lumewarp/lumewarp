"""Catalog API: one namespace, id-based references, rename and referential rules."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import (
    AuditLog,
    CatalogKind,
    CatalogNameHistory,
    PolicyGrant,
)
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token

H = ADMIN_AUTH
API = "/api/v1"


def _ok(resp, code: int = 201) -> dict:
    assert resp.status_code == code, resp.text
    return resp.json()


def _422(resp, needle: str) -> None:
    assert resp.status_code == 422, resp.text
    assert needle in resp.json()["error"]["message"], resp.json()


def _name(prefix: str = "n") -> str:
    return f"{prefix}-{suffix()}"


def _upstream(client) -> str:
    body = {"name": _name("up"), "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"}
    return _ok(client.post(f"{API}/upstreams", json=body, headers=H))["id"]


def _model(client, name: str | None = None) -> dict:
    body = {
        "name": name or _name("model"),
        "upstream_id": _upstream(client),
        "upstream_model": "real-name",
    }
    return _ok(client.post(f"{API}/models", json=body, headers=H))


def _router_raw(client, members: list[str], name: str | None = None, **body):
    body = {
        "name": name or _name("router"),
        "members": [{"model_id": m} for m in members],
        **body,
    }
    return client.post(f"{API}/routers", json=body, headers=H)


def _router(client, members: list[str], name: str | None = None, **body) -> dict:
    return _ok(_router_raw(client, members, name, **body))


def _alias(client, target: str, name: str | None = None) -> dict:
    body = {"name": name or _name("alias"), "target": target}
    return _ok(client.post(f"{API}/aliases", json=body, headers=H))


def _team(client, *names: str, access: str = "list") -> dict:
    body = {"name": _name("team"), "model_access": access, "allowed_models": list(names)}
    if access != "list":
        del body["allowed_models"]
    return _ok(client.post(f"{API}/teams", json=body, headers=H))


def _account(client, team: dict, *names: str):
    body = {"name": _name("sa"), "model_access": "list", "allowed_models": list(names)}
    return client.post(f"{API}/teams/{team['id']}/service-accounts", json=body, headers=H)


def _stored_grants(client, team: dict, account: dict) -> list[str]:
    path = f"{API}/teams/{team['id']}/service-accounts?limit=500"
    items = _ok(client.get(path, headers=H), 200)["items"]
    return next(item for item in items if item["id"] == account["id"])["allowed_models"]


# --------------------------------------------------------------------------- namespace


@pytest.mark.parametrize("taken_by", ["model", "alias", "router"])
@pytest.mark.parametrize("creating", ["model", "alias", "router"])
def test_name_is_unique_across_kinds(client, taken_by, creating) -> None:
    model = _model(client)
    name = _name("shared")
    if taken_by == "model":
        _model(client, name)
    elif taken_by == "alias":
        _alias(client, model["id"], name)
    else:
        _router(client, [model["id"]], name)
    if creating == "model":
        body = {"name": name, "upstream_id": model["upstream_id"], "upstream_model": "m"}
        resp = client.post(f"{API}/models", json=body, headers=H)
    elif creating == "alias":
        resp = client.post(f"{API}/aliases", json={"name": name, "target": model["id"]}, headers=H)
    else:
        resp = _router_raw(client, [model["id"]], name)
    _422(resp, f"already used by a {taken_by}")


def test_rename_cannot_take_a_name_of_another_kind(client) -> None:
    model, other = _model(client), _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, model["id"])
    for path, row in (("models", other), ("routers", router), ("aliases", alias)):
        resp = client.patch(f"{API}/{path}/{row['id']}", json={"name": model["name"]}, headers=H)
        _422(resp, "already used")


def test_reserved_name_is_refused(client) -> None:
    model = _model(client)
    _422(_router_raw(client, [model["id"]], "unknown"), "reserved")


def test_catalog_lists_every_name_with_its_kind(client) -> None:
    model = _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, router["id"])
    items = _ok(client.get(f"{API}/catalog?limit=500", headers=H), 200)["items"]
    kinds = {item["id"]: (item["name"], item["kind"]) for item in items}
    assert kinds[model["id"]] == (model["name"], "model")
    assert kinds[router["id"]] == (router["name"], "router")
    assert kinds[alias["id"]] == (alias["name"], "alias")


async def test_catalog_reads_and_writes_need_their_actions(client, committed) -> None:
    _, secret = await user_with_token(committed)
    await committed.commit()
    plain = auth(secret)
    model = _model(client)
    alias = _alias(client, model["id"])
    assert client.get(f"{API}/catalog", headers=plain).status_code == 200
    assert client.get(f"{API}/aliases/{alias['id']}", headers=plain).status_code == 200
    writes = (
        client.post(f"{API}/aliases", json={"name": _name(), "target": model["id"]}, headers=plain),
        client.patch(f"{API}/aliases/{alias['id']}", json={"name": _name()}, headers=plain),
        client.delete(f"{API}/aliases/{alias['id']}", headers=plain),
    )
    assert [w.status_code for w in writes] == [403, 403, 403]


# --------------------------------------------------------------------------- references


def test_router_without_fallback_is_creatable(client) -> None:
    router = _router(client, [_model(client)["id"]])
    assert router["fallback_model_ids"] == []
    assert router["retry"] is None


def test_model_body_rejects_weight(client) -> None:
    body = {
        "name": _name(),
        "upstream_id": _upstream(client),
        "upstream_model": "m",
        "weight": 2,
    }
    assert client.post(f"{API}/models", json=body, headers=H).status_code == 422


def test_alias_cannot_point_at_alias(client) -> None:
    model = _model(client)
    alias = _alias(client, model["id"])
    body = {"name": _name(), "target": alias["id"]}
    _422(client.post(f"{API}/aliases", json=body, headers=H), "another alias")
    other = _alias(client, model["id"])
    resp = client.patch(f"{API}/aliases/{other['id']}", json={"target": alias["id"]}, headers=H)
    _422(resp, "another alias")
    body = {"name": _name(), "target": str(uuid.uuid4())}
    _422(client.post(f"{API}/aliases", json=body, headers=H), "unknown")


def test_alias_can_point_at_a_router_and_be_repointed(client) -> None:
    model = _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, router["id"])
    assert (alias["target"], alias["target_kind"], alias["target_name"]) == (
        router["id"],
        "router",
        router["name"],
    )
    moved = _ok(
        client.patch(f"{API}/aliases/{alias['id']}", json={"target": model["id"]}, headers=H), 200
    )
    assert (moved["target_kind"], moved["target_name"]) == ("model", model["name"])


def test_member_and_fallback_must_be_models(client) -> None:
    model = _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, model["id"])
    for bad in (router["id"], alias["id"], str(uuid.uuid4())):
        _422(_router_raw(client, [bad]), "not models")
        resp = _router_raw(client, [model["id"]], fallback_model_ids=[bad], retry={})
        _422(resp, "not models")
        split = {"name": "a", "weight": 1, "members": [{"model_id": bad}]}
        resp = client.post(
            f"{API}/routers",
            json={"name": _name(), "traffic_split": [split]},
            headers=H,
        )
        _422(resp, "not models")


def test_upstream_with_models_cannot_be_deleted(client) -> None:
    model = _model(client)
    resp = client.delete(f"{API}/upstreams/{model['upstream_id']}", headers=H)
    assert resp.status_code == 409


# --------------------------------------------------------------------------- delete


def test_delete_referenced_name_is_409_listing_referrers(client) -> None:
    model = _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, model["id"])
    fallback_user = _router(client, [_model(client)["id"]])
    _ok(
        client.patch(
            f"{API}/routers/{fallback_user['id']}",
            json={"fallback_model_ids": [model["id"]], "retry": {}},
            headers=H,
        ),
        200,
    )
    resp = client.delete(f"{API}/models/{model['id']}", headers=H)
    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert f"alias {alias['name']}" in message
    assert f"router {router['name']}" in message
    assert f"router {fallback_user['name']} (fallback)" in message

    # A router targeted by an alias is protected too, and freed with the alias.
    router_alias = _alias(client, router["id"])
    resp = client.delete(f"{API}/routers/{router['id']}", headers=H)
    assert resp.status_code == 409
    assert f"alias {router_alias['name']}" in resp.json()["error"]["message"]
    assert client.delete(f"{API}/aliases/{router_alias['id']}", headers=H).status_code == 204
    assert client.delete(f"{API}/routers/{router['id']}", headers=H).status_code == 204


async def test_delete_removes_and_audits_grants_and_frees_the_name(client, committed) -> None:
    model = _model(client)
    team = _team(client, model["name"])
    account = _ok(_account(client, team, model["name"]))
    resp = client.delete(f"{API}/models/{model['id']}", headers=H)
    assert resp.status_code == 204, resp.text

    after = _ok(client.get(f"{API}/teams/{team['id']}", headers=H), 200)
    assert after["allowed_models"] == []
    assert _stored_grants(client, team, account) == []
    grants = await committed.scalars(
        select(PolicyGrant).where(PolicyGrant.name_id == uuid.UUID(model["id"]))
    )
    assert grants.all() == []
    rows = (
        await committed.scalars(select(AuditLog).where(AuditLog.action == "grant.delete"))
    ).all()
    mine = [r for r in rows if r.details["name_id"] == model["id"]]
    assert len(mine) == 2
    assert {str(r.team_id) for r in mine} == {team["id"]}
    assert {r.details["service_account_id"] for r in mine} == {None, account["id"]}
    assert client.get(f"{API}/models/{model['id']}", headers=H).status_code == 404
    # The freed name can go to any kind.
    assert _alias(client, _model(client)["id"], model["name"])["name"] == model["name"]


# --------------------------------------------------------------------------- rename


@pytest.mark.parametrize("kind", ["models", "routers", "aliases"])
async def test_rename_keeps_identity_grants_and_writes_history_and_audit(
    client, committed, kind
) -> None:
    model = _model(client)
    if kind == "models":
        row = model
    elif kind == "routers":
        row = _router(client, [model["id"]])
    else:
        row = _alias(client, model["id"])
    old, new = row["name"], _name("renamed")
    team = _team(client, old)
    renamed = _ok(client.patch(f"{API}/{kind}/{row['id']}", json={"name": new}, headers=H), 200)
    assert (renamed["id"], renamed["name"]) == (row["id"], new)
    # Grants follow the id; the API expands the current name.
    assert _ok(client.get(f"{API}/teams/{team['id']}", headers=H), 200)["allowed_models"] == [new]
    single = {"models": "model", "routers": "router", "aliases": "alias"}[kind]
    history = (
        await committed.execute(
            select(CatalogNameHistory.old_name, CatalogNameHistory.kind).where(
                CatalogNameHistory.name_id == uuid.UUID(row["id"])
            )
        )
    ).all()
    assert history == [(old, CatalogKind(single))]
    audit = await committed.scalar(
        select(AuditLog)
        .where(AuditLog.action == f"{single}.rename")
        .where(AuditLog.resource_id == row["id"])
    )
    assert audit.details == {"old_name": old, "new_name": new}
    # The old name may be reused by another item, which does not inherit anything.
    other = _model(client, old)
    assert other["id"] != row["id"]
    assert _ok(client.get(f"{API}/teams/{team['id']}", headers=H), 200)["allowed_models"] == [new]


def test_rename_model_keeps_router_alias_and_fallback_references(client) -> None:
    model = _model(client)
    router = _router(client, [model["id"]])
    alias = _alias(client, model["id"])
    user = _router(
        client, [_model(client)["id"]], fallback_model_ids=[model["id"]], retry={"max_attempts": 2}
    )
    new = _name("renamed")
    _ok(client.patch(f"{API}/models/{model['id']}", json={"name": new}, headers=H), 200)
    assert (
        _ok(client.get(f"{API}/routers/{router['id']}", headers=H), 200)["members"][0]["model_id"]
        == model["id"]
    )
    assert _ok(client.get(f"{API}/aliases/{alias['id']}", headers=H), 200)["target_name"] == new
    assert _ok(client.get(f"{API}/routers/{user['id']}", headers=H), 200)["fallback_model_ids"] == [
        model["id"]
    ]


async def test_keep_old_name_as_alias_carries_grants(client, committed) -> None:
    model = _model(client)
    old, new = model["name"], _name("renamed")
    team = _team(client, old)
    account = _ok(_account(client, team, old))
    renamed = _ok(
        client.patch(
            f"{API}/models/{model['id']}",
            json={"name": new, "keep_old_name_as_alias": True},
            headers=H,
        ),
        200,
    )
    assert renamed["name"] == new
    catalog = _ok(client.get(f"{API}/catalog?limit=500", headers=H), 200)["items"]
    entry = next(item for item in catalog if item["name"] == old)
    assert entry["kind"] == "alias"
    alias = _ok(client.get(f"{API}/aliases/{entry['id']}", headers=H), 200)
    assert (alias["target"], alias["target_name"]) == (model["id"], new)
    assert sorted(_stored_grants(client, team, account)) == sorted([old, new])
    row = await committed.scalar(
        select(AuditLog)
        .where(AuditLog.action == "alias.create")
        .where(AuditLog.resource_id == entry["id"])
    )
    assert row.details["grants_copied"] == 2
    # The API never does this implicitly.
    other = _model(client)
    _ok(client.patch(f"{API}/models/{other['id']}", json={"name": _name()}, headers=H), 200)
    names = {
        item["name"]
        for item in _ok(client.get(f"{API}/catalog?limit=500", headers=H), 200)["items"]
    }
    assert other["name"] not in names


def test_keep_old_name_needs_a_rename_and_only_models_have_it(client) -> None:
    model = _model(client)
    resp = client.patch(
        f"{API}/models/{model['id']}", json={"keep_old_name_as_alias": True}, headers=H
    )
    _422(resp, "needs a new name")
    router = _router(client, [model["id"]])
    resp = client.patch(
        f"{API}/routers/{router['id']}",
        json={"name": _name(), "keep_old_name_as_alias": True},
        headers=H,
    )
    assert resp.status_code == 422


async def test_alias_crud_is_audited(client, committed) -> None:
    model = _model(client)
    alias = _alias(client, model["id"])
    _ok(client.patch(f"{API}/aliases/{alias['id']}", json={"target": model["id"]}, headers=H), 200)
    assert client.delete(f"{API}/aliases/{alias['id']}", headers=H).status_code == 204
    rows = await committed.scalars(
        select(AuditLog.action).where(AuditLog.resource_id == alias["id"])
    )
    assert sorted(rows) == ["alias.create", "alias.delete", "alias.update"]


# --------------------------------------------------------------------------- permissions


def test_allowed_names_must_exist_and_stay_in_team_scope(client) -> None:
    inside, outside = _model(client), _model(client)
    body = {"name": _name(), "model_access": "list", "allowed_models": ["no-such-name"]}
    _422(client.post(f"{API}/teams", json=body, headers=H), "unknown names")

    team = _team(client, inside["name"])
    _422(_account(client, team, outside["name"]), "allowed_models")
    _422(_account(client, team, "no-such-name"), "unknown names")
    _ok(_account(client, team, inside["name"]))


async def test_member_grants_outside_team_are_422(client, committed) -> None:
    user, _ = await user_with_token(committed)
    await committed.commit()
    inside, outside = _model(client), _model(client)
    team = _team(client, inside["name"])
    path = f"{API}/teams/{team['id']}/members/{user.id}"
    body = {"role": "member", "model_access": "list"}
    _422(
        client.put(path, json={**body, "allowed_models": [outside["name"]]}, headers=H),
        "allowed_models",
    )
    created = _ok(client.put(path, json={**body, "allowed_models": [inside["name"]]}, headers=H))
    assert created["allowed_models"] == [inside["name"]]


def test_team_all_covers_future_names_for_owners(client) -> None:
    team = _team(client, access="all")
    later = _model(client)
    account = _ok(_account(client, team, later["name"]))
    assert account["allowed_models"] == [later["name"]]


async def test_snapshot_expands_grants_to_current_names(client, committed, api_settings) -> None:
    model = _model(client)
    alias = _alias(client, model["id"])
    team = _team(client, alias["name"], model["name"])
    account = _ok(_account(client, team, alias["name"]))
    key = _ok(
        client.post(
            f"{API}/teams/{team['id']}/keys",
            json={"name": "k", "service_account_id": account["id"]},
            headers=H,
        )
    )["key"]
    new = _name("renamed-alias")
    _ok(client.patch(f"{API}/aliases/{alias['id']}", json={"name": new}, headers=H), 200)
    compiled = await compile_scope(committed, api_settings.scope, require_local_membership=True)
    wire = next(k for k in compiled.snapshot.api_keys if k.key_id == key["id"])
    # Permission is by called name: only the alias was granted to the owner.
    assert list(wire.policy.allowed_models) == [new]
    assert not wire.policy.allow_all_models

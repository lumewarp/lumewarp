"""Key-level model narrowing: `model_access` inherit/list on keys, checked against the
owner's effective set and compiled as org ∩ team ∩ owner ∩ key.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import AuditLog, ModelAccess, TeamRole
from tests.api.helpers import (
    ADMIN_AUTH,
    auth,
    publish_zones,
    set_playground_url,
    suffix,
    user_with_token,
)
from tests.factories import (
    grant,
    make_member,
    make_model,
    make_org,
    make_service_account,
    make_team,
)


async def compiled_key(api_engine, key_id: str):
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_zone(session, "api-test", require_local_membership=True)
    return next(k for k in compiled.snapshot.api_keys if k.key_id == key_id)


async def _models(committed, count: int) -> list[str]:
    return [(await make_model(committed, f"km-{suffix()}")).name for _ in range(count)]


async def test_self_service_key_narrows_the_owner(client, committed, api_engine) -> None:
    a, b, c, d = await _models(committed, 4)
    org = await make_org(committed, model_access=ModelAccess.LIST)
    await grant(committed, org, [a, b, c])
    team = await make_team(
        committed, org=org, model_access=ModelAccess.LIST, allowed_models=[a, b, c, d]
    )
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, model_access=ModelAccess.LIST, allowed_models=[a, b])
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"

    outside = client.post(
        path,
        json={"name": "k", "model_access": "list", "allowed_models": [a, c]},
        headers=auth(token),
    )
    assert outside.status_code == 422, outside.text
    assert c in outside.text
    unknown = client.post(
        path,
        json={"name": "k", "model_access": "list", "allowed_models": ["no-such-model"]},
        headers=auth(token),
    )
    assert unknown.status_code == 422, unknown.text
    widen = client.post(path, json={"name": "k", "model_access": "all"}, headers=auth(token))
    assert widen.status_code == 422, widen.text
    no_list = client.post(path, json={"name": "k", "allowed_models": [a]}, headers=auth(token))
    assert no_list.status_code == 422, no_list.text

    created = client.post(
        path,
        json={"name": "k", "model_access": "list", "allowed_models": [a]},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert (key["model_access"], key["allowed_models"], key["granted_models"]) == (
        "list",
        [a],
        [a],
    )
    assert key["owner_models"] == sorted([a, b])
    published = await compiled_key(api_engine, key["id"])
    assert (published.policy.allow_all_models, list(published.policy.allowed_models)) == (
        False,
        [a],
    )

    own = client.get("/api/v1/users/me/keys?limit=500", headers=auth(token)).json()["items"]
    listed = next(row for row in own if row["id"] == key["id"])
    assert listed["allowed_models"] == [a] and listed["owner_models"] == sorted([a, b])

    # PATCH widens within the owner, refuses beyond it, and `inherit` clears the list.
    patched = client.patch(
        f"/api/v1/keys/{key['id']}", json={"allowed_models": [a, b]}, headers=auth(token)
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["allowed_models"] == sorted([a, b])
    refused = client.patch(
        f"/api/v1/keys/{key['id']}", json={"allowed_models": [d]}, headers=auth(token)
    )
    assert refused.status_code == 422, refused.text
    inherit = client.patch(
        f"/api/v1/keys/{key['id']}", json={"model_access": "inherit"}, headers=auth(token)
    )
    assert inherit.status_code == 200, inherit.text
    assert (inherit.json()["model_access"], inherit.json()["allowed_models"]) == ("inherit", [])
    published = await compiled_key(api_engine, key["id"])
    assert list(published.policy.allowed_models) == sorted([a, b])


async def test_admin_key_for_a_service_account_with_a_group(client, committed, api_engine) -> None:
    a, b, c = await _models(committed, 3)
    team = await make_team(committed, model_access=ModelAccess.LIST, allowed_models=[a, b, c])
    account = await make_service_account(
        committed, team, model_access=ModelAccess.LIST, allowed_models=[a, b]
    )
    await committed.commit()
    inside = client.post(
        "/api/v1/model-groups",
        json={"name": f"kg-{suffix()}", "members": [b]},
        headers=ADMIN_AUTH,
    ).json()
    outside = client.post(
        "/api/v1/model-groups",
        json={"name": f"kg-{suffix()}", "members": [b, c]},
        headers=ADMIN_AUTH,
    ).json()
    path = f"/api/v1/teams/{team.id}/keys"

    refused = client.post(
        path,
        json={
            "name": "sa",
            "service_account_id": str(account.id),
            "model_access": "list",
            "allowed_groups": [outside["name"]],
        },
        headers=ADMIN_AUTH,
    )
    assert refused.status_code == 422, refused.text
    unknown = client.post(
        path,
        json={
            "name": "sa",
            "service_account_id": str(account.id),
            "model_access": "list",
            "allowed_groups": ["no-such-group"],
        },
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422, unknown.text
    created = client.post(
        path,
        json={
            "name": "sa",
            "service_account_id": str(account.id),
            "model_access": "list",
            "allowed_models": [a],
            "allowed_groups": [inside["name"]],
        },
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key["allowed_groups"] == [inside["name"]]
    assert key["granted_models"] == sorted([a, b])
    assert key["owner_models"] == sorted([a, b])
    published = await compiled_key(api_engine, key["id"])
    assert list(published.policy.allowed_models) == sorted([a, b])

    # The team list shows the same fields.
    items = client.get(path + "?limit=500", headers=ADMIN_AUTH).json()["items"]
    assert next(row for row in items if row["id"] == key["id"])["granted_models"] == sorted([a, b])
    audit = await committed.scalar(
        select(AuditLog).where(AuditLog.action == "key.create", AuditLog.resource_id == key["id"])
    )
    assert audit.details["allowed_groups"] == [inside["name"]]


async def test_compiled_key_is_the_intersection_of_every_level(
    client, committed, api_engine
) -> None:
    a, b, c, d = await _models(committed, 4)
    org = await make_org(committed, model_access=ModelAccess.LIST)
    await grant(committed, org, [a, b, c])
    team = await make_team(
        committed, org=org, model_access=ModelAccess.LIST, allowed_models=[a, b, c, d]
    )
    user, token = await user_with_token(committed)
    await make_member(
        committed, team, user, model_access=ModelAccess.LIST, allowed_models=[a, b, d]
    )
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": "k", "model_access": "list", "allowed_models": [a, b]},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    # org {a,b,c} ∩ team {a,b,c,d} ∩ owner {a,b,d} ∩ key {a,b}
    published = await compiled_key(api_engine, key["id"])
    assert list(published.policy.allowed_models) == sorted([a, b])

    # The owner shrinks: the key's unchanged list is not rechecked, and still compiles
    # as the intersection.
    narrowed = client.patch(
        f"/api/v1/teams/{team.id}/members/{user.id}",
        json={"allowed_models": [a]},
        headers=ADMIN_AUTH,
    )
    assert narrowed.status_code == 200, narrowed.text
    renamed = client.patch(
        f"/api/v1/keys/{key['id']}", json={"name": "renamed"}, headers=auth(token)
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["allowed_models"] == sorted([a, b])
    assert renamed.json()["owner_models"] == [a]
    published = await compiled_key(api_engine, key["id"])
    assert list(published.policy.allowed_models) == [a]


async def test_limits_only_patch_is_unchanged(client, committed) -> None:
    (a,) = await _models(committed, 1)
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole.MEMBER)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": "k", "model_access": "list", "allowed_models": [a]},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key["owner_models"] is None

    patched = client.patch(f"/api/v1/keys/{key['id']}", json={"rpm_limit": 5}, headers=auth(token))
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["rpm_limit"] == 5
    assert (body["model_access"], body["allowed_models"], body["lifecycle"]) == (
        "list",
        [a],
        "active",
    )
    null = client.patch(
        f"/api/v1/keys/{key['id']}", json={"allowed_models": None}, headers=auth(token)
    )
    assert null.status_code == 422, null.text


async def test_playground_offers_only_the_key_list(client, committed, api_engine) -> None:
    from lumewarp_control_plane.auth import sessions

    a, b = await _models(committed, 2)
    team = await make_team(committed, model_access=ModelAccess.LIST, allowed_models=[a, b])
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    cookie = await sessions.create_session(committed, user.id, 3600)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": "k", "model_access": "list", "allowed_models": [a]},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]
    await set_playground_url(client, committed, "http://gateway:4000")
    await publish_zones(api_engine)
    body = client.get("/api/v1/playground", headers=auth("session:" + cookie)).json()
    assert next(k for k in body["keys"] if k["id"] == key_id)["models"] == [a]


async def test_group_and_catalog_names_share_one_namespace(client, committed) -> None:
    model = await make_model(committed, f"km-{suffix()}")
    await committed.commit()
    clash = client.post(
        "/api/v1/model-groups", json={"name": model.name, "members": []}, headers=ADMIN_AUTH
    )
    assert clash.status_code == 409, clash.text
    group = f"km-group-{suffix()}"
    created = client.post(
        "/api/v1/model-groups", json={"name": group, "members": [model.name]}, headers=ADMIN_AUTH
    )
    assert created.status_code == 201, created.text
    alias = client.post(
        "/api/v1/aliases", json={"name": group, "target": str(model.id)}, headers=ADMIN_AUTH
    )
    assert alias.status_code == 422, alias.text
    assert "model group" in alias.text
    rename = client.patch(f"/api/v1/models/{model.id}", json={"name": group}, headers=ADMIN_AUTH)
    assert rename.status_code == 422, rename.text


async def test_old_name_alias_keeps_group_membership(client, committed) -> None:
    model = await make_model(committed, f"km-{suffix()}")
    await committed.commit()
    old, new = model.name, f"km-{suffix()}"
    created = client.post(
        "/api/v1/model-groups",
        json={"name": f"km-group-{suffix()}", "members": [old]},
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    renamed = client.patch(
        f"/api/v1/models/{model.id}",
        json={"name": new, "keep_old_name_as_alias": True},
        headers=ADMIN_AUTH,
    )
    assert renamed.status_code == 200, renamed.text
    group = client.get(f"/api/v1/model-groups/{created.json()['id']}", headers=ADMIN_AUTH)
    assert sorted(m["name"] for m in group.json()["members"]) == sorted([old, new])


async def test_edit_dialog_patch_with_name_and_limits_changes_models(client, committed) -> None:
    """The edit dialog sends the name and limits with the model list. The rename flushes the
    row first, which expires its grant columns; reading them must not lazy-load (was 500).
    """
    a, b = await _models(committed, 2)
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    account = await make_service_account(committed, team)
    await committed.commit()
    group = client.post(
        "/api/v1/model-groups", json={"name": f"kg-{suffix()}", "members": [b]}, headers=ADMIN_AUTH
    ).json()
    owners = (({}, auth(token)), ({"service_account_id": str(account.id)}, ADMIN_AUTH))
    for owner, headers in owners:
        created = client.post(
            f"/api/v1/teams/{team.id}/keys",
            json={"name": "k", "rpm_limit": 5, **owner},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        key_id = created.json()["key"]["id"]
        for models in (
            {"model_access": "list", "allowed_models": [a], "allowed_groups": [group["name"]]},
            {"model_access": "inherit"},
        ):
            response = client.patch(
                f"/api/v1/keys/{key_id}",
                json={"name": f"k-{suffix()}", "rpm_limit": 3, "tpm_limit": None, **models},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["model_access"] == models["model_access"]
        assert response.json()["rpm_limit"] == 3

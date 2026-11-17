"""Model groups: CRUD, permissions, expansion in the compiled snapshot, cascades."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import AuditLog, ConfigOutbox, PolicyGroupGrant, Team
from tests.api.helpers import ADMIN_AUTH, auth, create_org, suffix, user_with_token
from tests.factories import make_alias, make_key, make_member, make_model, make_user


def create_group(client, members: list[str], name: str | None = None) -> dict:
    response = client.post(
        "/api/v1/model-groups",
        json={"name": name or f"group-{suffix()}", "description": "d", "members": members},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_team(client, org_id: str, **policy) -> dict:
    response = client.post(
        "/api/v1/teams",
        json={"name": f"team-{suffix()}", "org_id": org_id, **policy},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def compiled_key(api_engine, key_id: str):
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_scope(session, "api-test", require_local_membership=True)
    return next(k for k in compiled.snapshot.api_keys if k.key_id == key_id)


async def test_crud(client, committed) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    b = await make_model(committed, f"m-{suffix()}")
    alias = await make_alias(committed, f"al-{suffix()}", a)
    await committed.commit()

    group = create_group(client, [b.name, alias.name, a.name])
    assert [m["name"] for m in group["members"]] == sorted([a.name, b.name, alias.name])
    assert {m["kind"] for m in group["members"]} == {"model", "alias"}
    assert group["grants"] == 0
    duplicate = client.post(
        "/api/v1/model-groups", json={"name": group["name"]}, headers=ADMIN_AUTH
    )
    assert duplicate.status_code == 409, duplicate.text
    unknown = client.post(
        "/api/v1/model-groups",
        json={"name": f"group-{suffix()}", "members": ["no-such-name"]},
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422, unknown.text

    path = f"/api/v1/model-groups/{group['id']}"
    got = client.get(path, headers=ADMIN_AUTH)
    assert got.status_code == 200 and got.json()["name"] == group["name"]
    renamed = f"group-{suffix()}"
    updated = client.patch(path, json={"name": renamed, "members": [a.name]}, headers=ADMIN_AUTH)
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == renamed
    assert [m["name"] for m in updated.json()["members"]] == [a.name]
    null = client.patch(path, json={"members": None}, headers=ADMIN_AUTH)
    assert null.status_code == 422, null.text

    listed = client.get("/api/v1/model-groups?limit=500", headers=ADMIN_AUTH)
    assert listed.status_code == 200
    assert group["id"] in {row["id"] for row in listed.json()["items"]}

    deleted = client.delete(path, headers=ADMIN_AUTH)
    assert deleted.status_code == 204, deleted.text
    assert client.get(path, headers=ADMIN_AUTH).status_code == 404
    actions = set(
        await committed.scalars(select(AuditLog.action).where(AuditLog.resource_id == group["id"]))
    )
    assert {"model_group.create", "model_group.update", "model_group.delete"} <= actions


async def test_writes_need_an_installation_admin_and_reads_any_user(client, committed) -> None:
    model = await make_model(committed, f"m-{suffix()}")
    _, token = await user_with_token(committed)
    await committed.commit()
    group = create_group(client, [model.name])
    path = f"/api/v1/model-groups/{group['id']}"

    assert client.get("/api/v1/model-groups", headers=auth(token)).status_code == 200
    assert client.get(path, headers=auth(token)).status_code == 200
    created = client.post(
        "/api/v1/model-groups", json={"name": f"group-{suffix()}"}, headers=auth(token)
    )
    assert created.status_code == 403, created.text
    assert client.patch(path, json={"description": "x"}, headers=auth(token)).status_code == 403
    assert client.delete(path, headers=auth(token)).status_code == 403


async def test_team_group_expands_in_the_snapshot_and_follows_member_edits(
    client, committed, api_engine
) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    b = await make_model(committed, f"m-{suffix()}")
    c = await make_model(committed, f"m-{suffix()}")
    user, token = await user_with_token(committed)
    await committed.commit()
    group = create_group(client, [a.name, b.name])
    org = create_org(client)
    team = create_team(
        client, org["id"], model_access="list", allowed_models=[c.name],
        allowed_groups=[group["name"]],
    )  # fmt: skip
    assert team["allowed_groups"] == [group["name"]]
    assert team["allowed_models"] == [c.name]
    assert team["granted_models"] == sorted([a.name, b.name, c.name])
    assert team["effective_models"] == sorted([a.name, b.name, c.name])

    team_row = await committed.get(Team, team["id"])
    await make_member(committed, team_row, user)
    key = await make_key(committed, team_row, user)
    await committed.commit()

    published = await compiled_key(api_engine, str(key.id))
    assert published.policy.allow_all_models is False
    assert list(published.policy.allowed_models) == sorted([a.name, b.name, c.name])

    # The member sees the expanded set too.
    me = client.get("/api/v1/users/me", headers=auth(token)).json()
    row = next(r for r in me["teams"] if r["team_id"] == team["id"])
    assert row["models"] == sorted([a.name, b.name, c.name])

    outbox = select(ConfigOutbox.id).where(ConfigOutbox.reason == "api.model_group.update")
    before = set(await committed.scalars(outbox))
    edited = client.patch(
        f"/api/v1/model-groups/{group['id']}", json={"members": [a.name]}, headers=ADMIN_AUTH
    )
    assert edited.status_code == 200, edited.text
    assert set(await committed.scalars(outbox)) - before
    published = await compiled_key(api_engine, str(key.id))
    assert list(published.policy.allowed_models) == sorted([a.name, c.name])


async def test_member_edit_beyond_a_parent_is_clamped_not_refused(
    client, committed, api_engine
) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    b = await make_model(committed, f"m-{suffix()}")
    user = await make_user(committed)
    await committed.commit()
    group = create_group(client, [a.name])
    org = create_org(client, model_access="list", allowed_models=[a.name])
    team = create_team(client, org["id"], model_access="list", allowed_groups=[group["name"]])
    team_row = await committed.get(Team, team["id"])
    await make_member(committed, team_row, user)
    key = await make_key(committed, team_row, user)
    await committed.commit()

    widened = client.patch(
        f"/api/v1/model-groups/{group['id']}",
        json={"members": [a.name, b.name]},
        headers=ADMIN_AUTH,
    )
    assert widened.status_code == 200, widened.text
    published = await compiled_key(api_engine, str(key.id))
    assert list(published.policy.allowed_models) == [a.name]
    got = client.get(f"/api/v1/teams/{team['id']}", headers=ADMIN_AUTH).json()
    assert got["effective_models"] == [a.name]


async def test_narrowing_refuses_a_group_reaching_outside_the_parent(client, committed) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    b = await make_model(committed, f"m-{suffix()}")
    await committed.commit()
    inside = create_group(client, [a.name])
    outside = create_group(client, [a.name, b.name])
    org = create_org(client, model_access="list", allowed_models=[a.name])
    assert org["effective_models"] == [a.name]

    refused = client.post(
        "/api/v1/teams",
        json={
            "name": f"team-{suffix()}",
            "org_id": org["id"],
            "model_access": "list",
            "allowed_groups": [outside["name"]],
        },
        headers=ADMIN_AUTH,
    )
    assert refused.status_code == 422, refused.text
    assert b.name in refused.text
    team = create_team(client, org["id"], model_access="list", allowed_groups=[inside["name"]])
    patched = client.patch(
        f"/api/v1/teams/{team['id']}",
        json={"allowed_groups": [outside["name"]]},
        headers=ADMIN_AUTH,
    )
    assert patched.status_code == 422, patched.text
    unknown = client.patch(
        f"/api/v1/teams/{team['id']}",
        json={"allowed_groups": ["no-such-group"]},
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422, unknown.text
    not_list = client.patch(
        f"/api/v1/teams/{team['id']}",
        json={"model_access": "all", "allowed_groups": [inside["name"]]},
        headers=ADMIN_AUTH,
    )
    assert not_list.status_code == 422, not_list.text


async def test_delete_cascades_grants_and_audits_them(client, committed) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    await committed.commit()
    group = create_group(client, [a.name])
    org = create_org(client, model_access="list", allowed_groups=[group["name"]])
    team = create_team(client, org["id"], model_access="list", allowed_groups=[group["name"]])
    assert (
        client.get(f"/api/v1/model-groups/{group['id']}", headers=ADMIN_AUTH).json()["grants"] == 2
    )

    deleted = client.delete(f"/api/v1/model-groups/{group['id']}", headers=ADMIN_AUTH)
    assert deleted.status_code == 204, deleted.text
    remaining = await committed.scalar(
        select(PolicyGroupGrant.id).where(PolicyGroupGrant.group_id == group["id"])
    )
    assert remaining is None
    got = client.get(f"/api/v1/teams/{team['id']}", headers=ADMIN_AUTH).json()
    assert got["allowed_groups"] == [] and got["effective_models"] == []
    rows = (
        await committed.scalars(select(AuditLog).where(AuditLog.action == "grant.delete"))
    ).all()
    removed = [row for row in rows if (row.details or {}).get("group_id") == group["id"]]
    assert len(removed) == 2
    assert {row.details.get("group") for row in removed} == {group["name"]}
    assert {row.details.get("org_id") for row in removed} == {org["id"], None}
    summary = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "model_group.delete", AuditLog.resource_id == group["id"]
        )
    )
    assert summary is not None and summary.details["grants_removed"] == 2


async def test_deleting_a_member_name_is_audited(client, committed) -> None:
    a = await make_model(committed, f"m-{suffix()}")
    alias = await make_alias(committed, f"al-{suffix()}", a)
    await committed.commit()
    group = create_group(client, [a.name, alias.name])

    deleted = client.delete(f"/api/v1/aliases/{alias.id}", headers=ADMIN_AUTH)
    assert deleted.status_code == 204, deleted.text
    got = client.get(f"/api/v1/model-groups/{group['id']}", headers=ADMIN_AUTH).json()
    assert [m["name"] for m in got["members"]] == [a.name]
    row = await committed.scalar(
        select(AuditLog).where(
            AuditLog.action == "model_group.member_remove",
            AuditLog.resource_id == group["id"],
        )
    )
    assert row is not None
    assert row.details == {"name": group["name"], "member": alias.name, "member_id": str(alias.id)}

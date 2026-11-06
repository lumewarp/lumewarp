"""Team CRUD, visibility, archiving and history."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, Team, TeamMember, TeamRole
from lumewarp_control_plane.usage import ingest
from tests.api.helpers import ADMIN_AUTH, auth, create_org, suffix, user_with_token
from tests.factories import make_key, make_member, make_service_account, make_team, make_user


def test_team_create_requires_an_org(client) -> None:
    response = client.post("/api/v1/teams", json={"name": f"no-org-{suffix()}"}, headers=ADMIN_AUTH)
    assert response.status_code == 422, response.text
    assert "org_id" in response.json()["error"]["message"]


def test_team_crud_and_unique_name(client) -> None:
    org = create_org(client)
    name = f"team-{suffix()}"
    created = client.post(
        "/api/v1/teams",
        json={"name": name, "org_id": org["id"], "budget_nano_usd": 1000},
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    team = created.json()
    assert team["model_access"] == "all"
    assert team["budget_nano_usd"] == 1000
    assert team["org_id"] == org["id"]
    duplicate = client.post(
        "/api/v1/teams", json={"name": name, "org_id": org["id"]}, headers=ADMIN_AUTH
    )
    assert duplicate.status_code == 409
    path = f"/api/v1/teams/{team['id']}"
    assert client.get(path, headers=ADMIN_AUTH).json() == team
    updated = client.patch(
        path,
        json={"name": f"renamed-{suffix()}", "budget_nano_usd": None},
        headers=ADMIN_AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["budget_nano_usd"] is None
    # DELETE archives; the name is free again and the row stays for history.
    assert client.delete(path, headers=ADMIN_AUTH).status_code == 204
    assert client.get(path, headers=ADMIN_AUTH).status_code == 404
    assert client.delete(path, headers=ADMIN_AUTH).status_code == 409
    reused = client.post(
        "/api/v1/teams", json={"name": name, "org_id": org["id"]}, headers=ADMIN_AUTH
    )
    assert reused.status_code == 201, reused.text
    assert reused.json()["id"] != team["id"]


async def test_team_list_only_returns_readable_teams(client, committed) -> None:
    user, token = await user_with_token(committed)
    own = await make_team(committed)
    other = await make_team(committed)
    await make_member(committed, own, user)
    await committed.commit()
    listed = client.get("/api/v1/teams", headers=auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [str(own.id)]
    for team_id in (other.id, uuid.uuid4()):
        assert client.get(f"/api/v1/teams/{team_id}", headers=auth(token)).status_code == 404


async def test_archive_revokes_keys_and_deactivates_service_accounts(
    client, committed, api_scope
) -> None:
    team = await make_team(committed)
    owner = await make_user(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner)
    # A rotation-grace hash must die with the key too.
    key.previous_key_hash = b"p" * 32
    key.previous_key_expires_at = datetime.now(UTC) + timedelta(hours=1)
    account = await make_service_account(committed, team)
    account_key = await make_key(committed, team, account)
    await committed.commit()
    key_id, account_id, account_key_id = key.id, account.id, account_key.id

    team_id = team.id
    assert client.delete(f"/api/v1/teams/{team_id}", headers=ADMIN_AUTH).status_code == 204
    committed.expire_all()
    stored = await committed.get(ApiKey, key_id)
    assert stored is not None
    assert stored.lifecycle is KeyLifecycle.REVOKED
    assert stored.revoked_at is not None
    assert (stored.previous_key_hash, stored.previous_key_expires_at) == (None, None)
    assert (await committed.get(ApiKey, account_key_id)).lifecycle is KeyLifecycle.REVOKED
    from lumewarp_control_plane.models import ServiceAccount

    archived_account = await committed.get(ServiceAccount, account_id)
    assert archived_account is not None and archived_account.deactivated_at is not None
    archived_team = await committed.get(Team, team_id)
    assert archived_team is not None and archived_team.deleted_at is not None

    # Every write path is closed, including the key and service-account paths that do not
    # go through the team endpoint.
    assert client.get(f"/api/v1/teams/{team_id}", headers=ADMIN_AUTH).status_code == 404
    assert (
        client.patch(
            f"/api/v1/teams/{team_id}", json={"name": "nope"}, headers=ADMIN_AUTH
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/teams/{team_id}/keys", json={"name": "late"}, headers=ADMIN_AUTH
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/keys/{key_id}", headers=ADMIN_AUTH).status_code == 404
    assert client.post(f"/api/v1/keys/{key_id}/rotate", headers=ADMIN_AUTH).status_code == 404
    assert (
        client.get(f"/api/v1/service-accounts/{account_id}", headers=ADMIN_AUTH).status_code == 404
    )

    # The compiler leaves the team and its keys out (all keys are revoked).
    async with AsyncSession(committed.bind) as session:
        compiled = await compile_scope(session, api_scope, require_local_membership=True)
    assert str(team_id) not in {row.id for row in compiled.snapshot.teams}
    assert {str(key_id), str(account_key_id)} & {
        row.key_id for row in compiled.snapshot.api_keys
    } == set()


async def test_archive_keeps_members_and_late_usage_ingests(client, committed, api_scope) -> None:
    team = await make_team(committed)
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    await committed.commit()
    team_id, user_id, key_id = team.id, user.id, key.id
    assert client.delete(f"/api/v1/teams/{team_id}", headers=ADMIN_AUTH).status_code == 204
    committed.expire_all()
    # Archiving is not a delete: the membership and owner policy stay for attribution.
    assert (await committed.get(TeamMember, (team_id, user_id))) is not None

    # A usage event that was in flight during the archive still ingests without a rollback
    # (no FK to a removed team row, and the owner scope is still resolvable).
    event = usage_pb2.UsageEvent(
        request_id=str(uuid.uuid4()),
        timestamp_unix_ms=int(datetime.now(UTC).timestamp() * 1000),
        key_id=str(key_id),
        team_id=str(team_id),
        requested="mock",
        model="mock",
        endpoint="chat_completions",
        status=200,
        prompt_tokens=10,
        completion_tokens=5,
        cost_nano_usd=1000,
        cost_known=True,
        duration_ms=10,
        config_version=1,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )
    async with AsyncSession(committed.bind) as session, session.begin():
        assert await ingest(session, "gw-test", [event], scope_name=api_scope) == 1
    from lumewarp_control_plane.models import UsageTotal

    async with AsyncSession(committed.bind) as session:
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == "team", UsageTotal.scope_id == team_id
            )
        )
        assert total is not None and total.request_count == 1
    # History reads are not filtered: audit and usage still find the archived team.
    audit = client.get("/api/v1/audit", params={"team_id": str(team_id)}, headers=ADMIN_AUTH)
    assert audit.status_code == 200, audit.text
    assert any(row["action"] == "team.archive" for row in audit.json()["items"])
    usage = client.get(
        "/api/v1/usage",
        params={"team_id": str(team_id), "group_by": "team"},
        headers=ADMIN_AUTH,
    )
    assert usage.status_code == 200, usage.text
    assert [row["team_id"] for row in usage.json()["items"]] == [str(team_id)]


async def test_team_admin_may_change_settings_but_not_quota(client, committed) -> None:
    team = await make_team(
        committed,
        rpm_limit=10,
        budget_nano_usd=1000,
        max_keys_per_member=2,
        max_member_key_lifetime_seconds=3600,
    )
    admin, token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}"
    headers = auth(token)

    for body in (
        {"rpm_limit": 20},
        {"budget_nano_usd": 2000},
        {"model_access": "list", "allowed_models": []},
    ):
        response = client.patch(path, json=body, headers=headers)
        assert response.status_code == 403, (body, response.text)
        assert response.json()["error"]["code"] == "forbidden"
    unchanged = await committed.get(Team, team.id)
    assert unchanged is not None
    await committed.refresh(unchanged)
    assert (unchanged.rpm_limit, unchanged.budget_nano_usd) == (10, 1000)

    # Name and the member key limits are the team admin's own settings.
    renamed = client.patch(
        path,
        json={"name": f"renamed-{suffix()}", "max_keys_per_member": 3},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["max_keys_per_member"] == 3
    # Resending the unchanged policy values still works for a team admin.
    resend = client.patch(
        path,
        json={
            "model_access": "all",
            "allowed_models": [],
            "rpm_limit": 10,
            "tpm_limit": None,
            "max_parallel": None,
            "budget_nano_usd": 1000,
            "max_member_key_lifetime_seconds": 3600,
        },
        headers=headers,
    )
    assert resend.status_code == 200, resend.text
    # A plain member may not touch the team at all, not even with a no-op payload.
    for body in ({"name": "member-edit"}, {"model_access": "all"}, {}):
        response = client.patch(path, json=body, headers=auth(member_token))
        assert response.status_code == 403, (body, response.text)
    # Installation admin sets quota.
    quota = client.patch(path, json={"rpm_limit": 5}, headers=ADMIN_AUTH)
    assert quota.status_code == 200, quota.text
    assert quota.json()["rpm_limit"] == 5


async def test_team_read_is_basic_for_a_plain_member(client, committed) -> None:
    team = await make_team(
        committed,
        rpm_limit=10,
        budget_nano_usd=1000,
        max_keys_per_member=2,
        max_member_key_lifetime_seconds=3600,
    )
    member, member_token = await user_with_token(committed)
    admin, admin_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    await committed.commit()
    basic_keys = {
        "id",
        "org_id",
        "name",
        "created_at",
        "updated_at",
        "max_keys_per_member",
        "max_member_key_lifetime_seconds",
    }
    for path in (f"/api/v1/teams/{team.id}", "/api/v1/teams"):
        response = client.get(path, headers=auth(member_token))
        assert response.status_code == 200, response.text
        body = response.json() if path.endswith(str(team.id)) else response.json()["items"][0]
        assert set(body) == basic_keys, path
        assert (body["name"], body["max_keys_per_member"]) == (team.name, 2)
    full = client.get(f"/api/v1/teams/{team.id}", headers=auth(admin_token))
    assert full.status_code == 200
    assert {"model_access", "allowed_models", "rpm_limit", "budget_nano_usd"} <= set(full.json())
    listed = client.get("/api/v1/teams", headers=auth(admin_token)).json()["items"]
    assert {"model_access", "allowed_models", "rpm_limit", "budget_nano_usd"} <= set(listed[0])
    assert client.get(f"/api/v1/teams/{team.id}", headers=ADMIN_AUTH).status_code == 200

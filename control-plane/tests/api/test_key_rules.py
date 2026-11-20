"""Key zone binding and the per-zone key rules.

A key belongs to exactly one zone: it is created through `zone`, compiled only into that
zone's snapshot, and the zone's rules gate creation, rotation and resumption. A rule
tightened after a key exists never stops it; `rule_violations` reports it instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import TeamRole, Zone
from tests.api.helpers import ADMIN_AUTH, auth, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_org,
    make_org_admin,
    make_service_account,
    make_team,
    make_user,
)


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


def create_key(client, team, token: str | None = None, **body):
    return client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={"name": f"key-{uuid.uuid4().hex[:6]}", **body},
        headers=auth(token) if token is not None else ADMIN_AUTH,
    )


async def team_admin(committed) -> tuple[object, object, str]:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    await committed.commit()
    return team, user, token


async def compile_key(api_engine, zone: str, key_id: uuid.UUID) -> pb.ApiKey | None:
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_zone(session, zone, require_local_membership=True)
    return next((item for item in compiled.snapshot.api_keys if item.key_id == str(key_id)), None)


async def test_key_binds_its_zone_and_listing_filters(client, committed) -> None:
    zone = await create_zone(client, committed)
    team, _, token = await team_admin(committed)

    default_key = create_key(client, team, token)
    assert default_key.status_code == 201, default_key.text
    assert default_key.json()["key"]["zone"] == "api-test"
    zoned = create_key(client, team, token, zone=zone["id"])
    assert zoned.status_code == 201, zoned.text
    zoned_key = zoned.json()["key"]
    assert zoned_key["zone"] == zone["id"]
    # Rotation keeps the zone.
    rotated = client.post(f"/api/v1/keys/{zoned_key['id']}/rotate", headers=ADMIN_AUTH)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["key"]["zone"] == zone["id"]
    # The zone is immutable: it is not a writable key field.
    moved = client.patch(
        f"/api/v1/keys/{zoned_key['id']}", json={"zone": "api-test"}, headers=ADMIN_AUTH
    )
    assert moved.status_code == 422, moved.text

    path = f"/api/v1/teams/{team.id}/keys"
    everything = client.get(path, headers=ADMIN_AUTH).json()["items"]
    assert {row["id"] for row in everything} >= {
        default_key.json()["key"]["id"],
        zoned_key["id"],
    }
    filtered = client.get(path, params={"zone": zone["id"]}, headers=ADMIN_AUTH).json()["items"]
    assert [row["id"] for row in filtered] == [zoned_key["id"]]


async def test_key_zone_must_be_known_and_live(client, committed) -> None:
    team, _, _ = await team_admin(committed)
    live = await create_zone(client, committed)
    assert client.delete(f"/api/v1/zones/{live['id']}", headers=ADMIN_AUTH).status_code == 200
    archived = create_key(client, team, zone=live["id"])
    assert archived.status_code == 404, archived.text
    unknown = create_key(client, team, zone="no-such-zone")
    assert unknown.status_code == 404, unknown.text


async def test_min_creator_role_applies_to_user_and_service_account_keys(client, committed) -> None:
    zone = await create_zone(client, committed, key_min_creator_role="team_admin")
    team = await make_team(committed)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    admin_user, admin_token = await user_with_token(committed)
    await make_member(committed, team, admin_user, role=TeamRole.ADMIN)
    account = await make_service_account(committed, team)
    await committed.commit()

    refused = create_key(client, team, member_token, zone=zone["id"])
    assert refused.status_code == 422, refused.text
    assert "requires team_admin" in refused.json()["error"]["message"]
    refused_sa = create_key(
        client, team, member_token, zone=zone["id"], service_account_id=str(account.id)
    )
    assert refused_sa.status_code == 422, refused_sa.text
    assert create_key(client, team, admin_token, zone=zone["id"]).status_code == 201
    assert (
        create_key(
            client, team, admin_token, zone=zone["id"], service_account_id=str(account.id)
        ).status_code
        == 201
    )

    # `org_admin` needs the org role: a team admin is not enough.
    org = await make_org(committed)
    org_team = await make_team(committed, org=org)
    team_admin_user, team_admin_token = await user_with_token(committed)
    await make_member(committed, org_team, team_admin_user, role=TeamRole.ADMIN)
    org_admin_user, org_admin_token = await user_with_token(committed)
    await make_org_admin(committed, org, org_admin_user)
    installation_user, installation_token = await user_with_token(
        committed, installation_admin=True
    )
    strict = await create_zone(client, committed, key_min_creator_role="org_admin")
    await committed.commit()

    assert create_key(client, org_team, team_admin_token, zone=strict["id"]).status_code == 422
    assert create_key(client, org_team, org_admin_token, zone=strict["id"]).status_code == 201
    assert create_key(client, org_team, installation_token, zone=strict["id"]).status_code == 201
    assert installation_user.is_active is True


async def test_lifetime_is_the_minimum_of_zone_and_team(client, committed) -> None:
    zone = await create_zone(client, committed, key_max_lifetime_seconds=3600)
    team, _, token = await team_admin(committed)
    team.max_member_key_lifetime_seconds = 7200
    await committed.commit()
    user_key = create_key(client, team, token, zone=zone["id"])
    assert user_key.status_code == 201, user_key.text
    expires = datetime.fromisoformat(user_key.json()["key"]["expires_at"])
    remaining = (expires - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= 3600

    too_long = datetime.now(UTC) + timedelta(seconds=4000)
    refused = create_key(client, team, token, zone=zone["id"], expires_at=too_long.isoformat())
    assert refused.status_code == 422, refused.text
    assert "3600" in refused.json()["error"]["message"]

    # A tighter team cap wins for user keys.
    team.max_member_key_lifetime_seconds = 1800
    await committed.commit()
    tight = create_key(client, team, token, zone=zone["id"])
    assert tight.status_code == 201, tight.text
    tight_expiry = datetime.fromisoformat(tight.json()["key"]["expires_at"])
    assert (tight_expiry - datetime.now(UTC)).total_seconds() <= 1800

    # A service-account key ignores the team's member cap.
    account = await make_service_account(committed, team)
    await committed.commit()
    service = create_key(client, team, token, zone=zone["id"], service_account_id=str(account.id))
    assert service.status_code == 201, service.text
    service_expiry = datetime.fromisoformat(service.json()["key"]["expires_at"])
    assert (service_expiry - datetime.now(UTC)).total_seconds() > 1800


async def test_expiry_required_refuses_a_key_without_one(client, committed) -> None:
    zone = await create_zone(client, committed, key_expiry_required=True)
    team, _, token = await team_admin(committed)
    refused = create_key(client, team, token, zone=zone["id"])
    assert refused.status_code == 422, refused.text
    assert "expiry" in refused.json()["error"]["message"]
    expires = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    created = create_key(client, team, token, zone=zone["id"], expires_at=expires)
    assert created.status_code == 201, created.text
    # Clearing the expiry later is refused too.
    cleared = client.patch(
        f"/api/v1/keys/{created.json()['key']['id']}", json={"expires_at": None}, headers=ADMIN_AUTH
    )
    assert cleared.status_code == 422, cleared.text


async def test_per_member_count_is_counted_within_the_zone(client, committed) -> None:
    zone = await create_zone(client, committed, key_max_per_member=1)
    team, _, token = await team_admin(committed)
    team.max_keys_per_member = 5
    await committed.commit()
    first = create_key(client, team, token, zone=zone["id"])
    assert first.status_code == 201, first.text
    second = create_key(client, team, token, zone=zone["id"])
    assert second.status_code == 409, second.text
    # Another zone has its own count.
    assert create_key(client, team, token).status_code == 201
    # Another member has their own count in the same zone.
    other, other_token = await user_with_token(committed)
    await make_member(committed, team, other)
    await committed.commit()
    assert create_key(client, team, other_token, zone=zone["id"]).status_code == 201


async def test_service_account_keys_can_be_disallowed(client, committed) -> None:
    zone = await create_zone(client, committed)
    team, _, token = await team_admin(committed)
    account = await make_service_account(committed, team)
    await committed.commit()
    created = create_key(client, team, token, zone=zone["id"], service_account_id=str(account.id))
    assert created.status_code == 201, created.text
    tightened = client.patch(
        f"/api/v1/zones/{zone['id']}",
        json={"service_account_keys_allowed": False},
        headers=ADMIN_AUTH,
    )
    assert tightened.status_code == 200, tightened.text
    refused = create_key(client, team, token, zone=zone["id"], service_account_id=str(account.id))
    assert refused.status_code == 422, refused.text
    assert "service-account" in refused.json()["error"]["message"]
    # An existing key keeps working until rotation, which the tightened rule refuses.
    rotate = client.post(f"/api/v1/keys/{created.json()['key']['id']}/rotate", headers=ADMIN_AUTH)
    assert rotate.status_code == 409, rotate.text
    assert "service-account" in rotate.json()["error"]["message"]


async def test_user_keys_can_be_disallowed(client, committed) -> None:
    zone = await create_zone(client, committed)
    team, _, admin_token = await team_admin(committed)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    account = await make_service_account(committed, team)
    await committed.commit()
    created = create_key(client, team, admin_token, zone=zone["id"])
    assert created.status_code == 201, created.text
    tightened = client.patch(
        f"/api/v1/zones/{zone['id']}",
        json={"user_keys_allowed": False},
        headers=ADMIN_AUTH,
    )
    assert tightened.status_code == 200, tightened.text
    # A team admin and a plain member are refused alike.
    for token in (admin_token, member_token):
        refused = create_key(client, team, token, zone=zone["id"])
        assert refused.status_code == 422, refused.text
        assert "does not allow user keys" in refused.json()["error"]["message"]
    # Service-account keys are still allowed in this zone.
    service = create_key(
        client, team, admin_token, zone=zone["id"], service_account_id=str(account.id)
    )
    assert service.status_code == 201, service.text
    # An existing key keeps working until rotation, which the tightened rule refuses.
    rotate = client.post(f"/api/v1/keys/{created.json()['key']['id']}/rotate", headers=ADMIN_AUTH)
    assert rotate.status_code == 409, rotate.text
    assert "does not allow user keys" in rotate.json()["error"]["message"]


async def test_tightened_user_rule_flags_the_key_and_it_keeps_working(
    client, committed, api_engine
) -> None:
    zone = await create_zone(client, committed)
    team, _, token = await team_admin(committed)
    created = create_key(client, team, token, zone=zone["id"])
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]

    tightened = client.patch(
        f"/api/v1/zones/{zone['id']}",
        json={"user_keys_allowed": False},
        headers=ADMIN_AUTH,
    )
    assert tightened.status_code == 200, tightened.text

    rows = client.get(
        f"/api/v1/teams/{team.id}/keys",
        params={"zone": zone["id"]},
        headers=ADMIN_AUTH,
    ).json()["items"]
    row = next(item for item in rows if item["id"] == key_id)
    assert "user_keys_allowed" in row["rule_violations"]
    assert row["lifecycle"] == "active"
    # The tightened rule does not remove the key from the snapshot.
    wire = await compile_key(api_engine, zone["id"], uuid.UUID(key_id))
    assert wire is not None and wire.status == pb.KEY_STATUS_ACTIVE


async def test_tightened_rules_flag_the_key_and_it_keeps_working(
    client, committed, api_engine
) -> None:
    zone = await create_zone(client, committed)
    team = await make_team(committed)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    await committed.commit()
    created = create_key(client, team, member_token, zone=zone["id"])
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]

    tightened = client.patch(
        f"/api/v1/zones/{zone['id']}",
        json={
            "key_expiry_required": True,
            "key_min_creator_role": "team_admin",
            "key_max_lifetime_seconds": 60,
        },
        headers=ADMIN_AUTH,
    )
    assert tightened.status_code == 200, tightened.text

    rows = client.get(
        f"/api/v1/teams/{team.id}/keys",
        params={"zone": zone["id"]},
        headers=ADMIN_AUTH,
    ).json()["items"]
    row = next(item for item in rows if item["id"] == key_id)
    assert {"key_expiry_required", "key_min_creator_role"} <= set(row["rule_violations"])
    assert row["lifecycle"] == "active"
    # The tightened rule does not remove the key from the snapshot.
    wire = await compile_key(api_engine, zone["id"], uuid.UUID(key_id))
    assert wire is not None and wire.status == pb.KEY_STATUS_ACTIVE


async def test_key_compiles_only_into_its_zone_snapshot(client, committed, api_engine) -> None:
    first = await create_zone(client, committed)
    second = await create_zone(client, committed)
    team, _, _ = await team_admin(committed)
    owner = await make_user(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner, zone=first["id"])
    await committed.commit()

    own = await compile_key(api_engine, first["id"], key.id)
    assert own is not None and own.status == pb.KEY_STATUS_ACTIVE
    assert await compile_key(api_engine, "api-test", key.id) is None
    assert await compile_key(api_engine, second["id"], key.id) is None


async def test_archive_is_refused_while_a_live_key_exists(client, committed) -> None:
    zone = await create_zone(client, committed)
    team, _, token = await team_admin(committed)
    created = create_key(client, team, token, zone=zone["id"])
    assert created.status_code == 201, created.text
    blocked = client.delete(f"/api/v1/zones/{zone['id']}", headers=ADMIN_AUTH)
    assert blocked.status_code == 409, blocked.text
    assert "key" in blocked.json()["error"]["message"]
    assert (
        client.post(
            f"/api/v1/keys/{created.json()['key']['id']}/revoke", headers=ADMIN_AUTH
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/zones/{zone['id']}", headers=ADMIN_AUTH).status_code == 200

"""Inherited administration: installation admins and org admins listed below and owning keys.

An installation admin administers every org and team, an org admin its org's teams. Both
are listed in the lower lists with `inherited_from`, may own keys in the teams they
administer without a membership, and lose those keys (`owner_removed`) when the last source
that allowed them goes away.
"""

import uuid

from sqlalchemy import select

from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.models import AuditLog, OrgAdmin, TeamRole
from tests.api.helpers import ADMIN_AUTH, auth, user_with_token
from tests.factories import make_member, make_org, make_org_admin, make_team


def _create_key(client, team_id, headers, name="own") -> str:
    response = client.post(f"/api/v1/teams/{team_id}/keys", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["key"]["id"]


def _state(client, key_id) -> tuple[str, str | None]:
    row = client.get(f"/api/v1/keys/{key_id}", headers=ADMIN_AUTH).json()
    return row["lifecycle"], row["suspended_reason"]


async def _audit_details(session, action: str, resource_id) -> dict:
    row = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.resource_id == str(resource_id))
        .order_by(AuditLog.id.desc())
    )
    assert row is not None
    return row.details


async def test_removing_an_org_admin_suspends_keys_where_nothing_else_allows_them(
    client, committed
) -> None:
    org = await make_org(committed)
    inherited_only = await make_team(committed, org=org)
    also_member = await make_team(committed, org=org)
    user, token = await user_with_token(committed)
    await make_org_admin(committed, org, user)
    await make_member(committed, also_member, user)
    await committed.commit()
    headers = auth(token)
    lost = _create_key(client, inherited_only.id, headers)
    kept = _create_key(client, also_member.id, headers)

    response = client.delete(f"/api/v1/orgs/{org.id}/admins/{user.id}", headers=ADMIN_AUTH)
    assert response.status_code == 204, response.text
    assert _state(client, lost) == ("suspended", "owner_removed")
    assert _state(client, kept) == ("active", None)
    details = await _audit_details(committed, "org.admin.remove", user.id)
    assert details["suspended_keys"] == 1

    # The owner cannot resume while nothing allows them; once an org admin again, they can.
    response = client.patch(f"/api/v1/keys/{lost}", json={"lifecycle": "active"}, headers=headers)
    assert response.status_code == 404, response.text
    committed.add(OrgAdmin(org_id=org.id, user_id=user.id))
    await committed.commit()
    response = client.patch(f"/api/v1/keys/{lost}", json={"lifecycle": "active"}, headers=headers)
    assert response.status_code == 200, response.text
    assert _state(client, lost) == ("active", None)


async def test_removing_an_installation_admin_keeps_keys_still_allowed(client, committed) -> None:
    org = await make_org(committed)
    nothing_else = await make_team(committed)
    org_team = await make_team(committed, org=org)
    member_team = await make_team(committed)
    user, token = await user_with_token(committed, installation_admin=True)
    await make_org_admin(committed, org, user)
    await make_member(committed, member_team, user)
    await committed.commit()
    headers = auth(token)
    lost = _create_key(client, nothing_else.id, headers)
    by_org = _create_key(client, org_team.id, headers)
    by_member = _create_key(client, member_team.id, headers)

    response = client.delete(f"/api/v1/installation-admins/{user.id}", headers=ADMIN_AUTH)
    assert response.status_code == 204, response.text
    assert _state(client, lost) == ("suspended", "owner_removed")
    assert _state(client, by_org) == ("active", None)
    assert _state(client, by_member) == ("active", None)
    details = await _audit_details(committed, "installation_admin.remove", user.id)
    assert details["suspended_keys"] == 1


async def test_removing_the_membership_of_an_org_admin_keeps_their_keys(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    user, token = await user_with_token(committed)
    await make_org_admin(committed, org, user)
    await make_member(committed, team, user)
    await committed.commit()
    key = _create_key(client, team.id, auth(token))

    response = client.delete(f"/api/v1/teams/{team.id}/members/{user.id}", headers=ADMIN_AUTH)
    assert response.status_code == 204, response.text
    assert _state(client, key) == ("active", None)
    assert (await _audit_details(committed, "member.remove", user.id))["suspended_keys"] == 0
    # The user is still listed, now by inheritance only, and has no membership to edit.
    rows = client.get(f"/api/v1/teams/{team.id}/members?limit=500", headers=ADMIN_AUTH).json()[
        "items"
    ]
    row = next(r for r in rows if r["user_id"] == str(user.id))
    assert (row["role"], row["inherited_from"], row["direct"]) == ("admin", "org", False)
    response = client.patch(
        f"/api/v1/teams/{team.id}/members/{user.id}", json={"rpm_limit": 5}, headers=ADMIN_AUTH
    )
    assert response.status_code == 404, response.text


async def test_member_list_pages_once_over_direct_and_inherited_rows(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    direct, _ = await user_with_token(committed)
    both, _ = await user_with_token(committed)
    org_admin, _ = await user_with_token(committed)
    installation, _ = await user_with_token(committed, installation_admin=True)
    await make_member(committed, team, direct)
    await make_member(committed, team, both, role=TeamRole.ADMIN)
    await make_org_admin(committed, org, both)
    await make_org_admin(committed, org, org_admin)
    await committed.commit()

    rows, cursor = [], None
    while True:
        params = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = client.get(
            f"/api/v1/teams/{team.id}/members", params=params, headers=ADMIN_AUTH
        ).json()
        rows += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    ids = [r["user_id"] for r in rows]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    by_id = {r["user_id"]: (r["role"], r["inherited_from"], r["direct"]) for r in rows}
    assert by_id[str(direct.id)] == ("member", None, True)
    assert by_id[str(both.id)] == ("admin", "org", True)
    assert by_id[str(org_admin.id)] == ("admin", "org", False)
    assert by_id[str(installation.id)] == ("admin", "installation", False)


async def test_org_admin_list_includes_installation_admins_read_only(client, committed) -> None:
    org = await make_org(committed)
    local, _ = await user_with_token(committed)
    installation, _ = await user_with_token(committed, installation_admin=True)
    await make_org_admin(committed, org, local)
    await committed.commit()
    rows = client.get(f"/api/v1/orgs/{org.id}/admins?limit=500", headers=ADMIN_AUTH).json()["items"]
    by_id = {r["user_id"]: (r["inherited_from"], r["direct"]) for r in rows}
    assert by_id[str(local.id)] == (None, True)
    assert by_id[str(installation.id)] == ("installation", False)
    # Removed at the installation level, not here.
    response = client.delete(f"/api/v1/orgs/{org.id}/admins/{installation.id}", headers=ADMIN_AUTH)
    assert response.status_code == 404, response.text


async def test_inherited_admin_keys_follow_the_team_member_limits(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org, max_keys_per_member=1)
    user, token = await user_with_token(committed)
    await make_org_admin(committed, org, user)
    await committed.commit()
    headers = auth(token)
    _create_key(client, team.id, headers)
    response = client.post(f"/api/v1/teams/{team.id}/keys", json={"name": "two"}, headers=headers)
    assert response.status_code == 409, response.text
    assert "member key limit" in response.json()["error"]["message"]


async def test_users_me_lists_every_team_for_an_installation_admin(client, committed) -> None:
    team = await make_team(committed, max_keys_per_member=3)
    member_team = await make_team(committed)
    user, token = await user_with_token(committed, installation_admin=True)
    await make_member(committed, member_team, user)
    await committed.commit()
    me = client.get("/api/v1/users/me", headers=auth(token)).json()
    teams = {t["team_id"]: t for t in me["teams"]}
    row = teams[str(team.id)]
    assert (row["role"], row["inherited_from"], row["direct"]) == ("admin", "installation", False)
    assert row["key_limits"]["max_keys_per_member"] == 3
    row = teams[str(member_team.id)]
    assert (row["role"], row["inherited_from"], row["direct"]) == ("admin", "installation", True)
    names = [t["team_name"] for t in me["teams"]]
    assert names == sorted(names)


async def test_external_org_admin_owns_keys_by_check(client, committed, monkeypatch) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    user, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if user_id != str(user.id):
            return Decision.DENY
        if resource.type == "org" and resource.id == str(org.id):
            return Decision.ALLOW if relation is Relation.ADMIN else Decision.DENY
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    key = _create_key(client, team.id, auth(token))
    assert _state(client, key) == ("active", None)
    # No local row: the external org admin is not listed, yet owns the key.
    rows = client.get(f"/api/v1/teams/{team.id}/members?limit=500", headers=ADMIN_AUTH).json()[
        "items"
    ]
    assert str(user.id) not in {r["user_id"] for r in rows}
    assert uuid.UUID(key)

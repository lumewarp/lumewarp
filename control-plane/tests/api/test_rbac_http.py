"""Table-driven RBAC denial matrix through the HTTP API.

The raw matrix lives in `test_rbac.py`. This test proves the routers wire each action to
the intended hierarchy position: own team, another team in the same org, and another org.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lumewarp_control_plane.models import Org, Project, Team
from tests.api.helpers import (
    auth,
    grant,
    key_row,
    org_tree,
    suffix,
    user_with_token,
)


@dataclass(frozen=True)
class Case:
    method: str
    path: Callable[[dict], str]
    body: Callable[[str], dict | None]
    allowed: frozenset[str]


async def test_rbac_matrix(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="rbac-a1")
    team2 = Team(org_id=org.id, name=f"team2-{suffix()}")
    committed.add(team2)
    await committed.flush()
    project2 = Project(org_id=org.id, team_id=team2.id, name=f"project2-{suffix()}")
    committed.add(project2)
    await committed.flush()
    other_org, other_team, other_project = await org_tree(committed, prefix="rbac-b1")
    key = await key_row(committed, project, name="key-a1")
    await key_row(committed, project2, name="key-a2")
    revoke_key = await key_row(committed, project, name="key-revoke")
    empty_org = Org(name=f"empty-{suffix()}")
    committed.add(empty_org)
    await committed.flush()

    principals: dict[str, str] = {"superuser": ""}
    _, principals["superuser"] = await user_with_token(committed, superuser=True)
    for role, target_org, target_team, grant_role in (
        ("org_owner", org, None, "owner"),
        ("org_admin", org, None, "admin"),
        ("team_admin_a1", org, team, "admin"),
        ("member_a1", org, team, "member"),
        ("viewer_a1", org, team, "viewer"),
        ("team_admin_a2", org, team2, "admin"),
        ("team_admin_b1", other_org, other_team, "admin"),
    ):
        user, secret = await user_with_token(committed)
        await grant(committed, user, target_org, team=target_team, role=grant_role)
        principals[role] = secret
    await committed.commit()

    ids = {
        "org": str(org.id),
        "team": str(team.id),
        "project": str(project.id),
        "key": str(key.id),
        "revoke_key": str(revoke_key.id),
        "other_org": str(other_org.id),
        "other_team": str(other_team.id),
        "empty_org": str(empty_org.id),
    }

    def body_with_name(prefix: str) -> Callable[[str], dict]:
        return lambda role: {"name": f"{prefix}-{role}-{suffix()}"}

    cases = [
        Case("POST", lambda i: "/api/v1/orgs", body_with_name("org"), frozenset({"superuser"})),
        Case(
            "GET",
            lambda i: f"/api/v1/orgs/{i['org']}",
            lambda role: None,
            frozenset(
                {
                    "superuser",
                    "org_owner",
                    "org_admin",
                    "team_admin_a1",
                    "member_a1",
                    "viewer_a1",
                    "team_admin_a2",
                }
            ),
        ),
        Case(
            "PATCH",
            lambda i: f"/api/v1/orgs/{i['org']}",
            body_with_name("org-update"),
            frozenset({"superuser", "org_owner", "org_admin"}),
        ),
        Case(
            "DELETE",
            lambda i: f"/api/v1/orgs/{i['empty_org']}",
            lambda role: None,
            frozenset({"superuser"}),
        ),
        Case(
            "POST",
            lambda i: f"/api/v1/orgs/{i['org']}/teams",
            body_with_name("team"),
            frozenset({"superuser", "org_owner", "org_admin"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/teams/{i['team']}",
            lambda role: None,
            frozenset(
                {
                    "superuser",
                    "org_owner",
                    "org_admin",
                    "team_admin_a1",
                    "member_a1",
                    "viewer_a1",
                }
            ),
        ),
        Case(
            "PATCH",
            lambda i: f"/api/v1/teams/{i['team']}",
            body_with_name("team-update"),
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1"}),
        ),
        Case(
            "POST",
            lambda i: f"/api/v1/teams/{i['team']}/projects",
            body_with_name("project"),
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/projects/{i['project']}",
            lambda role: None,
            frozenset(
                {
                    "superuser",
                    "org_owner",
                    "org_admin",
                    "team_admin_a1",
                    "member_a1",
                    "viewer_a1",
                }
            ),
        ),
        Case(
            "PATCH",
            lambda i: f"/api/v1/projects/{i['project']}",
            body_with_name("project-update"),
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1"}),
        ),
        Case(
            "POST",
            lambda i: f"/api/v1/projects/{i['project']}/keys",
            body_with_name("key"),
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1", "member_a1"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/keys/{i['key']}",
            lambda role: None,
            frozenset(
                {
                    "superuser",
                    "org_owner",
                    "org_admin",
                    "team_admin_a1",
                    "member_a1",
                    "viewer_a1",
                }
            ),
        ),
        Case(
            "PATCH",
            lambda i: f"/api/v1/keys/{i['key']}",
            body_with_name("key-update"),
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1", "member_a1"}),
        ),
        Case(
            "POST",
            lambda i: f"/api/v1/keys/{i['revoke_key']}/revoke",
            lambda role: None,
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1", "member_a1"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/usage?group_by=model&team_id={i['team']}",
            lambda role: None,
            frozenset(
                {
                    "superuser",
                    "org_owner",
                    "org_admin",
                    "team_admin_a1",
                    "member_a1",
                    "viewer_a1",
                }
            ),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/usage?group_by=model&org_id={i['org']}",
            lambda role: None,
            frozenset({"superuser", "org_owner", "org_admin"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/audit?org_id={i['org']}&team_id={i['team']}",
            lambda role: None,
            frozenset({"superuser", "org_owner", "org_admin", "team_admin_a1"}),
        ),
        Case(
            "GET",
            lambda i: f"/api/v1/audit?org_id={i['org']}",
            lambda role: None,
            frozenset({"superuser", "org_owner", "org_admin"}),
        ),
        Case("GET", lambda i: "/api/v1/upstreams", lambda role: None, frozenset(principals)),
        Case(
            "POST",
            lambda i: "/api/v1/upstreams",
            lambda role: {
                "name": f"up-{role}-{suffix()}",
                "kind": "vllm",
                "base_url": "http://127.0.0.1:9000/v1",
            },
            frozenset({"superuser"}),
        ),
        Case("GET", lambda i: "/api/v1/users", lambda role: None, frozenset({"superuser"})),
        Case("GET", lambda i: "/api/v1/gateways", lambda role: None, frozenset({"superuser"})),
        Case(
            "GET",
            lambda i: "/api/v1/config/versions",
            lambda role: None,
            frozenset({"superuser"}),
        ),
        Case("GET", lambda i: "/api/v1/users/me/tokens", lambda role: None, frozenset(principals)),
    ]

    for case in cases:
        path = case.path(ids)
        for role, secret in principals.items():
            payload = case.body(role)
            resp = client.request(case.method, path, json=payload, headers=auth(secret))
            if role in case.allowed:
                assert resp.status_code < 300, (
                    case.method,
                    path,
                    role,
                    resp.status_code,
                    resp.text,
                )
            else:
                assert resp.status_code in (403, 404), (
                    case.method,
                    path,
                    role,
                    resp.status_code,
                    resp.text,
                )


async def test_delete_permissions(client, committed) -> None:
    org, team, project = await org_tree(committed, prefix="delete")
    empty_owner = Team(org_id=org.id, name=f"empty-owner-{suffix()}")
    empty_admin = Team(org_id=org.id, name=f"empty-admin-{suffix()}")
    empty_denied = Team(org_id=org.id, name=f"empty-denied-{suffix()}")
    empty_project = Project(org_id=org.id, team_id=team.id, name=f"empty-project-{suffix()}")
    empty_project2 = Project(org_id=org.id, team_id=team.id, name=f"empty-project2-{suffix()}")
    committed.add_all([empty_owner, empty_admin, empty_denied, empty_project, empty_project2])
    await committed.flush()
    owner, owner_secret = await user_with_token(committed)
    admin, admin_secret = await user_with_token(committed)
    team_admin, team_admin_secret = await user_with_token(committed)
    member, member_secret = await user_with_token(committed)
    await grant(committed, owner, org, role="owner")
    await grant(committed, admin, org, role="admin")
    await grant(committed, team_admin, org, team=team, role="admin")
    await grant(committed, team_admin, org, team=empty_denied, role="admin")
    await grant(committed, member, org, team=team, role="member")
    await committed.commit()

    # Org delete is installation-superuser only.
    assert client.delete(f"/api/v1/orgs/{org.id}", headers=auth(owner_secret)).status_code == 403
    assert client.delete(f"/api/v1/orgs/{org.id}", headers=auth(admin_secret)).status_code == 403

    # Team delete: org owner/admin (not team admin).
    resp = client.delete(f"/api/v1/teams/{empty_denied.id}", headers=auth(team_admin_secret))
    assert resp.status_code == 403
    resp = client.delete(f"/api/v1/teams/{empty_owner.id}", headers=auth(owner_secret))
    assert resp.status_code == 204
    resp = client.delete(f"/api/v1/teams/{empty_admin.id}", headers=auth(admin_secret))
    assert resp.status_code == 204

    # Project delete: team admin allowed, member not.
    resp = client.delete(f"/api/v1/projects/{empty_project.id}", headers=auth(team_admin_secret))
    assert resp.status_code == 204
    resp = client.delete(f"/api/v1/projects/{empty_project2.id}", headers=auth(member_secret))
    assert resp.status_code == 403

"""Exact HTTP authorization matrix: readable-but-forbidden is 403, invisible is 404."""

import uuid

import pytest

from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.models import PersonalAccessToken, TeamRole
from lumewarp_control_plane.security import SecretKind, generate_secret
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_service_account,
    make_team,
    make_user,
)

ROLES = ("installation_admin", "admin", "member", "other_team", "bootstrap")
READ = (200, 200, 200, 404, 200)
# A team admin reads the whole-team views; a plain member has no audit view.
ADMIN_READ = (200, 200, 403, 404, 200)
ADMIN = (200, 200, 403, 404, 200)
INSTALLATION = (200, 403, 403, 403, 200)
ANY = (200, 200, 200, 200, 200)
# A key owned by another member is admin-or-owner only; a foreign team stays a 404.
OWN_KEY = (200, 200, 403, 404, 200)

# name, method, path, body, exact response by ROLES order.
CASES = [
    ("teams.list", "GET", "/teams", None, ANY),
    ("teams.create", "POST", "/teams", {"name": "new-{mark}"}, (201, 403, 403, 403, 201)),
    ("teams.read", "GET", "/teams/{team}", None, READ),
    ("teams.update", "PATCH", "/teams/{team}", {"name": "updated-{mark}"}, ADMIN),
    ("teams.quota", "PATCH", "/teams/{team}", {"rpm_limit": 5}, (200, 403, 403, 404, 200)),
    ("teams.delete", "DELETE", "/teams/{team}", None, (204, 403, 403, 404, 204)),
    ("members.list", "GET", "/teams/{team}/members", None, ADMIN_READ),
    ("members.put", "PUT", "/teams/{team}/members/{target}", {"role": "member"}, ADMIN),
    ("members.patch", "PATCH", "/teams/{team}/members/{target}", {"rpm_limit": 10}, ADMIN),
    ("members.delete", "DELETE", "/teams/{team}/members/{target}", None, (204, 204, 403, 404, 204)),
    ("sa.list", "GET", "/teams/{team}/service-accounts", None, ADMIN_READ),
    (
        "sa.create",
        "POST",
        "/teams/{team}/service-accounts",
        {"name": "new"},
        (201, 201, 403, 404, 201),
    ),
    ("sa.read", "GET", "/service-accounts/{sa}", None, ADMIN_READ),
    ("sa.patch", "PATCH", "/service-accounts/{sa}", {"description": "updated"}, ADMIN),
    ("sa.delete", "DELETE", "/service-accounts/{sa}", None, (204, 204, 403, 404, 204)),
    ("keys.list", "GET", "/teams/{team}/keys", None, READ),
    (
        "keys.create",
        "POST",
        "/teams/{team}/keys",
        {"name": "sa", "service_account_id": "{sa}"},
        (201, 201, 403, 404, 201),
    ),
    ("keys.read", "GET", "/keys/{key}", None, OWN_KEY),
    ("keys.patch", "PATCH", "/keys/{key}", {"name": "managed"}, ADMIN),
    ("keys.revoke", "POST", "/keys/{key}/revoke", None, ADMIN),
    ("keys.rotate", "POST", "/keys/{key}/rotate", None, ADMIN),
    ("keys.own", "GET", "/users/me/keys", None, ANY),
    ("users.list", "GET", "/users", None, INSTALLATION),
    ("users.create", "POST", "/users", {"email": "{mark}@example.com"}, (201, 403, 403, 403, 201)),
    ("users.read", "GET", "/users/{target}", None, INSTALLATION),
    ("users.patch", "PATCH", "/users/{target}", {"display_name": "updated"}, INSTALLATION),
    ("users.delete", "DELETE", "/users/{unused_user}", None, (204, 403, 403, 403, 204)),
    (
        "users.token",
        "POST",
        "/users/{target}/tokens",
        {"name": "issued"},
        (201, 403, 403, 403, 201),
    ),
    ("users.me", "GET", "/users/me", None, ANY),
    ("tokens.list", "GET", "/users/me/tokens", None, ANY),
    ("upstreams.list", "GET", "/upstreams", None, INSTALLATION),
    (
        "upstreams.create",
        "POST",
        "/upstreams",
        {"name": "up-{mark}", "kind": "vllm", "base_url": "http://127.0.0.1:9000/v1"},
        (201, 403, 403, 403, 201),
    ),
    ("models.list", "GET", "/models", None, INSTALLATION),
    ("routers.list", "GET", "/routers", None, INSTALLATION),
    ("catalog.list", "GET", "/catalog", None, ANY),
    ("gateways.list", "GET", "/gateways", None, INSTALLATION),
    (
        "gateways.create",
        "POST",
        "/gateways",
        {"gateway_id": "gw-{mark}"},
        (201, 403, 403, 403, 201),
    ),
    ("config.list", "GET", "/config/versions", None, INSTALLATION),
    ("usage.read", "GET", "/usage?team_id={team}", None, READ),
    ("audit.read", "GET", "/audit?team_id={team}", None, ADMIN_READ),
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
async def test_endpoint_authorization_matrix(client, committed, role, case) -> None:
    name, method, path, body, statuses = case
    team = await make_team(committed)
    caller, token = await user_with_token(
        committed, installation_admin=role == "installation_admin"
    )
    if role in ("admin", "member"):
        await make_member(committed, team, caller, role=TeamRole(role))
    elif role == "other_team":
        other = await make_team(committed)
        await make_member(committed, other, caller)
    target = await make_user(committed)
    await make_member(committed, team, target)
    if role != "admin":
        await make_member(committed, team, role=TeamRole.ADMIN)
    unused = await make_user(committed)
    ids = {
        "team": str(team.id),
        "target": str(target.id),
        "unused_user": str(unused.id),
        "mark": suffix(),
    }
    if name != "teams.delete":
        account = await make_service_account(committed, team)
        key = await make_key(committed, team, target)
        ids.update(sa=str(account.id), key=str(key.id))
    await committed.commit()
    response = client.request(
        method,
        "/api/v1" + path.format(**ids),
        json={k: v.format(**ids) if isinstance(v, str) else v for k, v in body.items()}
        if body is not None
        else None,
        headers=ADMIN_AUTH if role == "bootstrap" else auth(token),
    )
    assert response.status_code == statuses[ROLES.index(role)], (
        name,
        role,
        response.status_code,
        response.text,
    )


@pytest.mark.parametrize(
    "resource,existing",
    [
        ("team", 200),
        ("member", 403),
        ("sa", 403),
        ("key", 403),
        ("usage", 403),
        ("audit", 403),
    ],
)
async def test_unknown_is_404_and_unreadable_keeps_its_own_answer(
    client, committed, resource, existing
) -> None:
    team = await make_team(committed)
    owner = await make_user(committed)
    await make_member(committed, team, owner)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, owner)
    caller, token = await user_with_token(committed)
    await make_member(committed, team, caller)
    await committed.commit()
    for missing in (False, True):
        team_id = uuid.uuid4() if missing else team.id
        account_id = uuid.uuid4() if missing else account.id
        key_id = uuid.uuid4() if missing else key.id
        paths = {
            "team": f"/teams/{team_id}",
            "member": f"/teams/{team_id}/members",
            "sa": f"/service-accounts/{account_id}",
            "key": f"/keys/{key_id}",
            "usage": f"/usage?key_id={key_id}",
            "audit": f"/audit?team_id={team_id}",
        }
        response = client.get("/api/v1" + paths[resource], headers=auth(token))
        assert response.status_code == (404 if missing else existing), (
            resource,
            missing,
            response.text,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/teams",
        "/users",
        "/gateways",
        "/config/versions",
        "/usage",
        "/audit",
        "/users/me",
    ],
)
async def test_backend_error_preserves_identity_but_rejects_admin_actions(
    client, committed, monkeypatch, path
) -> None:
    _, token = await user_with_token(committed)
    await make_team(committed)
    await committed.commit()

    async def error(user_id, relation, resource):
        return Decision.ERROR

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(error, "unavailable"))
    response = client.get("/api/v1" + path, headers=auth(token))
    if path == "/users/me":
        assert response.status_code == 200, response.text
        assert response.json()["authz_unavailable"] is True
        assert response.json()["is_admin"] is False
    elif path in ("/users", "/gateways", "/config/versions"):
        # Installation-level actions are a local decision now: no role, no backend call.
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "forbidden"
    else:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "authz_unavailable"
    # Break-glass is an installation admin without the database or the backend.
    assert client.get("/api/v1" + path, headers=ADMIN_AUTH).status_code == 200


async def test_backend_error_during_team_visibility_is_503(client, committed, monkeypatch) -> None:
    team = await make_team(committed)
    _, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        return Decision.ERROR

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "unavailable"))
    for path in ("/teams", "/usage", "/audit", f"/teams/{team.id}"):
        response = client.get("/api/v1" + path, headers=auth(token))
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "authz_unavailable"


@pytest.mark.parametrize("path", ["/teams", "/users/me", "/upstreams", "/usage", "/audit"])
def test_authentication_is_required(client, path) -> None:
    assert client.get("/api/v1" + path).status_code == 401
    assert client.get("/api/v1" + path, headers=auth("invalid")).status_code == 401


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_missing_member_is_404_before_manage_permission(client, committed, method) -> None:
    team = await make_team(committed)
    caller, token = await user_with_token(committed)
    await make_member(committed, team, caller)
    target = await make_user(committed)
    await committed.commit()
    for user_id in (target.id, uuid.uuid4()):
        response = client.request(
            method,
            f"/api/v1/teams/{team.id}/members/{user_id}",
            json={"role": "member"} if method == "PATCH" else None,
            headers=auth(token),
        )
        assert response.status_code == 404, response.text
    response = client.request(
        method,
        f"/api/v1/teams/{team.id}/members/{caller.id}",
        json={"role": "member"} if method == "PATCH" else None,
        headers=auth(token),
    )
    assert response.status_code == 403, response.text


async def test_missing_or_foreign_service_account_is_404_before_key_permission(
    client, committed
) -> None:
    team = await make_team(committed)
    other = await make_team(committed)
    caller, token = await user_with_token(committed)
    await make_member(committed, team, caller)
    own = await make_service_account(committed, team)
    foreign = await make_service_account(committed, other)
    await committed.commit()
    for account_id, expected in ((own.id, 403), (foreign.id, 404), (uuid.uuid4(), 404)):
        response = client.post(
            f"/api/v1/teams/{team.id}/keys",
            json={"name": "service", "service_account_id": str(account_id)},
            headers=auth(token),
        )
        assert response.status_code == expected, response.text


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_a_pat_authorizes_exactly_like_a_session(client, committed, role) -> None:
    """Both credentials resolve to the same user id; no rule differs by credential kind."""
    team = await make_team(committed, rpm_limit=10)
    caller = await make_user(committed)
    colleague = await make_user(committed)
    await make_member(committed, team, caller, role=TeamRole(role))
    await make_member(committed, team, colleague)
    own_key = await make_key(committed, team, caller)
    other_key = await make_key(committed, team, colleague)
    session_token = await sessions.create_session(committed, caller.id, 3600)
    secret = generate_secret(SecretKind.PAT)
    committed.add(
        PersonalAccessToken(
            user_id=caller.id,
            name="parity",
            token_hash=secret.hash,
            display_prefix=secret.display_prefix,
        )
    )
    await committed.commit()
    credentials = (
        auth("session:" + session_token),
        auth(secret.secret),
    )
    requests = [
        ("GET", "/teams", None),
        ("GET", f"/teams/{team.id}", None),
        ("GET", f"/teams/{team.id}/members", None),
        ("GET", f"/teams/{team.id}/service-accounts", None),
        ("GET", f"/teams/{team.id}/keys", None),
        ("GET", f"/keys/{own_key.id}", None),
        ("GET", f"/keys/{other_key.id}", None),
        ("GET", f"/usage?team_id={team.id}", None),
        ("GET", f"/audit?team_id={team.id}", None),
        ("PATCH", f"/teams/{team.id}", {"name": f"pat-{suffix()}"}),
        ("PATCH", f"/teams/{team.id}", {"rpm_limit": 5}),
        ("GET", "/upstreams", None),
        ("GET", "/models", None),
        ("GET", "/routers", None),
        ("GET", "/strategies", None),
        ("GET", "/catalog", None),
    ]
    for method, path, body in requests:
        statuses = [
            client.request(method, "/api/v1" + path, json=body, headers=headers).status_code
            for headers in credentials
        ]
        assert statuses[0] == statuses[1], (method, path, statuses)
    # Spot-check the answer itself, not only that both agree.
    assert statuses[1] == 200  # /catalog is the last request


@pytest.mark.parametrize("failed_relation", [Relation.MEMBER, Relation.ADMIN])
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/teams/{team}/members/{target}", {"role": "admin"}),
        ("DELETE", "/teams/{team}/members/{target}", None),
        ("POST", "/teams/{team}/service-accounts", {"name": "unavailable"}),
        ("PATCH", "/keys/{key}", {"name": "unavailable"}),
    ],
)
async def test_mutation_backend_error_is_503_and_preserves_state(
    client, committed, monkeypatch, failed_relation, method, path, body
) -> None:
    team = await make_team(committed)
    caller, token = await user_with_token(committed)
    target = await make_user(committed)
    await make_member(committed, team, target)
    key = await make_key(committed, team, target, name="unchanged")
    await committed.commit()

    async def check(user_id, relation, resource):
        if resource.type == "group":
            return Decision.DENY
        if resource.id == str(team.id):
            return Decision.ERROR if relation is failed_relation else Decision.ALLOW
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    response = client.request(
        method,
        "/api/v1" + path.format(team=team.id, target=target.id, key=key.id),
        json=body,
        headers=auth(token),
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "authz_unavailable"
    stored = client.get(f"/api/v1/keys/{key.id}", headers=ADMIN_AUTH).json()
    assert (stored["name"], stored["lifecycle"]) == ("unchanged", "active")
    member_rows = client.get(f"/api/v1/teams/{team.id}/members", headers=ADMIN_AUTH).json()["items"]
    assert [(r["user_id"], r["role"]) for r in member_rows] == [(str(target.id), "member")]
    assert (
        client.get(f"/api/v1/teams/{team.id}/service-accounts", headers=ADMIN_AUTH).json()["items"]
        == []
    )

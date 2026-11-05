"""Self-service key limits: /users/me details, expiry, rotation grace and rate limits."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api import keys as keys_module
from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import (
    ApiKey,
    AuditLog,
    ModelAccess,
    PersonalAccessToken,
    ScopeType,
    TeamRole,
    member_scope_id,
)
from lumewarp_control_plane.security import SecretKind, generate_secret, hash_secret
from lumewarp_control_plane.windows import month_window
from tests.api.helpers import (
    ADMIN_AUTH,
    auth,
    suffix,
    usage_total,
    user_with_token,
)
from tests.factories import (
    make_member,
    make_service_account,
    make_team,
    make_user,
)


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _audit_count(session, team_ids, action: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.team_id.in_(team_ids), AuditLog.action == action)
    )


async def test_me_reports_effective_models_limits_and_budget(client, committed) -> None:
    names = [f"m-{suffix()}" for _ in range(3)]
    team = await make_team(
        committed,
        allowed_models=names[:2],
        model_access=ModelAccess.LIST,
        rpm_limit=100,
        tpm_limit=10_000,
        budget_nano_usd=1000,
    )
    other = await make_team(
        committed,
        allowed_models=[names[2]],
        model_access=ModelAccess.LIST,
        max_parallel=4,
    )
    # A universal team narrowed by the member proves /me never widens the member view.
    universal = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(
        committed,
        team,
        user,
        model_access=ModelAccess.LIST,
        allowed_models=names[:1],
        rpm_limit=10,
        tpm_limit=5000,
        budget_nano_usd=400,
    )
    await make_member(committed, other, user)
    await make_member(
        committed,
        universal,
        user,
        model_access=ModelAccess.LIST,
        allowed_models=[names[1]],
    )
    window_start, window_end = month_window(datetime.now(UTC))
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=window_start, cost_nano_usd=300)
    )
    committed.add(
        usage_total(
            ScopeType.MEMBER,
            member_scope_id(team.id, user.id),
            window_start=window_start,
            cost_nano_usd=50,
        )
    )
    await committed.commit()

    response = client.get("/api/v1/users/me", headers=auth(token))
    assert response.status_code == 200, response.text
    teams = {row["team_id"]: row for row in response.json()["teams"]}
    narrow = teams[str(team.id)]
    assert narrow["models"] == [names[0]]
    assert narrow["all_models"] is False
    assert narrow["limits"] == {"rpm_limit": 10, "tpm_limit": 5000, "max_parallel": None}
    assert narrow["key_limits"] == {
        "max_keys_per_member": None,
        "max_member_key_lifetime_seconds": None,
        "key_count": 0,
    }
    budget = narrow["budget"]
    # A plain member gets the member scope and the effective remaining amount, never the
    # team's budget or the team's spend.
    assert (budget["team_budget_nano_usd"], budget["team_spent_nano_usd"]) == (None, None)
    assert (budget["member_budget_nano_usd"], budget["member_spent_nano_usd"]) == (400, 50)
    assert budget["remaining_nano_usd"] == 350
    assert parse_ts(budget["window_start"]) == window_start
    assert parse_ts(budget["window_end"]) == window_end
    # The team's second model and the other team's model must not leak in.
    assert names[1] not in narrow["models"]
    assert names[2] not in narrow["models"]

    sibling = teams[str(other.id)]
    assert sibling["models"] == [names[2]]
    assert sibling["limits"] == {"rpm_limit": None, "tpm_limit": None, "max_parallel": 4}
    assert sibling["budget"]["remaining_nano_usd"] is None

    narrowed_all = teams[str(universal.id)]
    assert narrowed_all["models"] == [names[1]]
    assert narrowed_all["all_models"] is False


async def _credential_headers(
    session: AsyncSession, user_id: uuid.UUID, kind: str
) -> dict[str, str]:
    if kind == "session":
        token = await sessions.create_session(session, user_id, 3600)
        return auth("session:" + token)
    new = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user_id,
            name="me-budget",
            token_hash=new.hash,
            display_prefix=new.display_prefix,
        )
    )
    await session.flush()
    return auth(new.secret)


@pytest.mark.parametrize("role", ["member", "admin"])
@pytest.mark.parametrize("credential", ["session", "pat"])
async def test_me_discloses_the_team_scope_to_admins_only(
    client, committed, role, credential
) -> None:
    """The team budget and spend are the team-admin view, with either credential kind."""
    team = await make_team(committed, budget_nano_usd=1000)
    user = await make_user(committed)
    await make_member(committed, team, user, role=TeamRole(role), budget_nano_usd=400)
    window_start, _ = month_window(datetime.now(UTC))
    committed.add(
        usage_total(ScopeType.TEAM, team.id, window_start=window_start, cost_nano_usd=900)
    )
    committed.add(
        usage_total(
            ScopeType.MEMBER,
            member_scope_id(team.id, user.id),
            window_start=window_start,
            cost_nano_usd=50,
        )
    )
    headers = await _credential_headers(committed, user.id, credential)
    await committed.commit()

    response = client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200, response.text
    [row] = response.json()["teams"]
    budget = row["budget"]
    # The effective minimum stays computed server-side: MIN(1000-900, 400-50).
    assert budget["remaining_nano_usd"] == 100
    assert (budget["member_budget_nano_usd"], budget["member_spent_nano_usd"]) == (400, 50)
    if role == "admin":
        assert (budget["team_budget_nano_usd"], budget["team_spent_nano_usd"]) == (1000, 900)
    else:
        assert (budget["team_budget_nano_usd"], budget["team_spent_nano_usd"]) == (
            None,
            None,
        )


async def test_me_matches_the_compiled_snapshot_after_team_narrowing(
    client, committed, api_engine
) -> None:
    """A member left at ALL must follow the team's new LIST, not the whole catalog."""
    name = f"m-{suffix()}"
    team = await make_team(committed, allowed_models=[name])
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, model_access=ModelAccess.ALL)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": "compiled"}, headers=auth(token)
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]
    narrowed = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"model_access": "list", "allowed_models": [name]},
        headers=ADMIN_AUTH,
    )
    assert narrowed.status_code == 200, narrowed.text

    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        compiled = await compile_scope(session, "api-test", require_local_membership=True)
    published = next(k for k in compiled.snapshot.api_keys if k.key_id == key_id)
    me = client.get("/api/v1/users/me", headers=auth(token))
    assert me.status_code == 200, me.text
    row = next(r for r in me.json()["teams"] if r["team_id"] == str(team.id))
    assert row["all_models"] is published.policy.allow_all_models
    assert row["models"] == list(published.policy.allowed_models)
    assert row["all_models"] is False
    assert row["models"] == [name]


async def test_me_external_member_uses_only_the_team_policy(client, committed, monkeypatch) -> None:
    name = f"m-{suffix()}"
    team = await make_team(
        committed, allowed_models=[name], model_access=ModelAccess.LIST, rpm_limit=7
    )
    user, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if resource.type == "group":
            return Decision.DENY
        if resource.id == str(team.id) and relation is Relation.MEMBER:
            return Decision.ALLOW
        return Decision.DENY

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    response = client.get("/api/v1/users/me", headers=auth(token))
    assert response.status_code == 200, response.text
    row = response.json()["teams"][0]
    assert (row["team_id"], row["role"]) == (str(team.id), "member")
    assert row["models"] == [name]
    assert row["limits"]["rpm_limit"] == 7
    assert row["budget"]["member_budget_nano_usd"] is None


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_create_enforces_expiry_limit_and_member_key_count(client, committed, role) -> None:
    team = await make_team(committed, max_keys_per_member=2, max_member_key_lifetime_seconds=3600)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole(role))
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"

    before = datetime.now(UTC)
    created = client.post(path, json={"name": "first"}, headers=auth(token))
    assert created.status_code == 201, created.text
    expires = parse_ts(created.json()["key"]["expires_at"])
    after = datetime.now(UTC)
    assert expires is not None
    assert before + timedelta(seconds=3600) <= expires <= after + timedelta(seconds=3601)

    too_long = client.post(
        path,
        json={
            "name": "too long",
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
        headers=auth(token),
    )
    assert too_long.status_code == 422, too_long.text
    within = client.post(
        path,
        json={"name": "within", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        headers=auth(token),
    )
    assert within.status_code == 201, within.text
    third = client.post(path, json={"name": "third"}, headers=auth(token))
    assert third.status_code == 409, third.text
    assert "member key limit" in third.json()["error"]["message"]
    me = client.get("/api/v1/users/me", headers=auth(token)).json()["teams"][0]
    assert me["key_limits"] == {
        "max_keys_per_member": 2,
        "max_member_key_lifetime_seconds": 3600,
        "key_count": 2,
    }

    # Revoking frees a slot; suspended keys still count.
    assert (
        client.post(
            f"/api/v1/keys/{created.json()['key']['id']}/revoke", headers=auth(token)
        ).status_code
        == 200
    )
    assert client.post(path, json={"name": "after revoke"}, headers=auth(token)).status_code == 201

    # Service-account keys are team-admin managed, not member keys.
    account = await make_service_account(committed, team)
    await committed.commit()
    service = client.post(
        path, json={"name": "service", "service_account_id": str(account.id)}, headers=ADMIN_AUTH
    )
    assert service.status_code == 201, service.text
    assert service.json()["key"]["expires_at"] is None


async def test_rotate_enforces_lifetime_and_keeps_grace(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": "rotate"}, headers=auth(token)
    ).json()
    key_path = f"/api/v1/keys/{created['key']['id']}"
    assert created["key"]["expires_at"] is None

    # Set the cap after creation: a later rotation must add the expiry.
    assert (
        client.patch(
            f"/api/v1/teams/{team.id}",
            json={"max_member_key_lifetime_seconds": 3600},
            headers=ADMIN_AUTH,
        ).status_code
        == 200
    )
    rotated = client.post(key_path + "/rotate", headers=auth(token))
    assert rotated.status_code == 200, rotated.text
    assert parse_ts(rotated.json()["key"]["expires_at"]) is not None

    # Grace is capped at 24 hours and cannot be negative.
    for grace in (-1, 24 * 3600 + 1):
        assert (
            client.post(
                key_path + "/rotate", json={"grace_seconds": grace}, headers=auth(token)
            ).status_code
            == 422
        )
    # A grace rotation keeps the old hash and its own deadline.
    before = datetime.now(UTC)
    graced = client.post(key_path + "/rotate", json={"grace_seconds": 120}, headers=auth(token))
    assert graced.status_code == 200, graced.text
    previous_expires = parse_ts(graced.json()["key"]["previous_key_expires_at"])
    assert previous_expires is not None
    assert (
        before + timedelta(seconds=120)
        <= previous_expires
        <= datetime.now(UTC) + timedelta(seconds=121)
    )
    row = await committed.get(ApiKey, created["key"]["id"])
    await committed.refresh(row)
    assert row.previous_key_hash == hash_secret(rotated.json()["secret"])
    assert row.key_hash == hash_secret(graced.json()["secret"])

    # Rotating twice inside the grace drops the older old secret; only one remains.
    twice = client.post(key_path + "/rotate", json={"grace_seconds": 120}, headers=auth(token))
    assert twice.status_code == 200, twice.text
    await committed.refresh(row)
    assert row.previous_key_hash == hash_secret(graced.json()["secret"])
    assert row.previous_key_hash != hash_secret(rotated.json()["secret"])
    assert row.key_hash == hash_secret(twice.json()["secret"])

    # Revoking kills both hashes as well as the key.
    assert client.post(key_path + "/revoke", headers=auth(token)).status_code == 200
    await committed.refresh(row)
    assert (row.previous_key_hash, row.previous_key_expires_at) == (None, None)


async def test_rotate_rejects_an_expiry_beyond_the_cap_even_for_admins(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys",
        json={
            "name": "late",
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
        headers=auth(token),
    ).json()
    key_path = f"/api/v1/keys/{created['key']['id']}"
    assert (
        client.patch(
            f"/api/v1/teams/{team.id}",
            json={"max_member_key_lifetime_seconds": 3600},
            headers=ADMIN_AUTH,
        ).status_code
        == 200
    )
    # An owner cannot edit around the cap either.
    assert client.patch(key_path, json={"expires_at": None}, headers=auth(token)).status_code == 422
    assert (
        client.patch(
            key_path,
            json={"expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat()},
            headers=auth(token),
        ).status_code
        == 422
    )
    # An owner and an installation admin are both refused; shortening the expiry unblocks.
    assert client.post(key_path + "/rotate", headers=auth(token)).status_code == 422
    assert client.post(key_path + "/rotate", headers=ADMIN_AUTH).status_code == 422
    assert (
        client.patch(
            key_path,
            json={"expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat()},
            headers=auth(token),
        ).status_code
        == 200
    )
    assert client.post(key_path + "/rotate", headers=auth(token)).status_code == 200
    assert client.post(key_path + "/rotate", headers=ADMIN_AUTH).status_code == 200


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_rate_limits_are_per_principal_not_per_target_and_429_is_not_audited(
    client, committed, role
) -> None:
    teams = [await make_team(committed) for _ in range(2)]
    user, token = await user_with_token(committed)
    for team in teams:
        await make_member(committed, team, user, role=TeamRole(role))
    await committed.commit()
    headers = auth(token)
    team_ids = [team.id for team in teams]

    key_ids = []
    for i in range(keys_module.KEY_CREATE_LIMIT):
        team = teams[i % len(teams)]
        response = client.post(
            f"/api/v1/teams/{team.id}/keys", json={"name": f"many-{i}"}, headers=headers
        )
        assert response.status_code == 201, response.text
        key_ids.append(response.json()["key"]["id"])
    created_audits = await _audit_count(committed, team_ids, "key.create")
    assert created_audits == keys_module.KEY_CREATE_LIMIT
    # A different team does not buy more create quota.
    for team in teams:
        limited = client.post(
            f"/api/v1/teams/{team.id}/keys", json={"name": "too many"}, headers=headers
        )
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1
    assert await _audit_count(committed, team_ids, "key.create") == created_audits

    # A different key does not buy more rotate quota.
    for _ in range(keys_module.KEY_ROTATE_LIMIT):
        assert client.post(f"/api/v1/keys/{key_ids[0]}/rotate", headers=headers).status_code == 200
    rotated_audits = await _audit_count(committed, team_ids, "key.rotate")
    assert rotated_audits == keys_module.KEY_ROTATE_LIMIT
    limited_rotate = client.post(f"/api/v1/keys/{key_ids[1]}/rotate", headers=headers)
    assert limited_rotate.status_code == 429, limited_rotate.text
    assert limited_rotate.json()["error"]["code"] == "rate_limited"
    # 429s are metric-only: no audit row for the refusal.
    assert await _audit_count(committed, team_ids, "key.rotate") == rotated_audits

    metrics = client.app.state.metrics
    assert metrics.value(
        "lumewarp_cp_key_writes_total", {"operation": "create", "result": "rate_limited"}
    ) == len(teams)
    assert (
        metrics.value(
            "lumewarp_cp_key_writes_total", {"operation": "rotate", "result": "rate_limited"}
        )
        == 1
    )


@pytest.mark.parametrize("caller", ["break_glass", "group_admin"])
async def test_installation_admin_and_break_glass_bypass_the_limiter(
    client, committed, monkeypatch, caller
) -> None:
    monkeypatch.setattr(keys_module, "KEY_CREATE_LIMIT", 2)
    monkeypatch.setattr(keys_module, "KEY_ROTATE_LIMIT", 2)
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    if caller == "group_admin":
        _, token = await user_with_token(committed, group_admin=True)
        headers = auth(token)
    else:
        headers = ADMIN_AUTH
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"
    key_ids = []
    for i in range(keys_module.KEY_CREATE_LIMIT + 1):
        response = client.post(
            path,
            json={"name": f"admin-{i}", "service_account_id": str(account.id)},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        key_ids.append(response.json()["key"]["id"])
    for _ in range(keys_module.KEY_ROTATE_LIMIT + 1):
        assert client.post(f"/api/v1/keys/{key_ids[0]}/rotate", headers=headers).status_code == 200
    limiter = getattr(client.app.state, "key_write_limiter", None)
    assert limiter is None or not limiter.entries


async def test_unknown_targets_allocate_no_limiter_entries(client, committed) -> None:
    user, token = await user_with_token(committed)
    await committed.commit()
    headers = auth(token)
    for _ in range(5):
        unknown_team = uuid.uuid4()
        response = client.post(
            f"/api/v1/teams/{unknown_team}/keys", json={"name": "unknown"}, headers=headers
        )
        assert response.status_code == 404, response.text
        response = client.post(f"/api/v1/keys/{uuid.uuid4()}/rotate", headers=headers)
        assert response.status_code == 404, response.text
    limiter = client.app.state.key_write_limiter
    assert set(limiter.entries) == {f"create:{user.id}", f"rotate:{user.id}"}


def test_limiter_window_resets_without_rebuilding_the_table() -> None:
    limiter = keys_module.KeyWriteLimiter(window_seconds=0.05)
    for _ in range(keys_module.KEY_CREATE_LIMIT):
        assert limiter.reserve("create:user", keys_module.KEY_CREATE_LIMIT) == 0
    assert limiter.reserve("create:user", keys_module.KEY_CREATE_LIMIT) >= 1
    time.sleep(0.06)
    assert limiter.reserve("create:user", keys_module.KEY_CREATE_LIMIT) == 0
    assert len(limiter.entries) == 1


def test_full_limiter_refuses_a_new_scope_without_evicting_live_counters() -> None:
    limiter = keys_module.KeyWriteLimiter(capacity=2, window_seconds=60)
    assert limiter.reserve("create:a", keys_module.KEY_CREATE_LIMIT) == 0
    assert limiter.reserve("create:b", keys_module.KEY_CREATE_LIMIT) == 0
    assert limiter.reserve("create:c", keys_module.KEY_CREATE_LIMIT) >= 1
    assert set(limiter.entries) == {"create:a", "create:b"}
    for _ in range(keys_module.KEY_CREATE_LIMIT - 1):
        assert limiter.reserve("create:a", keys_module.KEY_CREATE_LIMIT) == 0
    assert limiter.reserve("create:a", keys_module.KEY_CREATE_LIMIT) >= 1


async def test_lowering_the_count_cap_refuses_create_but_still_allows_rotate(
    client, committed
) -> None:
    team = await make_team(committed, max_keys_per_member=3)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    path = f"/api/v1/teams/{team.id}/keys"
    key_ids = []
    for i in range(3):
        response = client.post(path, json={"name": f"existing-{i}"}, headers=auth(token))
        assert response.status_code == 201, response.text
        key_ids.append(response.json()["key"]["id"])
    lowered = client.patch(
        f"/api/v1/teams/{team.id}", json={"max_keys_per_member": 1}, headers=ADMIN_AUTH
    )
    assert lowered.status_code == 200, lowered.text
    refused = client.post(path, json={"name": "over the cap"}, headers=auth(token))
    assert refused.status_code == 409, refused.text
    # Rotation does not add a key, so the count cap does not refuse it.
    for key_id in key_ids:
        assert client.post(f"/api/v1/keys/{key_id}/rotate", headers=auth(token)).status_code == 200
    assert (
        client.post(path, json={"name": "still over the cap"}, headers=auth(token)).status_code
        == 409
    )


async def test_audit_keeps_the_key_limit_and_grace_details(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": "audited"}, headers=auth(token)
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["key"]["id"]
    rotated = client.post(
        f"/api/v1/keys/{key_id}/rotate", json={"grace_seconds": 60}, headers=auth(token)
    )
    assert rotated.status_code == 200, rotated.text
    updated = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"max_keys_per_member": 3, "max_member_key_lifetime_seconds": 3600},
        headers=ADMIN_AUTH,
    )
    assert updated.status_code == 200, updated.text

    rows = (await committed.scalars(select(AuditLog).where(AuditLog.team_id == team.id))).all()
    by_action = {row.action: row.details for row in rows}
    assert "expires_at" in by_action["key.create"]
    assert by_action["key.rotate"]["grace_seconds"] == 60
    assert by_action["key.rotate"]["previous_key_expires_at"]
    assert by_action["team.update"]["changes"] == {
        "max_keys_per_member": 3,
        "max_member_key_lifetime_seconds": 3600,
    }

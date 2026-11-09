"""Self-service gate: Bob's real journey through Keycloak, the admin API and a gateway.

Bob is a plain member with no installation or team admin rights. He signs in to the real
dev Keycloak, reads what his team allows (`GET /users/me`), creates, rotates (with and
without a grace), suspends, resumes and revokes his own keys, and is refused everything
outside his own membership and the team's member-key limits. Every step is checked
against the gateway: each assertion waits for the compiled snapshot by polling the
gateway's answer, never by sleeping.

Runs on both authorization backends under `scripts/e2e-identity.sh` (the parametrized fixture),
and is not collected by `scripts/e2e-core.sh`, which has no IdP.
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from no_secret import assert_no_secret
from identity import cookie_api, grant, identity_stack, login
from secret_scan import scan_text
from stack import pg, record_secret, wait_for

TEAM_NAME = "self-service"
# Team upper bounds: lifetime one hour, two member keys. The key Bob creates initially
# outlives every rotation below.
LIFETIME = 3600
MAX_KEYS = 2
TEAM_BUDGET = 50_000_000_000
MEMBER_BUDGET = 20_000_000_000
RPM_LIMIT = 6000
TPM_LIMIT = 1_000_000_000
KEY_TTL = 1800
# The upstream key is referenced by name, never stored: the harness records its value and
# the pg_dump scan looks for it.
UPSTREAM_KEY_REF = "E2E_SELF_SERVICE_UPSTREAM_API_KEY"
UPSTREAM_KEY_VALUE = "self-service-upstream-env-secret-0d5f6a9b"


def wait_chat(gw, secret, status, *, model="mock", timeout=20, what=None):
    wait_for(
        lambda: gw.chat(secret, model=model).status_code == status,
        timeout,
        what or f"gateway answer {status}",
        interval=0.05,
    )


def key_row(stack, key_id):
    return tuple(
        pg(
            stack.dsn,
            "SELECT updated_at, previous_key_expires_at, key_hash, previous_key_hash"
            " FROM api_keys WHERE id = $1",
            uuid.UUID(key_id),
        )[0]
    )


@pytest.fixture(scope="module", params=["builtin", "openfga"])
def self_service(request, binaries):
    os.environ[UPSTREAM_KEY_REF] = UPSTREAM_KEY_VALUE
    try:
        with identity_stack(request.param, label="self-service") as state:
            stack = state.stack
            state.issued_secrets = []
            record_secret(UPSTREAM_KEY_VALUE, f"upstream API key from env {UPSTREAM_KEY_REF}")
            upstream = stack.ok(
                "POST",
                "/upstreams",
                json={
                    "name": "mock",
                    "kind": "openai_compatible",
                    "base_url": f"http://127.0.0.1:{stack.mock_port}/v1",
                    "api_key_ref": UPSTREAM_KEY_REF,
                },
            )
            state.model = stack.ok(
                "POST",
                "/models",
                json={
                    "name": "mock-a",
                    "model_type": "llm",
                    "upstream_id": upstream["id"],
                    "upstream_model": "mock",
                    "price_input_nano_usd_per_mtok": 1_000_000_000,
                    "price_output_nano_usd_per_mtok": 2_000_000_000,
                },
            )
            state.other_model = stack.ok(
                "POST",
                "/models",
                json={
                    "name": "other-a",
                    "model_type": "llm",
                    "upstream_id": upstream["id"],
                    "upstream_model": "mock",
                    "price_input_nano_usd_per_mtok": 1_000_000_000,
                    "price_output_nano_usd_per_mtok": 2_000_000_000,
                },
            )
            stack.ok(
                "POST",
                "/routers",
                json={"name": "mock", "members": [{"model_id": state.model["id"]}]},
            )
            stack.ok(
                "POST",
                "/routers",
                json={"name": "other", "members": [{"model_id": state.other_model["id"]}]},
            )
            state.team = stack.ok(
                "POST",
                "/teams",
                json={
                    "name": TEAM_NAME,
                    "org_id": stack.org_id,
                    "model_access": "list",
                    "allowed_models": ["mock"],
                    "rpm_limit": RPM_LIMIT,
                    "tpm_limit": TPM_LIMIT,
                    "max_parallel": 16,
                    "budget_nano_usd": TEAM_BUDGET,
                    "max_keys_per_member": MAX_KEYS,
                    "max_member_key_lifetime_seconds": LIFETIME,
                },
            )
            grant(state, state.user, "member")
            # Alice only exists here to own a key Bob may not touch.
            grant(state, state.admin_user, "admin")
            if state.backend == "builtin":
                cookie_api(
                    state.admin,
                    stack,
                    "PATCH",
                    f"/teams/{state.team['id']}/members/{state.user['id']}",
                    json={"budget_nano_usd": MEMBER_BUDGET},
                )
            state.gateway = stack.start_gateway()
            wait_for(
                lambda: (stack.gateway_status() or {}).get("acked_version")
                == stack.current_version(),
                20,
                "gateway ACKed the self-service config",
            )
            yield state
    finally:
        os.environ.pop(UPSTREAM_KEY_REF, None)


@pytest.fixture(scope="module")
def foreign_team(self_service):
    return self_service.stack.ok(
        "POST", "/teams", json={"name": "not-bobs", "org_id": self_service.stack.org_id}
    )["id"]


@pytest.fixture(scope="module")
def own_key(self_service):
    stack = self_service.stack
    expires = datetime.now(UTC) + timedelta(seconds=KEY_TTL)
    created = cookie_api(
        self_service.member,
        stack,
        "POST",
        f"/teams/{self_service.team['id']}/keys",
        json={"name": "bob-self-service", "expires_at": expires.isoformat()},
    )
    self_service.issued_secrets.append(created["secret"])
    assert created["key"]["owner_kind"] == "user"
    assert created["key"]["user_id"] == self_service.user["id"]
    assert datetime.fromisoformat(created["key"]["expires_at"]) == expires
    wait_chat(self_service.gateway, created["secret"], 200, what="Bob's new key on the gateway")
    return {"key": created["key"], "secret": created["secret"], "expires": expires}


@pytest.fixture(scope="module")
def rotated(self_service, own_key):
    stack, gw = self_service.stack, self_service.gateway
    key_id = own_key["key"]["id"]
    response = cookie_api(
        self_service.member,
        stack,
        "POST",
        f"/keys/{key_id}/rotate",
        json={"grace_seconds": 3600},
    )
    self_service.issued_secrets.append(response["secret"])
    assert response["key"]["id"] == key_id
    previous_expires = datetime.fromisoformat(response["key"]["previous_key_expires_at"])
    assert previous_expires > datetime.now(UTC)
    wait_chat(gw, response["secret"], 200, what="rotated secret on the gateway")
    # The replaced secret keeps working inside the grace.
    assert gw.chat(own_key["secret"]).status_code == 200

    # Both secrets are the same key: every usage row carries the one key_id.
    new_call = gw.chat(response["secret"])
    old_call = gw.chat(own_key["secret"])
    request_ids = {
        new_call.headers["x-lumewarp-request-id"],
        old_call.headers["x-lumewarp-request-id"],
    }

    def rows():
        return [
            row
            for row in stack.usage_rows()
            if str(row["key_id"]) == key_id and str(row["request_id"]) in request_ids
        ]

    wait_for(lambda: len(rows()) == 2, 20, "usage rows for both rotation secrets")
    assert {str(row["key_id"]) for row in rows()} == {key_id}
    return {
        "key_id": key_id,
        "old": own_key["secret"],
        "new": response["secret"],
        "previous_expires": previous_expires,
        "rows": rows(),
    }


@pytest.fixture(scope="module")
def rotated_twice(self_service, own_key, rotated):
    stack, gw = self_service.stack, self_service.gateway
    key_id = own_key["key"]["id"]
    response = cookie_api(
        self_service.member,
        stack,
        "POST",
        f"/keys/{key_id}/rotate",
        json={"grace_seconds": 3600},
    )
    self_service.issued_secrets.append(response["secret"])
    wait_chat(gw, response["secret"], 200, what="second rotated secret on the gateway")
    # Only one previous hash is kept: the older secret stops at once ...
    wait_chat(gw, rotated["old"], 401, what="older secret rejected while its successor graces")
    # ... and the secret that was in grace is now the one in grace.
    assert gw.chat(rotated["new"]).status_code == 200
    previous_expires = datetime.fromisoformat(response["key"]["previous_key_expires_at"])
    assert previous_expires > datetime.now(UTC)
    return {"new": response["secret"], "previous_expires": previous_expires}


@pytest.fixture(scope="module")
def short_grace(self_service, own_key, rotated_twice):
    stack, gw = self_service.stack, self_service.gateway
    key_id = own_key["key"]["id"]
    response = cookie_api(
        self_service.member,
        stack,
        "POST",
        f"/keys/{key_id}/rotate",
        json={"grace_seconds": 5},
    )
    self_service.issued_secrets.append(response["secret"])
    deadline = datetime.fromisoformat(response["key"]["previous_key_expires_at"])
    assert 0 < (deadline - datetime.now(UTC)).total_seconds() <= 5.5
    wait_chat(gw, response["secret"], 200, what="new secret after the short-grace rotation")
    # The secret it replaced (the one that was in grace) is accepted inside the grace.
    assert gw.chat(rotated_twice["new"]).status_code == 200
    assert datetime.now(UTC) < deadline

    # From here nothing writes to the control plane. The snapshot carries the deadline;
    # the gateway has to enforce it by itself.
    version = stack.current_version()
    before = key_row(stack, key_id)
    statuses = []

    def refused():
        status = gw.chat(rotated_twice["new"]).status_code
        statuses.append(status)
        return status == 401

    wait_for(refused, 20, "previous secret refused when its grace ends", interval=0.05)
    refused_at = datetime.now(UTC)
    assert set(statuses) <= {200, 401}, statuses
    assert refused_at >= deadline, (refused_at, deadline)
    assert stack.current_version() == version, "the control plane wrote while the grace ended"
    assert key_row(stack, key_id) == before, "the control plane wrote while the grace ended"
    # The current secret is still fine.
    assert gw.chat(response["secret"]).status_code == 200
    return {"new": response["secret"], "deadline": deadline, "refused_at": refused_at}


@pytest.fixture(scope="module")
def no_grace(self_service, own_key, short_grace):
    stack, gw = self_service.stack, self_service.gateway
    key_id = own_key["key"]["id"]
    response = cookie_api(
        self_service.member,
        stack,
        "POST",
        f"/keys/{key_id}/rotate",
        json={"grace_seconds": 0},
    )
    self_service.issued_secrets.append(response["secret"])
    assert response["key"]["previous_key_expires_at"] is None
    wait_chat(gw, response["secret"], 200, what="new secret after the no-grace rotation")
    wait_chat(
        gw,
        short_grace["new"],
        401,
        what="old secret rejected at once without a grace",
    )
    row = pg(
        stack.dsn,
        "SELECT previous_key_hash, previous_key_expires_at FROM api_keys WHERE id = $1",
        uuid.UUID(key_id),
    )[0]
    assert row["previous_key_hash"] is None and row["previous_key_expires_at"] is None
    return {"new": response["secret"]}


@pytest.fixture(scope="module")
def lifecycle(self_service, own_key, no_grace):
    stack, gw = self_service.stack, self_service.gateway
    key_id = own_key["key"]["id"]
    secret = no_grace["new"]
    result = {}
    suspended = cookie_api(
        self_service.member, stack, "PATCH", f"/keys/{key_id}", json={"lifecycle": "suspended"}
    )
    result["suspended"] = (suspended["lifecycle"], suspended["suspended_reason"])
    wait_chat(gw, secret, 401, what="suspended key rejected")
    result["suspended_error"] = gw.chat(secret).json()["error"]["code"]

    resumed = cookie_api(
        self_service.member, stack, "PATCH", f"/keys/{key_id}", json={"lifecycle": "active"}
    )
    result["resumed"] = (resumed["lifecycle"], resumed["suspended_reason"])
    wait_chat(gw, secret, 200, what="resumed key accepted")

    revoked = cookie_api(self_service.member, stack, "POST", f"/keys/{key_id}/revoke")
    result["revoked"] = (revoked["lifecycle"], revoked["revoked_at"] is not None)
    wait_chat(gw, secret, 401, what="revoked key rejected")
    result["rotate_after_revoke"] = self_service.member.post(
        stack.api_url + f"/keys/{key_id}/rotate", json={"grace_seconds": 0}
    )
    result["key"] = key_id
    return result


@pytest.fixture(scope="module")
def refused(self_service, foreign_team, lifecycle):
    stack, gw = self_service.stack, self_service.gateway
    team_id = self_service.team["id"]
    result = {}
    # Another member's key: not even metadata, and no management.
    alice = cookie_api(
        self_service.admin, stack, "POST", f"/teams/{team_id}/keys", json={"name": "alice-key"}
    )
    self_service.issued_secrets.append(alice["secret"])
    wait_chat(gw, alice["secret"], 200, what="Alice's key on the gateway")
    alice_id = alice["key"]["id"]
    result["alice_key_id"] = alice_id
    result["alice_secret"] = alice["secret"]
    result["alice"] = {
        "get": self_service.member.get(stack.api_url + f"/keys/{alice_id}").status_code,
        "patch": self_service.member.patch(
            stack.api_url + f"/keys/{alice_id}", json={"name": "bob-was-here"}
        ).status_code,
        "rotate": self_service.member.post(
            stack.api_url + f"/keys/{alice_id}/rotate", json={"grace_seconds": 0}
        ).status_code,
        "revoke": self_service.member.post(stack.api_url + f"/keys/{alice_id}/revoke").status_code,
        "still_serves": gw.chat(alice["secret"]).status_code,
    }
    # A team Bob is not in.
    result["foreign"] = {
        "create": self_service.member.post(
            stack.api_url + f"/teams/{foreign_team}/keys", json={"name": "nope"}
        ).status_code,
        "read": self_service.member.get(stack.api_url + f"/teams/{foreign_team}").status_code,
    }
    # An expiry past the team's maximum lifetime.
    too_far = datetime.now(UTC) + timedelta(seconds=LIFETIME + 30)
    late = self_service.member.post(
        stack.api_url + f"/teams/{team_id}/keys",
        json={"name": "too-long", "expires_at": too_far.isoformat()},
    )
    result["expiry"] = (
        late.status_code,
        late.json()["error"]["code"],
        late.json()["error"]["message"],
    )
    # More keys than max_keys_per_member. The revoked key no longer counts.
    count_before = cookie_api(self_service.member, stack, "GET", "/users/me")["teams"][0]["key_limits"][
        "key_count"
    ]
    created = []
    for name in ("second", "third"):
        key = cookie_api(
            self_service.member, stack, "POST", f"/teams/{team_id}/keys", json={"name": name}
        )
        self_service.issued_secrets.append(key["secret"])
        created.append(key)
    over = self_service.member.post(
        stack.api_url + f"/teams/{team_id}/keys", json={"name": "too-many"}
    )
    result["max_keys"] = {
        "count_before": count_before,
        "created": len(created),
        "over_status": over.status_code,
        "over_code": over.json()["error"]["code"],
        "count_after": cookie_api(self_service.member, stack, "GET", "/users/me")["teams"][0][
            "key_limits"
        ]["key_count"],
    }
    result["third_secret"] = created[-1]["secret"]
    result["third_key_id"] = created[-1]["key"]["id"]
    wait_chat(gw, result["third_secret"], 200, what="second key on the gateway")
    return result


@contextmanager
def clean_principal(self_service):
    """A logged-in member whose key-write limiter windows are both untouched.

    The limiter is per principal and per process (`KEY_WRITE_WINDOW_SECONDS`), and Bob has
    already created and rotated keys in this module, so the exact-quota tests need a
    principal whose first write is the one under test. Each call logs in a separate
    temporary Keycloak user (removed when the module ends) with no team grants, so every
    attempt is refused after the limiter and never writes a row.
    """
    with self_service.idp.temporary_user() as (username, _user_url):
        with httpx.Client(timeout=15, trust_env=False) as browser:
            login(browser, self_service.stack, username)
            yield browser


@pytest.fixture(scope="module")
def rotate_limited(self_service, refused):
    """Exhaust the rotate bucket through a target refused after the limiter.

    Alice's key is not readable by the clean principal, so every attempt stops at 404
    after the limiter has counted it, costing no rows and no rotation. The bucket starts
    empty, so exactly `KEY_ROTATE_LIMIT` attempts pass and the next is a 429; the create
    bucket must be untouched by it, and Alice's key may not change.
    """
    from lumewarp_control_plane.api.keys import KEY_ROTATE_LIMIT, KEY_WRITE_WINDOW_SECONDS

    stack = self_service.stack
    alice_id = refused["alice_key_id"]
    alice_before = tuple(
        pg(
            stack.dsn,
            "SELECT updated_at, key_hash, previous_key_hash FROM api_keys WHERE id = $1",
            uuid.UUID(alice_id),
        )[0]
    )
    with clean_principal(self_service) as browser:
        started = time.monotonic()
        statuses = []
        response = None
        # Exactly one attempt past the limit: the count is the product's, not a tolerance.
        for _ in range(KEY_ROTATE_LIMIT + 1):
            attempt = browser.post(
                stack.api_url + f"/keys/{alice_id}/rotate", json={"grace_seconds": 3600}
            )
            statuses.append(attempt.status_code)
            if attempt.status_code == 429:
                response = attempt
                break
        result = {
            "statuses": statuses,
            "elapsed": time.monotonic() - started,
            "limit": KEY_ROTATE_LIMIT,
            "window": KEY_WRITE_WINDOW_SECONDS,
            "allowed_before_429": sum(1 for status in statuses if status != 429),
            "code": response.json()["error"]["code"] if response is not None else None,
            "retry_after": response.headers.get("retry-after") if response is not None else None,
        }
        # One create attempt while rotate is exhausted: the buckets are separate, so this
        # is the foreign-team refusal from the target lookup, never a 429. It also proves
        # the exhausted rotate bucket did not leak into the create scope.
        create = browser.post(
            stack.api_url + f"/teams/{self_service.team['id']}/keys", json={"name": "after-rotate"}
        )
        result["create_status"] = create.status_code
        result["create_code"] = create.json().get("error", {}).get("code")
    result["alice_row_unchanged"] = (
        tuple(
            pg(
                stack.dsn,
                "SELECT updated_at, key_hash, previous_key_hash FROM api_keys WHERE id = $1",
                uuid.UUID(alice_id),
            )[0]
        )
        == alice_before
    )
    result["alice_still_serves"] = self_service.gateway.chat(refused["alice_secret"]).status_code
    return result


@pytest.fixture(scope="module")
def rate_limited(self_service, foreign_team, refused):
    from lumewarp_control_plane.api.keys import KEY_CREATE_LIMIT, KEY_WRITE_WINDOW_SECONDS

    stack = self_service.stack
    # A foreign team refuses the write after the limiter has counted it, so the clean
    # principal's create window can be exhausted without creating anything.
    with clean_principal(self_service) as browser:
        started = time.monotonic()
        statuses = []
        response = None
        for _ in range(KEY_CREATE_LIMIT + 1):
            attempt = browser.post(
                stack.api_url + f"/teams/{foreign_team}/keys", json={"name": "rate-limit"}
            )
            statuses.append(attempt.status_code)
            if attempt.status_code == 429:
                response = attempt
                break
        elapsed = time.monotonic() - started
        result = {
            "statuses": statuses,
            "elapsed": elapsed,
            "limit": KEY_CREATE_LIMIT,
            "window": KEY_WRITE_WINDOW_SECONDS,
            "allowed_before_429": sum(1 for status in statuses if status != 429),
            "code": response.json()["error"]["code"] if response is not None else None,
            "retry_after": response.headers.get("retry-after") if response is not None else None,
        }
    # The member was not written to: his existing key still serves.
    result["still_serves"] = self_service.gateway.chat(refused["third_secret"]).status_code
    return result


def test_01_users_me_lists_the_team_with_models_limits_and_budget(self_service):
    me = cookie_api(self_service.member, self_service.stack, "GET", "/users/me")
    assert me["user_id"] == self_service.user["id"]
    assert me["is_admin"] is False
    assert me["teams"], "Bob's team is missing from /users/me"
    [team] = me["teams"]
    assert (team["team_id"], team["team_name"], team["role"]) == (
        self_service.team["id"],
        TEAM_NAME,
        "member",
    )
    # The effective set is the team's allowlist, not the whole catalog.
    assert team["models"] == ["mock"]
    assert team["all_models"] is False
    assert team["limits"] == {
        "rpm_limit": RPM_LIMIT,
        "tpm_limit": TPM_LIMIT,
        "max_parallel": 16,
    }
    assert team["key_limits"] == {
        "max_keys_per_member": MAX_KEYS,
        "max_member_key_lifetime_seconds": LIFETIME,
        "key_count": 0,
    }
    budget = team["budget"]
    # Bob is a plain member: the team's budget and spend are the team-admin view.
    assert (budget["team_budget_nano_usd"], budget["team_spent_nano_usd"]) == (None, None)
    if self_service.backend == "builtin":
        assert budget["member_budget_nano_usd"] == MEMBER_BUDGET
        assert budget["member_spent_nano_usd"] == 0
        assert budget["remaining_nano_usd"] == min(TEAM_BUDGET, MEMBER_BUDGET)
    else:
        # Memberships live in OpenFGA; a member-level budget would be a local team_members
        # row, so only the team scope applies.
        assert budget["member_budget_nano_usd"] is None
        assert budget["remaining_nano_usd"] == TEAM_BUDGET
    now = datetime.now(UTC)
    window_start = datetime.fromisoformat(budget["window_start"])
    assert (window_start.year, window_start.month) == (now.year, now.month)
    assert datetime.fromisoformat(budget["window_end"]) > now


def test_02_own_key_authenticates_for_a_granted_model_only(self_service, own_key):
    gw = self_service.gateway
    allowed = gw.chat(own_key["secret"])
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["model"] == "mock"
    refused = gw.chat(own_key["secret"], model="other")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "model_not_allowed"
    assert "text/event-stream" in gw.chat(own_key["secret"], stream=True).headers["content-type"]
    me = cookie_api(self_service.member, self_service.stack, "GET", "/users/me")
    assert me["teams"][0]["key_limits"]["key_count"] == 1


def test_03_rotation_grace_accepts_both_secrets_on_one_key(self_service, rotated):
    stack, gw = self_service.stack, self_service.gateway
    assert gw.chat(rotated["old"]).status_code == 200
    assert gw.chat(rotated["new"]).status_code == 200
    assert len(rotated["rows"]) == 2
    for row in rotated["rows"]:
        assert row["status"] == 200
        assert row["prompt_tokens"] > 0
        assert str(row["key_id"]) == rotated["key_id"]
    assert datetime.fromisoformat(
        cookie_api(self_service.member, stack, "GET", f"/keys/{rotated['key_id']}")[
            "previous_key_expires_at"
        ]
    ) == rotated["previous_expires"]
    # Still only one key.
    assert cookie_api(self_service.member, stack, "GET", "/users/me")["teams"][0]["key_limits"][
        "key_count"
    ] == 1


def test_04_second_rotation_drops_the_older_secret_and_graces_the_previous(self_service, rotated_twice):
    assert self_service.gateway.chat(rotated_twice["new"]).status_code == 200
    assert rotated_twice["previous_expires"] > datetime.now(UTC)


def test_05_grace_of_a_few_seconds_is_enforced_by_the_gateway(self_service, short_grace):
    assert self_service.gateway.chat(short_grace["new"]).status_code == 200
    # The refusal happened at or after the control-plane deadline, while the snapshot and
    # the row stayed untouched: the gateway, not another write, ended the grace.
    assert 0 <= (short_grace["refused_at"] - short_grace["deadline"]).total_seconds() < 20


def test_06_rotation_without_grace_stops_the_old_secret(self_service, no_grace):
    assert self_service.gateway.chat(no_grace["new"]).status_code == 200


def test_07_suspend_resume_and_revoke_agree_with_the_gateway(self_service, lifecycle):
    assert lifecycle["suspended"] == ("suspended", "owner")
    assert lifecycle["suspended_error"] == "invalid_api_key"
    assert lifecycle["resumed"] == ("active", None)
    assert lifecycle["revoked"] == ("revoked", True)
    refused_rotate = lifecycle["rotate_after_revoke"]
    assert refused_rotate.status_code == 409, refused_rotate.text
    assert refused_rotate.json()["error"]["code"] == "conflict"
    assert cookie_api(self_service.member, self_service.stack, "GET", "/users/me")["teams"][0]["key_limits"][
        "key_count"
    ] == 0


def test_08_refusals_stay_inside_the_team_and_its_limits(refused):
    assert refused["alice"] == {
        # Metadata of another member's key is a team-admin view; Bob is only a member.
        "get": 403,
        "patch": 403,
        "rotate": 403,
        "revoke": 403,
        "still_serves": 200,
    }
    assert refused["foreign"] == {"create": 404, "read": 404}
    status, code, message = refused["expiry"]
    assert (status, code) == (422, "validation_error")
    assert "expires_at" in message and str(LIFETIME) in message
    assert refused["max_keys"]["count_before"] == 0
    assert refused["max_keys"]["created"] == MAX_KEYS
    assert refused["max_keys"]["over_status"] == 409
    assert refused["max_keys"]["over_code"] == "conflict"
    assert refused["max_keys"]["count_after"] == MAX_KEYS


def test_09_rotate_has_its_own_rate_limit(rotate_limited):
    limited = rotate_limited
    assert limited["statuses"][-1] == 429, limited["statuses"]
    # The clean principal cannot read Alice's key: every allowed attempt is a 404 from
    # the target lookup, which is after the limiter. Exactly the product's limit passes.
    assert set(limited["statuses"][:-1]) == {404}, limited["statuses"]
    assert limited["allowed_before_429"] == limited["limit"], limited
    assert len(limited["statuses"]) == limited["limit"] + 1, limited["statuses"]
    # Every attempt has to fall inside one fixed window, or the count could roll over.
    assert limited["elapsed"] < limited["window"], limited
    assert limited["code"] == "rate_limited"
    assert int(limited["retry_after"]) >= 1
    # Cheap enough to keep the suite short, and the refused target did not change.
    assert limited["alice_row_unchanged"]
    assert limited["alice_still_serves"] == 200
    # The create bucket was not exhausted with the rotate bucket.
    assert (limited["create_status"], limited["create_code"]) == (404, "not_found")


def test_10_key_writes_past_the_rate_limit_get_429(rate_limited):
    assert rate_limited["statuses"][-1] == 429, rate_limited["statuses"]
    # A foreign team is refused in the target lookup, after the limiter.
    assert set(rate_limited["statuses"][:-1]) == {404}, rate_limited["statuses"]
    assert rate_limited["allowed_before_429"] == rate_limited["limit"], rate_limited
    assert len(rate_limited["statuses"]) == rate_limited["limit"] + 1, rate_limited["statuses"]
    assert rate_limited["elapsed"] < rate_limited["window"], rate_limited
    assert rate_limited["code"] == "rate_limited"
    assert int(rate_limited["retry_after"]) >= 1
    # Fast enough to keep the suite short, and the refusal did not disturb serving.
    assert rate_limited["still_serves"] == 200


def test_11_audit_rows_exist_and_contain_no_secret(self_service):
    stack = self_service.stack
    rows = pg(
        stack.dsn,
        "SELECT action, details FROM audit_log"
        " WHERE team_id = $1 AND action IN ('key.create', 'key.rotate') ORDER BY id",
        uuid.UUID(self_service.team["id"]),
    )
    actions = [row["action"] for row in rows]
    assert "key.create" in actions and "key.rotate" in actions
    # asyncpg returns jsonb as text unless a codec is installed.
    details = [json.loads(row["details"]) for row in rows]
    text = json.dumps(details, default=str)
    assert_no_secret(text, self_service.issued_secrets, "an audit row contains a full secret")
    assert not scan_text(text, []), "an audit row contains a full token"
    # The create details keep the owner and expiry, the rotate details the grace.
    actions_details = zip(rows, details, strict=True)
    create = next(detail for row, detail in actions_details if row["action"] == "key.create")
    assert set(create) == {"name", "owner_kind", "owner_id", "expires_at"}
    actions_details = zip(rows, details, strict=True)
    rotate = next(detail for row, detail in actions_details if row["action"] == "key.rotate")
    assert {"name", "grace_seconds", "expires_at", "previous_key_expires_at"} == set(rotate)
    # Bob, a plain member, has no audit view: the team's audit is the team-admin view.
    denied = self_service.member.get(
        stack.api_url + "/audit",
        params={"team_id": self_service.team["id"], "action": "key.rotate"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "forbidden"

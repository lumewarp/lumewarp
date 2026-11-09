"""Identity gates against real Keycloak, Postgres, OpenFGA and gateway processes."""

import time
import uuid

import httpx
import pytest
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2
from database_events import DatabaseEvents
from identity import cookie_api, grant, identity_stack, login
from stack import pg


def assert_stream(response):
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text
    assert "chat.completion.chunk" in response.text


@pytest.fixture(scope="module", params=["builtin", "openfga"])
def p2(request, binaries):
    with identity_stack(request.param) as state:
        stack = state.stack
        state.events = state.cleanup.enter_context(DatabaseEvents(stack.dsn))
        # JIT creates no team memberships; Alice's first login created the proxy_admin.
        for browser in (state.member, state.admin):
            assert cookie_api(browser, stack, "GET", "/teams")["items"] == []
        assert pg(stack.dsn, "SELECT count(*) AS n FROM team_members")[0]["n"] == 0
        state.team = cookie_api(
            state.admin, stack, "POST", "/teams", json={"name": "p2", "org_id": stack.org_id, "model_access": "all"}
        )["id"]
        yield state


@pytest.fixture(scope="module")
def members(p2):
    grant(p2, p2.admin_user, "admin")
    grant(p2, p2.user, "member")
    return p2


def test_01_oidc_jit_and_team_visibility(members):
    state = members
    for browser in (state.member, state.admin):
        teams = cookie_api(browser, state.stack, "GET", "/teams")["items"]
        assert [team["id"] for team in teams] == [state.team]
    identities = pg(state.stack.dsn, "SELECT * FROM user_identities")
    assert len(identities) == 2
    assert {str(row["user_id"]) for row in identities} == {
        state.user["id"],
        state.admin_user["id"],
    }


@pytest.fixture(scope="module")
def keys(members):
    state = members
    stack = state.stack
    path = f"/teams/{state.team}"
    # Prove the session write is CSRF protected before using the valid token.
    response = state.member.post(
        stack.api_url + path + "/keys",
        headers={"X-Lumewarp-CSRF": "invalid"},
        json={"name": "no-csrf"},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_failed"
    state.user_key = cookie_api(
        state.member, stack, "POST", path + "/keys", json={"name": "member"}
    )
    response = state.member.post(
        stack.api_url + path + "/service-accounts", json={"name": "forbidden"}
    )
    assert response.status_code == 403, response.text
    state.sa = cookie_api(
        state.admin, stack, "POST", path + "/service-accounts", json={"name": "service"}
    )
    state.sa_key = cookie_api(
        state.admin,
        stack,
        "POST",
        path + "/keys",
        json={"name": "service", "service_account_id": state.sa["id"]},
    )
    return state


def test_02_member_and_admin_create_owned_keys(keys):
    assert keys.user_key["key"]["owner_kind"] == "user"
    assert keys.user_key["key"]["user_id"] == keys.user["id"]
    assert keys.sa_key["key"]["owner_kind"] == "service_account"
    assert keys.sa_key["key"]["service_account_id"] == keys.sa["id"]
    assert keys.sa["created_by_user_id"] == keys.admin_user["id"]


@pytest.fixture(scope="module")
def traffic(keys):
    state = keys
    stack = state.stack
    upstream = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "mock",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{stack.mock_port}/v1",
        },
    )
    model = stack.ok(
        "POST",
        "/models",
        json={
            "name": "mock-a",
            "upstream_id": upstream["id"],
            "upstream_model": "mock",
            "model_type": "llm",
            "price_input_nano_usd_per_mtok": 1_000_000_000,
            "price_output_nano_usd_per_mtok": 2_000_000_000,
        },
    )
    stack.ok("POST", "/routers", json={"name": "mock", "members": [{"model_id": model["id"]}]})

    def published():
        rows = pg(stack.dsn, "SELECT payload FROM config_versions ORDER BY version DESC LIMIT 1")
        if not rows:
            return False
        snapshot = snapshot_pb2.Snapshot.FromString(rows[0]["payload"])
        return len(snapshot.api_keys) == 2 and any(
            router.name == "mock" for router in snapshot.routers
        )

    state.events.wait(
        published,
        what="published keys and router",
    )
    state.gateway = stack.start_gateway()
    state.events.wait(
        lambda: stack.gateway_status()["acked_version"] == stack.current_version(),
        what="gateway ACK of routing and keys",
    )
    for key in (state.user_key, state.sa_key):
        response = state.gateway.chat(key["secret"])
        assert response.status_code == 200, response.text
        assert response.json()["usage"]["prompt_tokens"] > 0
        assert_stream(state.gateway.chat(key["secret"], stream=True))
    return state


def test_03_both_keys_serve_nonstream_and_stream(traffic):
    assert traffic.gateway.chat(None).status_code == 401
    assert traffic.gateway.chat("lw-invalid").status_code == 401


def test_04_usage_owners_cost_and_team_totals_reconcile(traffic):
    state = traffic
    stack = state.stack
    state.events.wait(lambda: len(stack.usage_rows()) == 4, what="four committed usage events")
    rows = stack.usage_rows()
    owner_scopes = [
        (
            state.user_key,
            "user",
            state.user["id"],
            "member",
            uuid.uuid5(uuid.UUID(state.team), state.user["id"]),
        ),
        (
            state.sa_key,
            "service_account",
            state.sa["id"],
            "service_account",
            uuid.UUID(state.sa["id"]),
        ),
    ]
    totals = pg(stack.dsn, "SELECT * FROM usage_totals")
    assert len(totals) == 3

    def reconcile(scope_type, scope_id, events):
        mine = [
            row for row in totals if row["scope_type"] == scope_type and row["scope_id"] == scope_id
        ]
        assert len(mine) == 1
        total = mine[0]
        assert total["request_count"] == len(events)
        assert total["unknown_cost_count"] == 0
        for field in ("prompt_tokens", "completion_tokens", "cost_nano_usd"):
            assert total[field] == sum(row[field] for row in events)

    for key, kind, owner_id, scope, scope_id in owner_scopes:
        mine = [row for row in rows if str(row["key_id"]) == key["key"]["id"]]
        assert len(mine) == 2 and {row["stream"] for row in mine} == {True, False}
        for row in mine:
            assert (str(row["team_id"]), row["owner_kind"], str(row["owner_id"])) == (
                state.team,
                kind,
                owner_id,
            )
            assert (row["status"], row["requested"], row["model"]) == (200, "mock", "mock-a")
            assert row["cost_known"] and row["prompt_tokens"] > 0 and row["completion_tokens"] > 0
            assert row["cost_nano_usd"] == (
                row["prompt_tokens"] * 1000 + row["completion_tokens"] * 2000
            )
        reconcile(scope, scope_id, mine)
    reconcile("team", uuid.UUID(state.team), rows)
    api_rows = cookie_api(
        state.admin, stack, "GET", "/usage", params={"team_id": state.team, "group_by": "owner"}
    )["items"]
    assert len(api_rows) == 2
    assert sum(row["requests"] for row in api_rows) == 4
    assert sum(row["cost_nano_usd"] for row in api_rows) == sum(
        row["cost_nano_usd"] for row in rows
    )


def test_05_iam_outage_revocation_and_idp_disable(traffic):
    state = traffic
    stack = state.stack
    gateway = state.gateway
    if state.backend == "openfga":
        started = time.monotonic()
        state.proxy.outage()
        try:
            response = state.member.get(stack.api_url + "/teams")
            assert response.status_code == 503, response.text
            for key in (state.user_key, state.sa_key):
                assert gateway.chat(key["secret"]).status_code == 200
            state.proxy.wait_polls()
            assert time.monotonic() - started >= 1, "must span distinct reconciler ticks"
            active = pg(stack.dsn, "SELECT lifecycle, suspended_reason FROM api_keys")
            assert len(active) == 2
            assert all(
                row["lifecycle"] == "active" and row["suspended_reason"] is None for row in active
            )
            for key in (state.user_key, state.sa_key):
                assert gateway.chat(key["secret"]).status_code == 200
                assert_stream(gateway.chat(key["secret"], stream=True))
            assert state.member.get(stack.api_url + "/teams").status_code == 503
        finally:
            with state.proxy.condition:
                state.proxy.failed = False
        assert state.member.get(stack.api_url + "/teams").status_code == 200

    started = time.monotonic()
    grant(state, state.user, "member", delete=True)
    state.events.wait(
        lambda: gateway.chat(state.user_key["secret"]).status_code == 401,
        timeout=max(0, 5 - (time.monotonic() - started)),
        what="removed member gateway 401",
    )
    elapsed = time.monotonic() - started
    print(f"revocation backend={state.backend} latency_seconds={elapsed:.6f}")
    assert elapsed < 5
    stored = stack.ok("GET", f"/keys/{state.user_key['key']['id']}")
    assert (stored["lifecycle"], stored["suspended_reason"]) == ("suspended", "owner_removed")
    assert cookie_api(state.member, stack, "GET", "/teams")["items"] == []
    assert gateway.chat(state.sa_key["secret"]).status_code == 200
    assert_stream(gateway.chat(state.sa_key["secret"], stream=True))

    with state.idp.temporary_user() as (username, user_url):
        with httpx.Client(timeout=15, trust_env=False) as browser:
            login(browser, stack, username)
        response = state.idp.http.put(user_url, json={"enabled": False})
        response.raise_for_status()
        # A fresh browser avoids mistaking an existing RP/IdP session for a new login.
        with httpx.Client(timeout=15, trust_env=False) as browser:
            login(browser, stack, username, enabled=False)


def test_06_openfga_renamed_store_via_mapping():
    """An existing store with other type names: sign in, create a key, revoke the tuple.

    The full scan is pushed far away, so the suspension has to come from `ReadChanges`
    mapped back to the Lumewarp user; `Check` and `ListObjects` run through the same
    mapping when the key is created and the team list is read.
    """
    from stack import ROOT

    model = ROOT / "deploy/openfga/model-renamed.fga"
    with identity_stack(
        "openfga",
        label="renamed",
        model_path=model,
        object_type="llm_team",
        extra_env={
            "LUMEWARP_OPENFGA_OBJECT_TYPES": '{"team": "llm_team", "org": "llm_org"}',
            "LUMEWARP_OPENFGA_RELATIONS": '{"team.admin": "owner"}',
            "LUMEWARP_KEY_RECONCILE_FULL_SECONDS": "3600",
        },
    ) as state:
        stack = state.stack
        team = cookie_api(
            state.admin, stack, "POST", "/teams", json={"name": "renamed", "org_id": stack.org_id, "model_access": "all"}
        )
        state.team = team
        grant(state, state.user, "member")
        listed = cookie_api(state.member, stack, "GET", "/teams")["items"]
        assert [row["id"] for row in listed] == [team["id"]]
        key = cookie_api(
            state.member, stack, "POST", f"/teams/{team['id']}/keys", json={"name": "mapped"}
        )
        assert key["key"]["user_id"] == state.user["id"]

        grant(state, state.user, "member", delete=True)

        deadline = time.monotonic() + 15
        stored = stack.ok("GET", f"/keys/{key['key']['id']}")
        while time.monotonic() < deadline and stored["lifecycle"] != "suspended":
            time.sleep(0.25)
            stored = stack.ok("GET", f"/keys/{key['key']['id']}")
        assert stored["lifecycle"] == "suspended", (
            "the removed tuple did not suspend the key through ReadChanges"
        )
        assert stored["suspended_reason"] == "owner_removed"
        assert cookie_api(state.member, stack, "GET", "/teams")["items"] == []

"""Core e2e gate: Postgres + control plane + gateway + mock upstream as real processes."""

import time
import uuid

import httpx
import pytest
from stack import GATEWAY_ID, pg, wait_for


def sse_ok(resp: httpx.Response) -> bool:
    return (
        resp.status_code == 200
        and "text/event-stream" in resp.headers.get("content-type", "")
        and "data: [DONE]" in resp.text
    )


def error_code(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


@pytest.mark.parametrize("owner_kind", ["service_account", "user"])
def test_two_keys_serve_and_usage_lands(stack, owner_kind):
    seeded = stack.seed(priced=True, owner_kind=owner_kind)
    gw = stack.start_gateway()

    for key in (seeded.a, seeded.b):
        r = gw.chat(key.secret)
        assert r.status_code == 200, r.text
        assert r.json()["usage"]["prompt_tokens"] > 0
        assert sse_ok(gw.chat(key.secret, stream=True))
    assert gw.chat(None).status_code == 401
    assert gw.chat("lw-not-a-key").status_code == 401

    wait_for(lambda: len(stack.usage_rows()) >= 4, 10, "4 usage rows")
    rows = stack.usage_rows()
    assert len(rows) == 4
    for key in (seeded.a, seeded.b):
        mine = [r for r in rows if str(r["key_id"]) == key.id]
        assert len(mine) == 2, "one stream and one non-stream per key"
        assert {r["stream"] for r in mine} == {True, False}
        for r in mine:
            assert str(r["team_id"]) == seeded.team_id
            assert r["owner_kind"] == owner_kind
            assert str(r["owner_id"]) == seeded.owner_id
            assert (r["status"], r["requested"], r["model"]) == (200, "mock", "mock-a")
            assert r["cost_known"] and r["prompt_tokens"] > 0 and r["completion_tokens"] > 0
            # price: 1000 / 2000 nano-USD per token
            assert r["cost_nano_usd"] == r["prompt_tokens"] * 1000 + r["completion_tokens"] * 2000
    spend = [line for line in gw.metrics().splitlines() if line.startswith("lumewarp_spend_total{")]
    assert len(spend) == 1
    labels, value = spend[0].removeprefix("lumewarp_spend_total{").split("} ")
    assert labels == f'team="{seeded.team_id}"'
    assert float(value) == pytest.approx(sum(r["cost_nano_usd"] for r in rows) / 1e9)

    # The control plane reports the gateway on the current version.
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == stack.current_version(),
        10,
        "ACKed version == current version",
    )


def test_removing_member_suspends_keys_within_two_seconds(stack):
    seeded = stack.seed(owner_kind="user")
    gw = stack.start_gateway()
    for key in (seeded.a, seeded.b):
        assert gw.chat(key.secret).status_code == 200

    started = time.monotonic()
    response = stack.api("DELETE", f"/teams/{seeded.team_id}/members/{seeded.owner_id}")
    assert response.status_code == 204, response.text
    for key in (seeded.a, seeded.b):
        wait_for(
            lambda key=key: gw.chat(key.secret).status_code == 401,
            max(0, 2.0 - (time.monotonic() - started)),
            "removed member key rejected",
        )
        assert error_code(gw.chat(key.secret)) == "invalid_api_key"
        stored = stack.ok("GET", f"/keys/{key.id}")
        assert (stored["lifecycle"], stored["suspended_reason"]) == ("suspended", "owner_removed")
    assert time.monotonic() - started < 2.0


def test_revoke_key_takes_effect_within_two_seconds(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    assert gw.chat(seeded.a.secret).status_code == 200
    assert gw.chat(seeded.b.secret).status_code == 200

    stack.ok("POST", f"/keys/{seeded.a.id}/revoke")
    started = time.monotonic()
    wait_for(
        lambda: gw.chat(seeded.a.secret).status_code == 401,
        2.0,
        "revoked key rejected",
        interval=0.05,
    )
    elapsed = time.monotonic() - started
    print(f"revoke propagated in {elapsed:.2f}s")
    assert elapsed < 2.0
    assert gw.chat(seeded.b.secret).status_code == 200
    assert error_code(gw.chat(seeded.a.secret)) == "invalid_api_key"


def test_archive_team_revokes_both_secrets_and_frees_the_name(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    assert gw.chat(seeded.a.secret).status_code == 200

    # Rotate with grace: the gateway accepts both the current and the previous secret.
    rotated = stack.ok("POST", f"/keys/{seeded.a.id}/rotate", json={"grace_seconds": 3600})
    new_secret = rotated["secret"]
    # Wait for the compile that carries the new hash; the previous one keeps working until
    # its own grace expiry.
    wait_for(lambda: gw.chat(new_secret).status_code == 200, 5, "rotated secret accepted")
    assert gw.chat(seeded.a.secret).status_code == 200

    assert stack.api("DELETE", f"/teams/{seeded.team_id}").status_code == 204
    wait_for(
        lambda: gw.chat(seeded.a.secret).status_code == 401,
        5.0,
        "both archived secrets rejected",
        interval=0.05,
    )
    # The rotation-grace hash was cleared too, and the team's other key is gone with it.
    for secret in (seeded.a.secret, new_secret, seeded.b.secret):
        response = gw.chat(secret)
        assert response.status_code == 401, response.text
        assert error_code(response) == "invalid_api_key"

    # The API no longer serves the team or its (deactivated) service account, but the row
    # and its name stay reusable for a new live team.
    assert stack.api("GET", f"/teams/{seeded.team_id}").status_code == 404
    assert stack.api("GET", f"/service-accounts/{seeded.owner_id}").status_code == 404
    reused = stack.ok("POST", "/teams", json={"name": "ml", "org_id": stack.org_id})
    assert reused["id"] != seeded.team_id
    archived = pg(
        stack.dsn,
        "SELECT deleted_at, name FROM teams WHERE id = $1",
        uuid.UUID(seeded.team_id),
    )
    assert archived[0]["deleted_at"] is not None and archived[0]["name"] == "ml"


def test_control_plane_outage_and_recovery(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    assert gw.chat(seeded.b.secret).status_code == 200
    version = stack.current_version()
    wait_for(lambda: (stack.gateway_status() or {}).get("acked_version") == version, 10, "ack")

    stack.stop_cp()
    assert gw.chat(seeded.b.secret).status_code == 200, "keeps serving with the CP down"
    assert sse_ok(gw.chat(seeded.b.secret, stream=True))

    # Restart the gateway while the CP is still down: boots from the on-disk snapshot.
    state = gw.state_dir
    gw.stop()
    gw2 = stack.start_gateway(state, tag="gateway-restarted", wait_ready=False)
    wait_for(lambda: gw2.readyz() == 200, 5, "ready from disk snapshot with CP down")
    assert gw2.chat(seeded.b.secret).status_code == 200
    assert gw2.metric("lumewarp_config_version") == version

    # The CP comes back: the gateway reconnects and the CP sees the current version ACKed.
    stack.start_cp()
    wait_for(
        lambda: (
            (s := stack.gateway_status())
            and s["connected"]
            and s["acked_version"] == stack.current_version()
        ),
        20,
        "gateway reconnected and ACKed the current version",
    )
    # And it follows new config again.
    stack.ok("POST", f"/keys/{seeded.b.id}/revoke")
    wait_for(lambda: gw2.chat(seeded.b.secret).status_code == 401, 5, "revoke after reconnect")
    wait_for(
        lambda: stack.gateway_status()["acked_version"] == stack.current_version(),
        5,
        "ACKed version follows current version",
    )


def test_cold_boot_without_cache_and_control_plane_down_is_never_ready(stack):
    seeded = stack.seed()
    stack.stop_cp()
    gw = stack.start_gateway(wait_ready=False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert gw.readyz() == 503
        r = gw.chat(seeded.a.secret)
        assert r.status_code == 503
        assert error_code(r) == "not_ready"
        assert gw.chat(None).status_code == 503
        time.sleep(0.2)
    assert not list(gw.state_dir.glob("snapshot.pb"))


@pytest.mark.parametrize("scope", ["team", "owner"])
@pytest.mark.parametrize("owner_kind", ["service_account", "user"])
def test_budget_exhaustion_blocks_key_within_five_seconds(stack, scope, owner_kind):
    # Each mock request costs ~16_000 nano-USD; the scope may spend 50_000.
    seeded = stack.seed(priced=True, owner_kind=owner_kind, **{f"{scope}_budget": 50_000})
    gw = stack.start_gateway()

    for _ in range(6):
        assert gw.chat(seeded.a.secret).status_code == 200
    spent = lambda: sum(r["cost_nano_usd"] for r in stack.usage_rows())  # noqa: E731
    wait_for(lambda: spent() > 50_000, 10, "usage over budget ingested")
    owner_scope = (
        uuid.uuid5(uuid.UUID(seeded.team_id), seeded.owner_id)
        if owner_kind == "user"
        else uuid.UUID(seeded.owner_id)
    )
    totals = pg(
        stack.dsn,
        "SELECT cost_nano_usd FROM usage_totals WHERE scope_type = $1 AND scope_id = $2",
        "member" if owner_kind == "user" else "service_account",
        owner_scope,
    )
    assert len(totals) == 1 and totals[0]["cost_nano_usd"] == spent()
    ingested = time.monotonic()

    def blocked() -> bool:
        r = gw.chat(seeded.a.secret)
        if r.status_code == 429:
            assert error_code(r) == "insufficient_quota"
            return True
        return False

    wait_for(blocked, 5.0, "key rejected with 429 insufficient_quota", interval=0.1)
    print(f"budget block {time.monotonic() - ingested:.2f}s after ingestion")
    assert gw.chat(seeded.b.secret).status_code == 429, "same owner and team, same budget"


def test_org_rate_limit_decrease_blocks_at_the_lower_value(stack):
    """A parent decrease below the team's setting is live after one compile.

    The org scope is the last line of defence: the team allows 1000 rpm but the org was
    lowered to 2, so the gateway admits two requests and then answers 429. Team and owner
    values are unchanged; the minimum down the chain is what the gateway enforces.
    """
    seeded = stack.seed()
    stack.ok("PATCH", f"/teams/{seeded.team_id}", json={"rpm_limit": 1000})
    (org_id,) = pg(stack.dsn, "SELECT org_id FROM teams WHERE id = $1", uuid.UUID(seeded.team_id))[
        0
    ].values()
    stack.ok("PATCH", f"/orgs/{org_id}", json={"rpm_limit": 1000})
    gw = stack.start_gateway()
    assert gw.chat(seeded.a.secret).status_code == 200
    # Let the 1000-rpm bucket drain so the lower rate starts from an empty bucket.
    time.sleep(0.2)

    stack.ok("PATCH", f"/orgs/{org_id}", json={"rpm_limit": 2})
    version = stack.current_version()
    wait_for(lambda: stack.current_version() > version, 10, "a new config version")
    target = stack.current_version()
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == target,
        10,
        "gateway ACKed the org decrease",
    )
    statuses = [gw.chat(seeded.a.secret).status_code for _ in range(3)]
    assert statuses == [200, 200, 429], statuses


def test_authz_denials(stack):
    seeded = stack.seed()
    admin = stack.create_pat("admin@e2e.test", seeded.team_id, "admin")
    member = stack.create_pat("member@e2e.test", seeded.team_id, "member")
    # Members cannot manage service accounts; neither team role manages gateways.
    r = stack.api(
        "POST", f"/teams/{seeded.team_id}/service-accounts", member, json={"name": "nope"}
    )
    assert r.status_code == 403, (r.status_code, r.text)
    fake = "00000000-0000-0000-0000-000000000000"
    for pat in (admin, member):
        assert stack.api("POST", "/gateways", pat, json={"gateway_id": "evil"}).status_code == 403
        r = stack.api("POST", f"/gateways/{GATEWAY_ID}/tokens/{fake}/revoke", pat)
        assert r.status_code == 403, (r.status_code, r.text)
        denied = stack.api("POST", "/teams", pat, json={"name": "evil", "org_id": stack.org_id})
        assert denied.status_code == 404, (denied.status_code, denied.text)
    assert (
        stack.api("POST", f"/teams/{seeded.team_id}/keys", member, json={"name": "ok"}).status_code
        == 201
    )
    assert stack.api("GET", "/users/me", admin).status_code == 200
    assert stack.api("POST", "/gateways", "lwp-garbage").status_code == 401

    # A key limited to other models cannot use "mock" through the gateway.
    up = stack.ok("GET", "/upstreams")["items"][0]
    dep = stack.ok(
        "POST",
        "/models",
        json={"name": "mock-b", "upstream_id": up["id"], "upstream_model": "mock"},
    )
    stack.ok("POST", "/routers", json={"name": "other-model", "members": [{"model_id": dep["id"]}]})
    other = stack.ok(
        "POST",
        f"/teams/{seeded.team_id}/service-accounts",
        admin,
        json={"name": "other", "model_access": "list", "allowed_models": ["other-model"]},
    )
    limited = stack.ok(
        "POST",
        f"/teams/{seeded.team_id}/keys",
        admin,
        json={"name": "limited", "service_account_id": other["id"]},
    )["secret"]
    stack.wait_key_count(4)  # a, b, member's key, limited
    gw = stack.start_gateway()
    assert gw.chat(limited, model="other-model").status_code == 200
    r = gw.chat(limited)
    assert r.status_code == 403, r.text
    assert error_code(r) == "model_not_allowed"
    assert gw.chat(seeded.a.secret).status_code == 200


def test_gateway_token_revoke_closes_stream(stack):
    stack.seed()
    gw = stack.start_gateway()
    wait_for(lambda: (stack.gateway_status() or {}).get("connected"), 10, "connected")
    token = stack.gateway_status()["tokens"][0]
    stack.ok("POST", f"/gateways/{token['gateway_id']}/tokens/{token['id']}/revoke")
    wait_for(
        lambda: gw.metric("lumewarp_config_stream_connected") == 0.0,
        10,
        "gateway stream disconnected after token revoke",
    )
    wait_for(lambda: not stack.gateway_status()["connected"], 10, "CP marks gateway disconnected")
    # The gateway keeps serving its last snapshot.
    assert gw.readyz() == 200

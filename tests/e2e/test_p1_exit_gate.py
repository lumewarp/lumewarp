"""P1 exit gate: Postgres + control plane + gateway + mock upstream as real processes."""

import time

import httpx
from stack import GATEWAY_ID, wait_for


def sse_ok(resp: httpx.Response) -> bool:
    return (
        resp.status_code == 200
        and "text/event-stream" in resp.headers.get("content-type", "")
        and "data: [DONE]" in resp.text
    )


def error_code(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


def test_two_keys_serve_and_usage_lands(stack):
    seeded = stack.seed(priced=True)
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
            assert str(r["project_id"]) == seeded.project_id
            assert str(r["team_id"]) == seeded.team_id
            assert str(r["org_id"]) == seeded.org_id
            assert (r["status"], r["model"], r["deployment"]) == (200, "mock", "mock-a")
            assert r["cost_known"] and r["prompt_tokens"] > 0 and r["completion_tokens"] > 0
            # price: 1000 / 2000 nano-USD per token
            assert r["cost_nano_usd"] == r["prompt_tokens"] * 1000 + r["completion_tokens"] * 2000

    # The control plane reports the gateway on the current version.
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == stack.current_version(),
        10,
        "ACKed version == current version",
    )


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


def test_budget_exhaustion_blocks_key_within_five_seconds(stack):
    # Each mock request costs ~16_000 nano-USD; the project may spend 50_000.
    seeded = stack.seed(priced=True, project_budget=50_000)
    gw = stack.start_gateway()

    for _ in range(6):
        assert gw.chat(seeded.a.secret).status_code == 200
    spent = lambda: sum(r["cost_nano_usd"] for r in stack.usage_rows())  # noqa: E731
    wait_for(lambda: spent() > 50_000, 10, "usage over budget ingested")
    ingested = time.monotonic()

    def blocked() -> bool:
        r = gw.chat(seeded.a.secret)
        if r.status_code == 429:
            assert error_code(r) == "insufficient_quota"
            return True
        return False

    wait_for(blocked, 5.0, "key rejected with 429 insufficient_quota", interval=0.1)
    print(f"budget block {time.monotonic() - ingested:.2f}s after ingestion")
    assert gw.chat(seeded.b.secret).status_code == 429, "same project, same budget"


def test_rbac_denials(stack):
    seeded = stack.seed()
    viewer = stack.create_pat("viewer@e2e.test", seeded.org_id, seeded.team_id, "viewer")
    member = stack.create_pat("member@e2e.test", seeded.org_id, seeded.team_id, "member")
    # Viewers cannot create keys; members may (RBAC matrix), but neither manages gateways.
    r = stack.api("POST", f"/projects/{seeded.project_id}/keys", viewer, json={"name": "nope"})
    assert r.status_code == 403, (r.status_code, r.text)
    fake = "00000000-0000-0000-0000-000000000000"
    for pat in (viewer, member):
        assert stack.api("POST", "/gateways", pat, json={"gateway_id": "evil"}).status_code == 403
        r = stack.api("POST", f"/gateways/{GATEWAY_ID}/tokens/{fake}/revoke", pat)
        assert r.status_code == 403, (r.status_code, r.text)
        assert stack.api("POST", "/orgs", pat, json={"name": "evil"}).status_code == 403
    assert (
        stack.api(
            "POST", f"/projects/{seeded.project_id}/keys", member, json={"name": "ok"}
        ).status_code
        == 201
    )
    assert stack.api("GET", "/users/me", viewer).status_code == 200
    assert stack.api("POST", "/gateways", "lwp-garbage").status_code == 401

    # A key limited to other models cannot use "mock" through the gateway.
    up = stack.ok("GET", "/upstreams")["items"][0]
    dep = stack.ok(
        "POST",
        "/deployments",
        json={"name": "mock-b", "upstream_id": up["id"], "upstream_model": "mock"},
    )
    stack.ok("POST", "/routes", json={"model": "other-model", "deployment_ids": [dep["id"]]})
    other = stack.ok("POST", f"/teams/{seeded.team_id}/projects", json={"name": "other"})
    limited = stack.ok(
        "POST",
        f"/projects/{other['id']}/keys",
        json={"name": "limited", "model_access": "list", "allowed_models": ["other-model"]},
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

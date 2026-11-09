"""An upstream API key entered in the admin API reaches the upstream as a bearer token,
while the database, the API, the audit log and the gateway's on-disk snapshot only ever
hold ciphertext (the run's secret scan checks the database). Runs under e2e-core.sh.
"""

import httpx
from stack import record_secret, wait_for

UPSTREAM_KEY = "sk-e2e-sealed-upstream-key-5c1e9a7d"


def test_sealed_upstream_key_reaches_the_upstream(stack):
    record_secret(UPSTREAM_KEY, "sealed upstream API key")
    seeded = stack.seed()
    port = stack.start_mock("mock-echo", "--echo")
    upstream = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "sealed",
            "kind": "openai",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": UPSTREAM_KEY,
        },
    )
    assert upstream["api_key_set"] is True
    model = stack.ok(
        "POST",
        "/models",
        json={"name": "sealed-a", "upstream_id": upstream["id"], "upstream_model": "echo", "model_type": "llm"},
    )
    assert UPSTREAM_KEY not in str(stack.ok("GET", f"/upstreams/{upstream['id']}"))
    assert UPSTREAM_KEY not in stack.api("GET", "/audit").text

    gw = stack.start_gateway()

    def call() -> httpx.Response:
        return httpx.post(
            gw.url("/v1/chat/completions"),
            headers={"authorization": f"Bearer {seeded.a.secret}"},
            json={"model": model["name"], "messages": []},
            timeout=5,
        )

    # The model reaches the gateway with the snapshot that compiles it, which can be a
    # version after the one the gateway first ACKs.
    wait_for(lambda: call().status_code != 404, 20, "gateway serves the sealed model")
    response = call()
    assert response.status_code == 200, response.text
    assert response.json()["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
    for path in gw.state_dir.iterdir():
        assert UPSTREAM_KEY.encode() not in path.read_bytes(), path

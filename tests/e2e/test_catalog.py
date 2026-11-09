"""The model catalog through a real control plane and gateway: names are created over the
admin API, reach the gateway in a snapshot, and are called like a client would. Runs under
scripts/e2e-core.sh and e2e-identity.sh.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from no_secret import assert_no_secret
from stack import free_port, pg, wait_for

# nano-USD per million tokens: multiples of 1e6, so the cost per token is exact.
PRICE_A = (1_000_000_000, 2_000_000_000)
PRICE_B = (3_000_000_000, 5_000_000_000)


# ------------------------------------------------------------------------------- helpers


def _upstream(stack, name: str, port: int) -> str:
    return stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": name,
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{port}/v1",
        },
    )["id"]


def _mock_upstream(stack) -> str:
    return next(u["id"] for u in stack.ok("GET", "/upstreams")["items"] if u["name"] == "mock")


def _model(stack, name: str, upstream: str, *, upstream_model: str = "mock", price=PRICE_A) -> str:
    return stack.ok(
        "POST",
        "/models",
        json={
            "name": name,
            "upstream_id": upstream,
            "upstream_model": upstream_model,
            "model_type": "llm",
            "price_input_nano_usd_per_mtok": price[0],
            "price_output_nano_usd_per_mtok": price[1],
        },
    )["id"]


def _router(stack, name: str, *models: str, **fields) -> str:
    members = [{"model_id": model} for model in models]
    return stack.ok("POST", "/routers", json={"name": name, "members": members, **fields})["id"]


def _alias(stack, name: str, target: str) -> str:
    return stack.ok("POST", "/aliases", json={"name": name, "target": target})["id"]


def _limited_key(stack, team_id: str, name: str, allowed: list[str]) -> tuple[str, str]:
    """A service account limited to `allowed`, and a key of it: (service account id, secret)."""
    account = stack.ok(
        "POST",
        f"/teams/{team_id}/service-accounts",
        json={"name": name, "model_access": "list", "allowed_models": allowed},
    )["id"]
    secret = stack.ok(
        "POST",
        f"/teams/{team_id}/keys",
        json={"name": name, "service_account_id": account},
    )["secret"]
    return account, secret


def _names(gw, key: str) -> set[str]:
    r = httpx.get(gw.url("/v1/models"), headers={"authorization": f"Bearer {key}"}, timeout=2)
    return {m["id"] for m in r.json()["data"]} if r.status_code == 200 else set()


def _wait_names(gw, key: str, present=(), absent=()) -> None:
    """Until the gateway's snapshot has these names and not those (sends no inference)."""
    wanted, gone = set(present), set(absent)
    wait_for(
        lambda: (seen := _names(gw, key)) >= wanted and not seen & gone,
        15,
        f"gateway serving {wanted} and not {gone}",
    )


def _version_settled(stack) -> int:
    """Until the last committed mutation is compiled and the gateway ACKed it."""
    revision = pg(stack.dsn, "SELECT source_revision FROM scopes WHERE name = 'e2e'")[0][0]

    def compiled():
        return (
            stack.ok("GET", "/config/versions", params={"limit": 1})["items"][0]["source_revision"]
            >= revision
        )

    wait_for(compiled, 15, f"source revision {revision} compiled")
    version = stack.current_version()
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == version,
        15,
        f"gateway ACKed version {version}",
    )
    return version


def _sse_models(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    assert "data: [DONE]" in response.text, response.text
    return [
        json.loads(line.removeprefix("data: "))["model"]
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]


def _assert_answers_as(gw, key: str, name: str) -> None:
    """Both kinds of response carry the called name, never the serving model's."""
    response = gw.chat(key, model=name)
    assert response.status_code == 200, (name, response.text)
    assert response.json()["model"] == name
    models = _sse_models(gw.chat(key, model=name, stream=True))
    assert models and set(models) == {name}, (name, models)


def _row(stack, response: httpx.Response):
    """The usage row of this response, by the id the client got."""
    request_id = uuid.UUID(response.headers["x-lumewarp-request-id"])

    def found():
        rows = pg(stack.dsn, "SELECT * FROM usage_events WHERE request_id = $1", request_id)
        return rows[0] if rows else None

    return wait_for(found, 15, f"usage row {request_id}")


def _served_by(stack, response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    return _row(stack, response)["model"]


def _usage(stack, **params) -> list[dict]:
    return stack.ok("GET", "/usage", params=params)["items"]


# ------------------------------------------------------------------------------- calling


def test_model_is_callable_listed_and_answers_with_its_name(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    _model(stack, "lumewarp/model", _mock_upstream(stack), upstream_model="Qwen/Qwen3-8B")
    _wait_names(gw, seeded.a.secret, ["lumewarp/model"])

    _assert_answers_as(gw, seeded.a.secret, "lumewarp/model")
    # Kinds are not told apart in the list, and the upstream's own name stays out of it.
    assert "Qwen/Qwen3-8B" not in _names(gw, seeded.a.secret)
    assert gw.chat(seeded.a.secret, model="Qwen/Qwen3-8B").status_code == 404


def test_alias_router_and_fallback_answer_with_the_called_name(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    upstream = _mock_upstream(stack)
    down = _upstream(stack, "down", free_port())
    good = _model(stack, "good", upstream, upstream_model="good-upstream")
    pool = _router(stack, "pool", good)
    _alias(stack, "to-model", good)
    _alias(stack, "to-router", pool)
    _router(
        stack,
        "flaky",
        _model(stack, "broken", down),
        retry={"max_attempts": 2},
        fallback_model_ids=[good],
    )
    names = ["pool", "to-model", "to-router", "flaky"]
    _wait_names(gw, seeded.a.secret, names)

    for name in names:
        _assert_answers_as(gw, seeded.a.secret, name)
    served = gw.chat(seeded.a.secret, model="flaky")
    assert _served_by(stack, served) == "good", "the fallback model served it"
    assert served.json()["model"] == "flaky"


def test_changing_an_alias_target_moves_new_requests(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    upstream = _mock_upstream(stack)
    first, second = _model(stack, "first", upstream), _model(stack, "second", upstream)
    pool = _router(stack, "pool", second)
    alias = _alias(stack, "nick", first)
    _wait_names(gw, seeded.a.secret, ["nick", "pool"])
    assert _served_by(stack, gw.chat(seeded.a.secret, model="nick")) == "first"

    stack.ok("PATCH", f"/aliases/{alias}", json={"target": pool})
    version = _version_settled(stack)
    assert gw.metric("lumewarp_config_version") == version
    for _ in range(5):
        response = gw.chat(seeded.a.secret, model="nick")
        assert _served_by(stack, response) == "second"
        assert response.json()["model"] == "nick"
    row = _row(stack, response)
    assert (row["alias"], row["router"]) == ("nick", "pool"), "provenance follows the new target"


# ------------------------------------------------------------------------------- renames


def test_renames_end_the_old_name_unless_a_model_keeps_it(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    upstream = _mock_upstream(stack)
    plain = _model(stack, "plain", upstream)
    kept = _model(stack, "kept", upstream)
    pool = _router(stack, "pool", plain)
    nick = _alias(stack, "nick", pool)
    _alias(stack, "to-plain", plain)
    _, limited = _limited_key(stack, seeded.team_id, "limited", ["plain", "kept", "pool", "nick"])
    every = ["plain", "kept", "pool", "nick", "to-plain"]
    _wait_names(gw, seeded.a.secret, every)
    _wait_names(gw, limited, ["plain", "kept", "pool", "nick"])

    stack.ok("PATCH", f"/models/{plain}", json={"name": "plain2"})
    stack.ok("PATCH", f"/models/{kept}", json={"name": "kept2", "keep_old_name_as_alias": True})
    stack.ok("PATCH", f"/routers/{pool}", json={"name": "pool2"})
    stack.ok("PATCH", f"/aliases/{nick}", json={"name": "nick2"})
    _wait_names(
        gw,
        seeded.a.secret,
        ["plain2", "kept2", "kept", "pool2", "nick2", "to-plain"],
        ["plain", "pool", "nick"],
    )

    for gone in ("plain", "pool", "nick"):
        response = gw.chat(seeded.a.secret, model=gone)
        assert response.status_code == 404, (gone, response.text)
    for name in ("plain2", "kept2", "pool2", "nick2"):
        _assert_answers_as(gw, seeded.a.secret, name)
    # The kept name is an alias now and still answers as itself.
    _assert_answers_as(gw, seeded.a.secret, "kept")
    assert _row(stack, gw.chat(seeded.a.secret, model="kept"))["alias"] == "kept"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="kept")) == "kept2"
    # References are by id: the router and the aliases follow the renamed model and router.
    assert _served_by(stack, gw.chat(seeded.a.secret, model="pool2")) == "plain2"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="to-plain")) == "plain2"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="nick2")) == "plain2"

    # Grants are by id too, and the kept alias carries the model's.
    _wait_names(
        gw, limited, ["plain2", "kept2", "kept", "pool2", "nick2"], ["plain", "pool", "nick"]
    )
    for name in ("plain2", "kept2", "kept", "pool2", "nick2"):
        assert gw.chat(limited, model=name).status_code == 200, name
    for name in ("plain", "pool", "nick"):
        assert gw.chat(limited, model=name).status_code == 403, name


# ------------------------------------------------------------------------------- grants


def test_a_grant_covers_the_called_name_only(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    upstream = _mock_upstream(stack)
    member = _model(stack, "member", upstream)
    target = _model(stack, "target", upstream)
    fallback = _model(stack, "backup", upstream)
    _router(stack, "pool", member, fallback_model_ids=[fallback], retry={"max_attempts": 2})
    _alias(stack, "nick", target)
    _, router_only = _limited_key(stack, seeded.team_id, "router-only", ["pool"])
    _, alias_only = _limited_key(stack, seeded.team_id, "alias-only", ["nick"])
    _, target_only = _limited_key(stack, seeded.team_id, "target-only", ["target"])
    _wait_names(gw, router_only, ["pool"])
    _wait_names(gw, alias_only, ["nick"])
    _wait_names(gw, target_only, ["target"])
    assert _names(gw, router_only) == {"pool"}, "lists what the key may call, nothing behind it"
    assert _names(gw, alias_only) == {"nick"}

    def denied(key: str, name: str) -> None:
        response = gw.chat(key, model=name)
        assert response.status_code == 403, (name, response.text)
        assert response.json()["error"]["code"] == "model_not_allowed"

    assert gw.chat(router_only, model="pool").status_code == 200
    denied(router_only, "member")
    denied(router_only, "backup")
    assert gw.chat(alias_only, model="nick").status_code == 200
    denied(alias_only, "target")
    # The other direction: the target's grant does not reach its alias.
    assert gw.chat(target_only, model="target").status_code == 200
    denied(target_only, "nick")


# ------------------------------------------------------------------------------- rollback


def test_rollback_restores_names_targets_and_weights_with_grants_intact(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    upstream = _mock_upstream(stack)
    light = _model(stack, "light", upstream)
    heavy = _model(stack, "heavy", upstream)
    members = [{"model_id": light, "weight": 0}, {"model_id": heavy, "weight": 1}]
    pool = stack.ok("POST", "/routers", json={"name": "pool", "members": members})["id"]
    nick = _alias(stack, "nick", pool)
    doomed = _alias(stack, "doomed", light)
    account, limited = _limited_key(stack, seeded.team_id, "limited", ["pool", "nick", "heavy"])
    other, other_key = _limited_key(stack, seeded.team_id, "other", ["doomed"])
    _wait_names(gw, limited, ["pool", "nick", "heavy"])
    _wait_names(gw, other_key, ["doomed"])
    before = _version_settled(stack)
    assert _served_by(stack, gw.chat(limited, model="pool")) == "heavy"

    # Every kind renamed (one model keeping its name, so a later rollback must take that
    # alias away again), weights swapped, a target moved, one name deleted, one added.
    stack.ok("PATCH", f"/models/{light}", json={"name": "light2"})
    stack.ok("PATCH", f"/models/{heavy}", json={"name": "heavy2", "keep_old_name_as_alias": True})
    stack.ok("PATCH", f"/routers/{pool}", json={"name": "pool2"})
    stack.ok("PATCH", f"/aliases/{nick}", json={"name": "nick2", "target": light})
    swapped = [{"model_id": light, "weight": 1}, {"model_id": heavy, "weight": 0}]
    stack.ok("PATCH", f"/routers/{pool}", json={"members": swapped})
    assert stack.api("DELETE", f"/aliases/{doomed}").status_code == 204
    added = _model(stack, "added", upstream)
    after = _version_settled(stack)
    assert after > before
    assert _served_by(stack, gw.chat(seeded.a.secret, model="pool2")) == "light2"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="nick2")) == "light2"
    assert sorted(stack.ok("GET", f"/service-accounts/{account}")["allowed_models"]) == [
        "heavy",
        "heavy2",
        "nick2",
        "pool2",
    ], "grants follow the ids, and the kept alias has the model's"

    version = stack.ok("POST", "/config/rollback", json={"version": before})["version"]
    assert version > after
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == version,
        15,
        "gateway on the rollback version",
    )

    # Source tables: same ids, old names, old weights and target; what came later is gone.
    models = {m["name"]: m["id"] for m in stack.ok("GET", "/models")["items"]}
    assert models["light"] == light and models["heavy"] == heavy
    assert "light2" not in models and "heavy2" not in models and "added" not in models
    assert stack.api("GET", f"/models/{added}").status_code == 404
    router = stack.ok("GET", f"/routers/{pool}")
    assert router["name"] == "pool"
    weights = {m["model_id"]: m["weight"] for m in router["members"]}
    assert weights == {light: 0, heavy: 1}
    aliases = {a["name"]: a for a in stack.ok("GET", "/aliases")["items"]}
    assert set(aliases) == {"nick", "doomed"}, "the kept-name alias went with the rename"
    assert aliases["nick"]["id"] == nick and aliases["nick"]["target"] == pool
    assert aliases["doomed"]["id"] == doomed, "a deleted name comes back under its own id"

    # The gateway serves that state.
    _wait_names(
        gw,
        seeded.a.secret,
        ["light", "heavy", "pool", "nick", "doomed"],
        ["light2", "heavy2", "pool2", "nick2", "added"],
    )
    for gone in ("light2", "heavy2", "pool2", "nick2", "added"):
        assert gw.chat(seeded.a.secret, model=gone).status_code == 404, gone
    for _ in range(5):
        assert _served_by(stack, gw.chat(seeded.a.secret, model="pool")) == "heavy", "weights"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="nick")) == "heavy", "target"
    assert _served_by(stack, gw.chat(seeded.a.secret, model="doomed")) == "light"

    # Grants followed the ids across the rename and back; references are intact.
    assert sorted(stack.ok("GET", f"/service-accounts/{account}")["allowed_models"]) == [
        "heavy",
        "nick",
        "pool",
    ]
    _wait_names(gw, limited, ["pool", "nick", "heavy"])
    for name in ("pool", "nick", "heavy"):
        assert gw.chat(limited, model=name).status_code == 200, name
    assert gw.chat(limited, model="light").status_code == 403
    assert stack.api("DELETE", f"/models/{heavy}").status_code == 409, "still a router member"
    # Grants are not routing config: the deleted name's grant stays deleted (documented).
    assert stack.ok("GET", f"/service-accounts/{other}")["allowed_models"] == []
    assert gw.chat(other_key, model="doomed").status_code == 403

    # And forward again, to the version after the renames.
    forward = stack.ok("POST", "/config/rollback", json={"version": after})["version"]
    wait_for(
        lambda: (stack.gateway_status() or {}).get("acked_version") == forward,
        15,
        "gateway on the forward rollback",
    )
    _wait_names(gw, seeded.a.secret, ["pool2", "nick2", "added", "heavy"], ["pool", "nick"])
    assert stack.ok("GET", f"/models/{added}")["id"] == added
    assert _served_by(stack, gw.chat(seeded.a.secret, model="pool2")) == "light2"
    assert gw.chat(limited, model="pool2").status_code == 200


# ------------------------------------------------------------------------------- usage


def _usage_catalog(stack):
    upstream = _mock_upstream(stack)
    down = _upstream(stack, "down", free_port())
    a = _model(stack, "gpt-4o", upstream, upstream_model="up-a", price=PRICE_A)
    b = _model(stack, "lumewarp/qwen3-b", upstream, upstream_model="up-b", price=PRICE_B)
    _alias(stack, "gpt-4", a)
    pool = _router(stack, "qwen-coder", b)
    _alias(stack, "coder-latest", pool)
    flaky = _router(
        stack,
        "flaky",
        _model(stack, "broken", down, upstream_model="up-broken"),
        retry={"max_attempts": 2},
        fallback_model_ids=[a],
    )
    _alias(stack, "flaky-latest", flaky)
    return a, b


def test_usage_rows_record_what_served_and_how_it_got_there(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    a, b = _usage_catalog(stack)
    # The table under "Usage 記錄": called name, model, alias, router, fallback, attempts.
    table = [
        ("gpt-4o", "gpt-4o", "", "", False, 1, "up-a", PRICE_A),
        ("gpt-4", "gpt-4o", "gpt-4", "", False, 1, "up-a", PRICE_A),
        ("qwen-coder", "lumewarp/qwen3-b", "", "qwen-coder", False, 1, "up-b", PRICE_B),
        (
            "coder-latest",
            "lumewarp/qwen3-b",
            "coder-latest",
            "qwen-coder",
            False,
            1,
            "up-b",
            PRICE_B,
        ),
        ("flaky-latest", "gpt-4o", "flaky-latest", "flaky", True, 2, "up-a", PRICE_A),
    ]
    _wait_names(gw, seeded.a.secret, [line[0] for line in table])

    ids = {"gpt-4o": a, "lumewarp/qwen3-b": b}
    for called, model, alias, router, fallback, attempts, upstream_model, price in table:
        response = gw.chat(seeded.a.secret, model=called)
        assert response.status_code == 200, response.text
        assert response.json()["model"] == called
        row = _row(stack, response)
        assert row["requested"] == called
        assert (row["model"], row["alias"], row["router"]) == (model, alias, router), called
        assert (row["fallback"], row["attempts"]) == (fallback, attempts), called
        assert (row["upstream"], row["upstream_model"]) == ("mock", upstream_model), called
        assert row["model_id"] == uuid.UUID(ids[model]), called
        assert (row["alias_id"] is not None) == bool(alias), called
        assert (row["router_id"] is not None) == bool(router), called
        # The price of the model that served, whatever the client called.
        assert (
            row["cost_nano_usd"]
            == (row["prompt_tokens"] * price[0] + row["completion_tokens"] * price[1]) // 1_000_000
        ), called
        assert row["cost_known"] and row["prompt_tokens"] > 0


def test_usage_filters_and_renames_keep_one_identity(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    a, b = _usage_catalog(stack)
    called = ["gpt-4o", "gpt-4", "qwen-coder", "coder-latest", "flaky-latest"]
    _wait_names(gw, seeded.a.secret, called)
    before = {name: gw.chat(seeded.a.secret, model=name) for name in called}
    for response in before.values():
        assert response.status_code == 200
        _row(stack, response)

    def requests(**params) -> int:
        return sum(item["requests"] for item in _usage(stack, team_id=seeded.team_id, **params))

    assert requests(model="gpt-4o") == 3  # direct, alias, fallback
    assert requests(model_id=b) == 2
    assert requests(upstream_model="up-b") == 2
    assert requests(alias="coder-latest") == 1
    assert requests(alias_missing=True) == 2
    assert requests(alias_missing=False) == 3
    assert requests(router="qwen-coder") == 2
    assert requests(router_missing=True) == 2

    # An upstream change does not rewrite history: old rows keep the name they went to.
    stack.ok("PATCH", f"/models/{b}", json={"upstream_model": "up-b2"})
    _version_settled(stack)
    moved = gw.chat(seeded.a.secret, model="qwen-coder")
    assert _row(stack, moved)["upstream_model"] == "up-b2"
    assert _row(stack, before["qwen-coder"])["upstream_model"] == "up-b"
    assert requests(upstream_model="up-b") == 2
    assert requests(upstream_model="up-b2") == 1

    # A rename keeping the old name: the old name is now an alias too, and events from
    # before the rename still belong to the model, not to that alias.
    stack.ok("PATCH", f"/models/{a}", json={"name": "gpt-4o-v2", "keep_old_name_as_alias": True})
    version = _version_settled(stack)
    assert gw.metric("lumewarp_config_version") == version
    earlier = _row(stack, before["gpt-4o"])
    assert earlier["model"] == "gpt-4o", "the row keeps the spelling of its time"
    assert earlier["model_id"] == uuid.UUID(a) and earlier["alias_id"] is None
    after_new = gw.chat(seeded.a.secret, model="gpt-4o-v2")
    after_old = gw.chat(seeded.a.secret, model="gpt-4o")
    row = _row(stack, after_new)
    assert (row["model"], row["alias"]) == ("gpt-4o-v2", "")
    assert row["model_id"] == uuid.UUID(a)
    row = _row(stack, after_old)
    assert (row["model"], row["alias"], row["requested"]) == ("gpt-4o-v2", "gpt-4o", "gpt-4o")
    assert row["model_id"] == uuid.UUID(a) and row["alias_id"] is not None

    # The filter by id and by the current name see one continuous series, under one group.
    assert requests(model_id=a) == 5
    assert requests(model="gpt-4o-v2") == 5
    assert requests(alias="gpt-4o") == 1, "the alias only has what came through it"
    groups = [
        item
        for item in _usage(stack, team_id=seeded.team_id, group_by="model")
        if item["model_id"] == a
    ]
    assert len(groups) == 1 and groups[0]["model"] == "gpt-4o-v2", groups
    assert groups[0]["requests"] == 5
    assert set(groups[0]["upstream_models"]) == {"up-a"}


@pytest.fixture
def gated_upstream():
    """An upstream that holds each chat completion until released."""
    arrived = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            arrived.set()
            release.wait(60)
            body = json.dumps(
                {
                    "id": "chatcmpl-gated",
                    "object": "chat.completion",
                    "model": "gated",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], arrived, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_a_request_in_flight_across_a_rename_stays_with_its_model(stack, gated_upstream):
    """Started under the old name, reported after the rename and the keep-old-name alias are
    live: the ingest has to resolve the old serving string through the name history, and must
    not hand the row to the alias now holding that name."""
    port, arrived, release = gated_upstream
    seeded = stack.seed()
    gw = stack.start_gateway()
    model = _model(stack, "before-rename", _upstream(stack, "gated", port), upstream_model="gated")
    _wait_names(gw, seeded.a.secret, ["before-rename"])

    responses: list[httpx.Response] = []

    def call() -> None:
        responses.append(
            httpx.post(
                gw.url("/v1/chat/completions"),
                json={"model": "before-rename", "messages": [{"role": "user", "content": "hi"}]},
                headers={"authorization": f"Bearer {seeded.a.secret}"},
                timeout=60,
            )
        )

    client = threading.Thread(target=call)
    client.start()
    try:
        assert arrived.wait(15), "the request never reached the upstream"
        stack.ok(
            "PATCH",
            f"/models/{model}",
            json={"name": "after-rename", "keep_old_name_as_alias": True},
        )
        _version_settled(stack)
        assert not responses, "the request must still be in flight across the rename"
    finally:
        release.set()
        client.join(30)
    assert responses, "the request did not finish"
    response = responses[0]
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "before-rename", "the response keeps the called name"

    row = _row(stack, response)
    renamed_at = pg(
        stack.dsn, "SELECT renamed_at FROM catalog_name_history WHERE old_name = 'before-rename'"
    )[0]["renamed_at"]
    assert row["started_at"] < renamed_at, "the request has to start before the rename"
    assert (row["requested"], row["model"], row["alias"]) == (
        "before-rename",
        "before-rename",
        "",
    )
    assert row["model_id"] == uuid.UUID(model), "resolved through the name history"
    assert row["alias_id"] is None, "the alias on the old name did not serve this request"

    def requests(**params) -> int:
        return sum(item["requests"] for item in _usage(stack, team_id=seeded.team_id, **params))

    assert requests(model_id=model) == 1
    assert requests(model="after-rename") == 1
    assert requests(alias="before-rename") == 0
    groups = [
        item for item in _usage(stack, team_id=seeded.team_id, group_by="model") if item["requests"]
    ]
    assert [(g["model_id"], g["model"], g["requests"]) for g in groups] == [
        (model, "after-rename", 1)
    ], groups


def test_refused_requests_keep_the_called_name(stack):
    seeded = stack.seed()
    gw = stack.start_gateway()
    _, limited = _limited_key(stack, seeded.team_id, "limited", ["mock"])
    _wait_names(gw, limited, ["mock"])

    unknown = gw.chat(seeded.a.secret, model="no-such-name")
    assert unknown.status_code == 404
    row = _row(stack, unknown)
    assert (row["requested"], row["model"], row["status"]) == ("no-such-name", "", 404)
    forbidden = gw.chat(limited, model="mock-a")
    assert forbidden.status_code == 403
    row = _row(stack, forbidden)
    assert (row["requested"], row["model"], row["status"]) == ("mock-a", "", 403)
    # A key refused before the body is read has no called name to record (docs/architecture.md,
    # "實作取捨"): gateway/tests/usage.rs
    # keys_refused_before_the_body_report_an_event_with_no_name.


# ------------------------------------------------------------------------------- OTel


def _fields(data: bytes):
    """Just enough protobuf to read OTLP: (field number, integer value, bytes value)."""

    def varint(i: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, i
            shift += 7

    i = 0
    while i < len(data):
        tag, i = varint(i)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = varint(i)
            yield number, value, b""
        elif wire == 1:
            yield number, 0, data[i : i + 8]
            i += 8
        elif wire == 2:
            size, i = varint(i)
            yield number, 0, data[i : i + size]
            i += size
        elif wire == 5:
            yield number, 0, data[i : i + 4]
            i += 4
        else:
            raise ValueError(f"wire type {wire}")


def _spans(request: bytes) -> list[dict]:
    """ExportTraceServiceRequest.resource_spans(1).scope_spans(2).spans(2)."""
    spans = []
    for number, _, resource in _fields(request):
        if number != 1:
            continue
        for number, _, scope in _fields(resource):
            if number != 2:
                continue
            for number, _, raw in _fields(scope):
                if number != 2:
                    continue
                span = {"attributes": {}}
                for field, _, value in _fields(raw):
                    if field == 1:
                        span["trace_id"] = value.hex()
                    elif field == 5:
                        span["name"] = value.decode()
                    elif field == 9:
                        key, attribute = "", ""
                        for n, _, v in _fields(value):
                            if n == 1:
                                key = v.decode()
                            elif n == 2:
                                for kind, integer, s in _fields(v):
                                    attribute = {
                                        1: s.decode(errors="replace"),
                                        2: str(bool(integer)).lower(),
                                        3: str(integer),
                                    }.get(kind, attribute)
                        span["attributes"][key] = attribute
                spans.append(span)
    return spans


class OtlpSink:
    def __init__(self) -> None:
        self.raw: list[bytes] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's naming
                length = int(self.headers.get("content-length", 0))
                sink.raw.append(self.rfile.read(length))
                self.send_response(200)
                self.send_header("content-type", "application/x-protobuf")
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def spans(self) -> list[dict]:
        return [span for raw in list(self.raw) for span in _spans(raw)]

    def trace_of(self, request_id: str) -> tuple[dict, list[dict]]:
        """The request span with this id, and the attempts in its trace."""

        def lookup():
            spans = self.spans()
            root = next(
                (
                    s
                    for s in spans
                    if s["name"] == "request" and s["attributes"].get("request_id") == request_id
                ),
                None,
            )
            if root is None:
                return None
            return root, [
                s for s in spans if s["name"] != "request" and s["trace_id"] == root["trace_id"]
            ]

        return wait_for(lookup, 15, f"request span {request_id}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def otlp():
    sink = OtlpSink()
    yield sink
    sink.close()


def test_a_request_is_found_in_otel_by_the_id_in_its_response(stack, otlp):
    prompt = "the-prompt-must-not-be-exported"
    seeded = stack.seed()
    gw = stack.start_gateway(
        extra_env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp.url,
            "OTEL_BSP_SCHEDULE_DELAY": "50",
        }
    )
    _usage_catalog(stack)
    _wait_names(gw, seeded.a.secret, ["flaky-latest", "gpt-4o"])

    def call(name: str, **headers) -> httpx.Response:
        return gw.http.post(
            gw.url("/v1/chat/completions"),
            json={"model": name, "messages": [{"role": "user", "content": prompt}]},
            headers={"authorization": f"Bearer {seeded.a.secret}", **headers},
        )

    response = call("flaky-latest")
    assert response.status_code == 200, response.text
    request_id = response.headers["x-lumewarp-request-id"]
    root, attempts = otlp.trace_of(request_id)
    expected = {
        "request_id": request_id,
        "team_id": seeded.team_id,
        "key_id": seeded.a.id,
        "requested": "flaky-latest",
        "alias": "flaky-latest",
        "router": "flaky",
        "model": "gpt-4o",
        "upstream": "mock",
        "upstream_model": "up-a",
        "attempts": "2",
        "fallback": "true",
        "status": "200",
    }
    assert {k: root["attributes"].get(k) for k in expected} == expected, root
    assert root["attributes"]["config_version"], root
    # Every attempt: the model that failed, then the fallback that served.
    tried = sorted(
        (s["attributes"]["model"], s["attributes"]["upstream_model"], s["attributes"]["outcome"])
        for s in attempts
    )
    assert tried == [("broken", "up-broken", "connect_error"), ("gpt-4o", "up-a", "success")]
    # The same id finds the usage event, which has the summary but not the attempts.
    row = _row(stack, response)
    assert (row["model"], row["attempts"]) == ("gpt-4o", 2)

    # What must not be in a span: the prompt, the response, the API key.
    answer = response.json()["choices"][0]["message"]["content"]
    exported = b"".join(otlp.raw)
    assert_no_secret(
        exported,
        [prompt.encode(), answer.encode(), seeded.a.secret.encode(), seeded.a.secret[-12:].encode()],
        "a span contains request data or the API key",
    )

    # A client's traceparent with sampled=0 keeps the trace id and cannot erase the record.
    parent = "1" * 32
    response = call("gpt-4o", traceparent=f"00-{parent}-{'2' * 16}-00")
    assert response.status_code == 200, response.text
    root, attempts = otlp.trace_of(response.headers["x-lumewarp-request-id"])
    assert root["trace_id"] == parent, "stays in the client's trace"
    assert root["attributes"]["requested"] == "gpt-4o"
    assert "alias" not in root["attributes"] and "router" not in root["attributes"]
    assert [s["attributes"]["outcome"] for s in attempts] == ["success"]

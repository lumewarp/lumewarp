"""A router's reasoning default level: members, splits, fallbacks and the Playground.

A router's accepted levels are the intersection of what every member, split member and
fallback accepts: a passthrough or strip model accepts any level and a mapped model
exactly its fragment keys. The default level must be one of them, and a later write that
removes it is refused instead of making every request without reasoning_effort a 400.
"""

from __future__ import annotations

from sqlalchemy import select

from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import CatalogName
from tests.api.helpers import ADMIN_AUTH as H
from tests.api.helpers import auth, create_model, publish_zones, suffix, user_with_token
from tests.factories import make_alias, make_key, make_member, make_model, make_router, make_team

API = "/api/v1"


def _upstream(client) -> str:
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"}
    response = client.post(f"{API}/upstreams", json=body, headers=H)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _model(client, upstream_id: str, **reasoning) -> dict:
    return create_model(
        client,
        upstream_id,
        name=f"m-{suffix()}",
        upstream_model="Qwen/Qwen3-8B",
        reasoning=reasoning or None,
    )


def _mapped(client, upstream_id: str, levels: list[str]) -> dict:
    return _model(
        client,
        upstream_id,
        mode="mapped",
        fragments={
            level: {"chat_template_kwargs": {"enable_thinking": level != "none"}}
            for level in levels
        },
    )


def _router(client, members: list[dict], **body):
    if body.get("fallback_model_ids"):
        body.setdefault("retry", {})
    body = {
        "name": f"r-{suffix()}",
        "members": [{"model_id": member["id"], "weight": 1} for member in members],
        **body,
    }
    return client.post(f"{API}/routers", json=body, headers=H)


def _ok(response, code: int = 201) -> dict:
    assert response.status_code == code, response.text
    return response.json()


def _refused(response, needle: str) -> None:
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert needle in message, message


def test_accepted_levels_are_the_intersection_over_members_splits_and_fallbacks(client) -> None:
    upstream_id = _upstream(client)
    low_medium = _mapped(client, upstream_id, ["low", "medium"])
    medium_high = _mapped(client, upstream_id, ["medium", "high"])
    passthrough = _model(client, upstream_id)
    strip = _model(client, upstream_id, mode="strip")

    both = _ok(_router(client, [low_medium, passthrough]))
    assert both["accepted_reasoning_levels"] == ["low", "medium"]
    assert both["reasoning_default_level"] is None
    # strip accepts any level too, so it does not narrow the intersection.
    assert _ok(_router(client, [low_medium, strip]))["accepted_reasoning_levels"] == [
        "low",
        "medium",
    ]
    assert _ok(_router(client, [low_medium, medium_high, passthrough]))[
        "accepted_reasoning_levels"
    ] == ["medium"]
    # No mapped member: any level.
    assert _ok(_router(client, [passthrough, strip]))["accepted_reasoning_levels"] is None

    split = _ok(
        _router(
            client,
            [],
            traffic_split=[
                {"name": "stable", "weight": 1, "members": [{"model_id": low_medium["id"]}]},
                {"name": "canary", "weight": 1, "members": [{"model_id": medium_high["id"]}]},
            ],
        )
    )
    assert split["accepted_reasoning_levels"] == ["medium"]

    fallback = _ok(
        _router(
            client,
            [low_medium],
            fallback_model_ids=[medium_high["id"]],
            retry={},
        )
    )
    assert fallback["accepted_reasoning_levels"] == ["medium"]

    listed: dict[str, dict] = {}
    cursor = None
    while True:
        params = {"limit": 500, **({"cursor": cursor} if cursor else {})}
        page = client.get(f"{API}/routers", headers=H, params=params).json()
        listed.update({row["id"]: row for row in page["items"]})
        if not (cursor := page["next_cursor"]):
            break
    for router in (both, split, fallback):
        assert (
            listed[router["id"]]["accepted_reasoning_levels"] == router["accepted_reasoning_levels"]
        )


def test_default_level_outside_the_intersection_is_refused(client) -> None:
    upstream_id = _upstream(client)
    low_medium = _mapped(client, upstream_id, ["low", "medium"])
    name = f"r-{suffix()}"
    refused = _router(client, [low_medium], name=name, reasoning_default_level="high")
    _refused(refused, f"router {name!r}")
    _refused(refused, "not accepted by every member")
    assert name not in {
        row["name"] for row in client.get(f"{API}/routers", headers=H).json()["items"]
    }

    router = _ok(_router(client, [low_medium], name=name, reasoning_default_level="low"))
    assert router["reasoning_default_level"] == "low"
    assert router["accepted_reasoning_levels"] == ["low", "medium"]

    _refused(
        client.patch(
            f"{API}/routers/{router['id']}", json={"reasoning_default_level": "high"}, headers=H
        ),
        f"router {name!r}",
    )
    # An invalid level name is refused by the field itself.
    assert (
        client.patch(
            f"{API}/routers/{router['id']}",
            json={"reasoning_default_level": "not a level"},
            headers=H,
        ).status_code
        == 422
    )
    cleared = _ok(
        client.patch(
            f"{API}/routers/{router['id']}", json={"reasoning_default_level": None}, headers=H
        ),
        200,
    )
    assert cleared["reasoning_default_level"] is None
    restored = _ok(
        client.patch(
            f"{API}/routers/{router['id']}",
            json={"reasoning_default_level": "medium"},
            headers=H,
        ),
        200,
    )
    assert restored["reasoning_default_level"] == "medium"


def test_router_update_that_drops_its_default_is_refused(client) -> None:
    upstream_id = _upstream(client)
    low = _mapped(client, upstream_id, ["low"])
    high = _mapped(client, upstream_id, ["high"])
    router = _ok(_router(client, [low], reasoning_default_level="low"))
    name = router["name"]

    _refused(
        client.patch(
            f"{API}/routers/{router['id']}", json={"members": [{"model_id": high["id"]}]}, headers=H
        ),
        f"router {name!r}",
    )
    # Changing the members and the level together is allowed.
    moved = _ok(
        client.patch(
            f"{API}/routers/{router['id']}",
            json={
                "members": [{"model_id": high["id"]}],
                "reasoning_default_level": "high",
            },
            headers=H,
        ),
        200,
    )
    assert moved["members"][0]["model_id"] == high["id"]
    assert moved["accepted_reasoning_levels"] == ["high"]

    # A fallback is served too, so its levels narrow the router's set.
    router = _ok(
        _router(
            client,
            [low],
            fallback_model_ids=[low["id"]],
            reasoning_default_level="low",
            retry={},
        )
    )
    _refused(
        client.patch(
            f"{API}/routers/{router['id']}",
            json={"fallback_model_ids": [high["id"]]},
            headers=H,
        ),
        f"router {router['name']!r}",
    )


def test_model_update_that_removes_a_referencing_router_default_is_refused(client) -> None:
    upstream_id = _upstream(client)
    model = _mapped(client, upstream_id, ["low", "medium"])
    router = _ok(_router(client, [model], reasoning_default_level="low"))

    _refused(
        client.patch(
            f"{API}/models/{model['id']}",
            json={"reasoning": {"mode": "mapped", "fragments": {"high": {}}}},
            headers=H,
        ),
        f"router {router['name']!r}",
    )
    stored = client.get(f"{API}/models/{model['id']}", headers=H).json()
    assert stored["reasoning"]["fragments"] == model["reasoning"]["fragments"]

    # A strip model accepts any level, so this change keeps the router's default valid.
    _ok(
        client.patch(
            f"{API}/models/{model['id']}", json={"reasoning": {"mode": "strip"}}, headers=H
        ),
        200,
    )
    # From any level back to mapped without `low`: refused again.
    _refused(
        client.patch(
            f"{API}/models/{model['id']}",
            json={"reasoning": {"mode": "mapped", "fragments": {"high": {}}}},
            headers=H,
        ),
        f"router {router['name']!r}",
    )
    # Keeping `low` is fine.
    kept = _ok(
        client.patch(
            f"{API}/models/{model['id']}",
            json={"reasoning": {"mode": "mapped", "fragments": {"low": {}, "high": {}}}},
            headers=H,
        ),
        200,
    )
    assert sorted(kept["reasoning"]["fragments"]) == ["high", "low"]
    still = client.get(f"{API}/routers/{router['id']}", headers=H).json()
    assert still["reasoning_default_level"] == "low"


async def _playground_key(committed):
    team = await make_team(committed)
    user, _pat = await user_with_token(committed)
    await make_member(committed, team, user)
    cookie = await sessions.create_session(committed, user.id, 3600)
    await make_key(committed, team, user)
    await committed.commit()
    return auth("session:" + cookie)


async def test_compiled_snapshot_carries_the_level_and_capability(
    client, committed, api_settings
) -> None:
    upstream_id = _upstream(client)
    model = _mapped(client, upstream_id, ["low", "medium"])
    named = _ok(_router(client, [model], name=f"r-{suffix()}", reasoning_default_level="medium"))
    plain = _ok(_router(client, [model], name=f"r-{suffix()}"))

    committed.expire_all()
    compiled = await compile_zone(committed, api_settings.zone, require_local_membership=True)
    routers = {router.name: router for router in compiled.snapshot.routers}
    assert routers[named["name"]].reasoning_default_level == "medium"
    assert routers[plain["name"]].reasoning_default_level == ""
    assert "router_reasoning_default" in compiled.snapshot.required_capabilities


async def test_playground_lists_routers_and_their_aliases(client, committed, api_engine) -> None:
    headers = await _playground_key(committed)
    fragments = {"low": {"reasoning_effort": "low"}, "high": {"reasoning_effort": "high"}}
    mapped = await make_model(
        committed,
        f"m-{suffix()}",
        reasoning_mode="mapped",
        reasoning_fragments=fragments,
    )
    plain = await make_model(committed, f"m-{suffix()}")
    router = await make_router(
        committed,
        f"r-{suffix()}",
        [mapped, plain],
        reasoning_default_level="low",
    )
    alias = await make_alias(committed, f"a-{suffix()}", router)
    any_router = await make_router(committed, f"r-{suffix()}", [plain])
    custom_default = await make_router(
        committed, f"r-{suffix()}", [plain], reasoning_default_level="xhigh"
    )
    plain_alias = await make_alias(committed, f"a-{suffix()}", plain)
    await committed.commit()

    async def names(*rows) -> list[str]:
        out = []
        for row in rows:
            out.append(
                await committed.scalar(select(CatalogName.name).where(CatalogName.id == row.id))
            )
        return out

    (
        router_name,
        alias_name,
        any_router_name,
        custom_name,
        plain_name,
        plain_alias_name,
    ) = await names(router, alias, any_router, custom_default, plain, plain_alias)
    await publish_zones(api_engine)
    body = client.get(f"{API}/playground", headers=headers).json()
    assert {router_name, alias_name, any_router_name, custom_name} <= set(body["keys"][0]["models"])
    reasoning = body["reasoning"]
    assert reasoning[router_name] == {"levels": ["high", "low"], "default_level": "low"}
    assert reasoning[alias_name] == reasoning[router_name]
    # A router with no mapped member accepts any level: offer the standard OpenAI ones.
    assert reasoning[any_router_name] == {
        "levels": ["minimal", "low", "medium", "high"],
        "default_level": None,
    }
    # A default outside that list is still offered.
    assert reasoning[custom_name] == {
        "levels": ["minimal", "low", "medium", "high", "xhigh"],
        "default_level": "xhigh",
    }
    # Plain models and their aliases still have no picker.
    assert plain_name not in reasoning
    assert plain_alias_name not in reasoning

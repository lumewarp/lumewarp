"""Rollback must restore source state, not merely copy the old published payload."""

import pytest
from sqlalchemy import delete, select

from lumewarp_control_plane.compiler import compile_scope, publish_pending, rollback_routing
from lumewarp_control_plane.models import (
    Alias,
    CatalogName,
    Model,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
    StickyBy,
    StreamUsageMode,
    Upstream,
    UpstreamKind,
)
from tests import factories
from tests.api.helpers import report_strategies
from tests.compiler.helpers import SCOPE, add_router, load_version, seed_scope, touch

GROUPS = [
    "upstream",
    "stored_api_key",
    "health_check",
    "health_defaults",
    "circuit_breaker",
    "breaker_defaults",
    "metrics_url",
    "model",
    "price",
    "free_price",
    "model_settings",
    "strategy",
    "custom_strategy",
    "custom_params",
    "retry",
    "retry_defaults",
    "fallbacks",
    "traffic_split",
    "sticky",
    "sticky_header",
    "members",
    "member_weights",
    "alias",
    "model_name",
]


async def _seed(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "public", [("a", None), ("b", None)], upstream="primary")
        await add_router(session, "fallback-one", [("c", None)], upstream="secondary")
        await add_router(session, "fallback-two", [("b", None)], upstream="primary")
    # What the custom groups roll back to still has to exist on a gateway.
    async with factory() as session:
        any_settings = ({"type": "object"}, {"type": "object"})
        await report_strategies(
            session, SCOPE, "g", {"first_healthy": any_settings, "header_pin": any_settings}
        )


async def _set_group(session, group, configured):
    upstream = await session.scalar(select(Upstream).where(Upstream.name == "primary"))
    router = await session.scalar(select(Router).where(Router.name == "public"))
    models = {d.name: d for d in await session.scalars(select(Model))}
    model = models.get("a") or models["a-renamed"]
    if group == "upstream":
        upstream.kind = UpstreamKind.OPENAI_COMPATIBLE if configured else UpstreamKind.VLLM
        upstream.base_url = "https://alternate/v1" if configured else "http://primary/v1"
        upstream.api_key_ref = "TEST_UPSTREAM_KEY" if configured else None
        upstream.timeout_ms = 1234 if configured else 0
        upstream.stream_usage_mode = (
            StreamUsageMode.PASSTHROUGH if configured else StreamUsageMode.INCLUDE_USAGE
        )
    elif group == "stored_api_key":
        # Ciphertext only; the catalog sidecar must carry the bytes through JSON.
        upstream.api_key_ciphertext = (b"\x01" + bytes(range(40))) if configured else None
    elif group == "model_settings":
        model.max_output_tokens = 512 if configured else None
        model.default_body = {"top_k": 20, "extra_body": {"x": [1, 2]}} if configured else None
        model.reasoning_mode = "mapped" if configured else "passthrough"
        model.reasoning_fragments = (
            {"none": {"chat_template_kwargs": {"enable_thinking": False}}} if configured else None
        )
        model.reasoning_default_level = "none" if configured else None
        model.chat_check_interval_ms = 30000 if configured else None
        model.chat_check_timeout_ms = 5000 if configured else None
        model.chat_check_unhealthy_threshold = 2 if configured else None
    elif group in ("health_check", "health_defaults"):
        explicit = configured and group == "health_check"
        upstream.health_check_interval_ms = 5000 if configured else None
        upstream.health_check_timeout_ms = 1200 if configured else None
        upstream.health_check_url = "http://ready.internal/ready" if explicit else None
        upstream.health_check_unhealthy_threshold = 4 if explicit else None
        upstream.health_check_healthy_threshold = 2 if explicit else None
    elif group in ("circuit_breaker", "breaker_defaults"):
        explicit = configured and group == "circuit_breaker"
        upstream.circuit_breaker_enabled = configured
        upstream.circuit_failure_threshold = 7 if explicit else None
        upstream.circuit_open_ms = 12345 if explicit else None
    elif group == "metrics_url":
        upstream.metrics_url = "http://metrics:9000/custom" if configured else None
    elif group == "model":
        secondary = await session.scalar(select(Upstream).where(Upstream.name == "secondary"))
        model.upstream_id = secondary.id if configured else upstream.id
        model.upstream_model = "alternate-name" if configured else "a-name"
    elif group in ("price", "free_price"):
        model.price_input_nano_usd_per_mtok = (
            (0 if group == "free_price" else 123) if configured else None
        )
        model.price_output_nano_usd_per_mtok = (
            (0 if group == "free_price" else 456) if configured else None
        )
    elif group == "strategy":
        router.strategy = "lowest_latency" if configured else "weighted"
    elif group in ("custom_strategy", "custom_params"):
        custom = configured or group == "custom_params"
        name = "header_pin" if configured else "first_healthy"
        router.strategy = name if custom else "weighted"
        router.params = {"headers": ["x-pin"], "pin": "a" if configured else "b"} if custom else {}
    elif group in ("retry", "retry_defaults"):
        explicit = configured and group == "retry"
        router.retry_enabled = configured
        router.retry_max_attempts = 4 if explicit else None
        router.retry_on_status = [429, 500, 503] if explicit else []
        router.retry_total_timeout_ms = 1234 if explicit else 0
    elif group == "fallbacks":
        router.retry_enabled = True
        router.retry_max_attempts = 3
        await session.execute(delete(RouterFallback).where(RouterFallback.router_id == router.id))
        if configured:
            for position, name in enumerate(("c", "b")):
                session.add(
                    RouterFallback(router_id=router.id, model_id=models[name].id, position=position)
                )
    elif group in ("traffic_split", "sticky", "sticky_header"):
        await session.execute(delete(RouterSplit).where(RouterSplit.router_id == router.id))
        router.sticky = StickyBy.NONE
        router.sticky_header = None
        if configured or group != "traffic_split":
            if group == "traffic_split":
                router.sticky = StickyBy.HEADER
                router.sticky_header = "x-session"
            elif group == "sticky":
                router.sticky = StickyBy.TEAM if configured else StickyBy.KEY
            else:
                router.sticky = StickyBy.HEADER
                router.sticky_header = "x-one" if configured else "x-two"
            for position, (name, weight, members) in enumerate(
                (("canary", 1, ("b", "a")), ("stable", 9, ("a", "b")))
            ):
                split = RouterSplit(
                    router_id=router.id, name=name, weight=weight, position=position
                )
                session.add(split)
                await session.flush()
                for dep_position, member in enumerate(members):
                    session.add(
                        RouterSplitMember(
                            split_id=split.id,
                            model_id=models[member].id,
                            position=dep_position,
                        )
                    )
    elif group == "members":
        await session.execute(delete(RouterMember).where(RouterMember.router_id == router.id))
        for position, name in enumerate(("b", "a") if configured else ("a", "b")):
            session.add(
                RouterMember(router_id=router.id, model_id=models[name].id, position=position)
            )
    elif group == "member_weights":
        member = await session.get(RouterMember, (router.id, model.id))
        member.weight = 7 if configured else 1
    elif group == "alias":
        alias = await session.scalar(select(Alias))
        target = router if configured else model
        if alias is None:
            await factories.make_alias(session, "coder", target)
        else:
            alias.target_name_id, alias.target_kind = target.id, target.kind
    elif group == "model_name":
        name = await session.get(CatalogName, model.id)
        # Compared by id across both directions: `models` above is keyed by the old name.
        name.name = "a-renamed" if configured else "a"
    else:
        raise AssertionError(group)


async def _publish(factory):
    await touch(factory)
    return await publish_pending(factory, SCOPE, require_local_membership=True)


async def _rollback_and_compare(factory):
    before = await load_version(factory, 1)
    changed = await load_version(factory, 2)
    fields = ("upstreams", "models", "aliases", "routers", "required_capabilities")
    assert any(list(getattr(before, f)) != list(getattr(changed, f)) for f in fields)
    async with factory() as session, session.begin():
        assert await rollback_routing(session, SCOPE, 1, require_local_membership=True) == 3
    restored = await load_version(factory, 3)
    async with factory() as session:
        recompiled = (await compile_scope(session, SCOPE, require_local_membership=True)).snapshot
    for field in fields:
        assert list(getattr(restored, field)) == list(getattr(before, field)), field
        assert list(getattr(recompiled, field)) == list(getattr(before, field)), field
    # The next ordinary publication must also retain the restored source routing.
    assert await _publish(factory) is None


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("configured", [False, True], ids=["absent_or_first", "present_or_second"])
async def test_rollback_restores_routing_field_group(factory, group, configured):
    await _seed(factory)
    async with factory() as session, session.begin():
        await _set_group(session, group, configured)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await _set_group(session, group, not configured)
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)


async def test_rollback_removes_new_routing_graph_without_restrict_conflicts(factory):
    await _seed(factory)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await add_router(session, "late", [("late-model", None)], upstream="late-upstream")
        router = await session.scalar(select(Router).where(Router.name == "public"))
        model = await session.scalar(select(Model).where(Model.name == "late-model"))
        router.retry_enabled = True
        router.retry_max_attempts = 2
        session.add(RouterFallback(router_id=router.id, model_id=model.id, position=0))
        split = RouterSplit(router_id=router.id, name="late", weight=1, position=0)
        session.add(split)
        await session.flush()
        session.add(RouterSplitMember(split_id=split.id, model_id=model.id, position=0))
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)


async def test_rollback_recreates_removed_routers_before_fallback_edges(factory):
    await _seed(factory)
    async with factory() as session, session.begin():
        await _set_group(session, "fallbacks", True)
        await _set_group(session, "traffic_split", True)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await session.execute(delete(RouterFallback))
        await session.execute(delete(RouterSplit))
        await session.execute(
            delete(CatalogName).where(CatalogName.name.in_(["fallback-one", "fallback-two"]))
        )
        await session.execute(delete(CatalogName).where(CatalogName.name == "c"))
        await session.execute(delete(Upstream).where(Upstream.name == "secondary"))
        router = await session.scalar(select(Router))
        router.sticky = StickyBy.NONE
        router.sticky_header = None
        router.retry_enabled = False
        router.retry_max_attempts = None
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)

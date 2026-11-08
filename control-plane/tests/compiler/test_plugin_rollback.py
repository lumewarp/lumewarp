"""A rollback meets the same plugin rules as a router write: the gateways may have changed
since the version it restores (docs/plugins.md, "Control-plane validation").
"""

import pytest
from sqlalchemy import delete, func, select

from lumewarp_control_plane.compiler import publish_pending, rollback_routing
from lumewarp_control_plane.compiler.plugins import PluginRejected
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigVersion,
    Router,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
)
from tests import factories
from tests.api.helpers import report_plugins
from tests.compiler.helpers import SCOPE, seed_scope, touch

CUSTOM = "custom"
ANY = {"type": "object"}
LIMIT = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 0, "maximum": 5}},
    "additionalProperties": False,
}
PRIORITY = {
    "type": "object",
    "properties": {"priority": {"type": "integer", "minimum": 1, "maximum": 5}},
    "additionalProperties": False,
}


async def _report(factory: SessionFactory, gateway: str, strategies: dict, **kwargs) -> None:
    async with factory() as session:
        await report_plugins(session, SCOPE, gateway, strategies, **kwargs)


async def _publish_custom_then_weighted(
    factory: SessionFactory,
    *,
    params: dict | None = None,
    member_params: dict | None = None,
    split_member_params: dict | None = None,
) -> None:
    """v1 has the router on `custom` with these settings, v2 on weighted with none."""
    await seed_scope(factory)
    async with factory() as s, s.begin():
        model = await factories.make_model(s, "a")
        router = await factories.make_router(
            s, "r", [(model, 1)], strategy=CUSTOM, params=params or {}
        )
        member = await s.scalar(select(RouterMember))
        member.params = member_params or {}
        if split_member_params is not None:
            split = RouterSplit(router_id=router.id, name="canary", weight=1, position=0)
            s.add(split)
            await s.flush()
            s.add(
                RouterSplitMember(
                    split_id=split.id,
                    model_id=model.id,
                    position=0,
                    params=split_member_params,
                )
            )
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as s, s.begin():
        await s.execute(delete(RouterSplit))
        router = await s.scalar(select(Router))
        router.strategy, router.params = "weighted", {}
        member = await s.scalar(select(RouterMember))
        member.params = {}
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2


async def _rollback(factory: SessionFactory, **kwargs) -> int:
    async with factory() as s, s.begin():
        return await rollback_routing(s, SCOPE, 1, require_local_membership=True, **kwargs)


async def _refused(factory: SessionFactory, *needles: str, **kwargs) -> PluginRejected:
    with pytest.raises(PluginRejected) as error:
        await _rollback(factory, **kwargs)
    for needle in needles:
        assert needle in error.value.message, error.value.message
    # All of it undone: the routers, the versions, the audit.
    async with factory() as s:
        router = await s.scalar(select(Router))
        assert router.strategy == "weighted"
        assert await s.scalar(select(func.max(ConfigVersion.version))) == 2
        assert not await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "router.force_save")
        )
    return error.value


async def test_a_strategy_no_gateway_has_any_more_is_rejected(factory: SessionFactory):
    await _publish_custom_then_weighted(factory)
    await _report(factory, "g", {})
    rejected = await _refused(factory, "no gateway reports", "router 'r'")
    assert rejected.field == "strategy"
    # force is for connected gateways that lack it, not for a strategy nobody has.
    await _refused(factory, "no gateway reports", force=True)


async def test_params_outside_a_narrowed_schema_are_rejected(factory: SessionFactory):
    await _publish_custom_then_weighted(factory, params={"limit": 10})
    await _report(factory, "g", {CUSTOM: (LIMIT, ANY)})
    rejected = await _refused(factory, "router 'r'", "limit")
    assert rejected.field == "params"
    await _refused(factory, "limit", force=True)


async def test_member_params_are_checked(factory: SessionFactory):
    await _publish_custom_then_weighted(factory, member_params={"priority": 9})
    await _report(factory, "g", {CUSTOM: (ANY, PRIORITY)})
    await _refused(factory, "members[0].params")


async def test_split_member_params_are_checked(factory: SessionFactory):
    await _publish_custom_then_weighted(factory, split_member_params={"priority": 9})
    await _report(factory, "g", {CUSTOM: (ANY, PRIORITY)})
    await _refused(factory, "traffic_split[0].members[0].params")


async def test_a_connected_gateway_lacking_it_needs_force_and_force_is_audited(
    factory: SessionFactory,
):
    await _publish_custom_then_weighted(factory)
    await _report(factory, "has-it", {CUSTOM: (ANY, ANY)}, connected=True)
    await _report(factory, "lacks-it", {}, connected=True)
    await _refused(factory, "lacks-it", "force")

    assert await _rollback(factory, force=True) == 3
    async with factory() as s:
        router = await s.scalar(select(Router))
        assert router.strategy == CUSTOM
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "router.force_save"))
    assert audit.details["missing_gateways"] == ["lacks-it"]
    assert audit.details["strategy"] == CUSTOM
    assert audit.resource_id == str(router.id)


async def test_valid_settings_roll_back_without_force_or_audit(factory: SessionFactory):
    await _publish_custom_then_weighted(
        factory,
        params={"limit": 5},
        member_params={"priority": 5},
        split_member_params={"priority": 1},
    )
    await _report(factory, "g", {CUSTOM: (LIMIT, PRIORITY)}, connected=True)
    assert await _rollback(factory) == 3
    async with factory() as s:
        assert not await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "router.force_save")
        )

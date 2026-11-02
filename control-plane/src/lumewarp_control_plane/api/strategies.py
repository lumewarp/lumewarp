"""Read-only list of routing strategies: built-ins and what gateways report."""

from __future__ import annotations

from fastapi import APIRouter

from lumewarp_control_plane.api.common import SessionDep, SettingsDep
from lumewarp_control_plane.api.schemas import Page, StrategyGatewayOut, StrategyOut
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.strategy import BUILTIN_STRATEGIES, load_strategies

router = APIRouter(tags=["routing"])


@router.get("/strategies", response_model=Page[StrategyOut], operation_id="listStrategies")
async def list_strategies(
    authz: Authz, session: SessionDep, settings: SettingsDep
) -> Page[StrategyOut]:
    await authz.require(Action.ROUTING_READ)
    registry = await load_strategies(session, settings.scope)
    items = []
    for name in registry.names():
        current = registry.current(name) or {}
        items.append(
            StrategyOut(
                name=name,
                title=current.get("title", ""),
                description=current.get("description", ""),
                uses_weight=current.get("uses_weight", False),
                needs=current.get("needs", []),
                params_schema=current.get("params_schema"),
                member_schema=current.get("member_schema"),
                schema_problems=current.get("schema_problems", []),
                builtin=name in BUILTIN_STRATEGIES,
                gateways=[
                    StrategyGatewayOut(
                        gateway_id=support.gateway_id,
                        connected=support.connected,
                        digest=support.digest,
                        seen_at=support.seen_at,
                    )
                    for support in registry.support(name)
                ],
                missing_gateways=registry.missing(name),
            )
        )
    return Page[StrategyOut](items=items, next_cursor=None)

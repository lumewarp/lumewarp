"""Read-only list of gateway plugins: built-ins and what gateways report, per kind.

Every plugin is a descriptor a gateway sent in `Hello.plugins` (docs/plugins.md) or a
built-in from the committed manifest. The control plane treats a descriptor as untrusted
data: the UI renders its title, description and schema as plain text.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from lumewarp_control_plane.api.common import SessionDep, SettingsDep
from lumewarp_control_plane.api.schemas import Page, PluginGatewayOut, PluginOut
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.plugins import load_plugins
from lumewarp_control_plane.models import PluginKind

router = APIRouter(tags=["routing"])


@router.get("/plugins", response_model=Page[PluginOut], operation_id="listPlugins")
async def list_plugins(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    kind: Annotated[
        PluginKind | None,
        Query(description="Only this kind; omitted lists all three."),
    ] = None,
) -> Page[PluginOut]:
    """The plugins a write may name, and which connected instances have each.

    A plugin counts only if every connected instance of a gateway id reports it: one entry
    per instance in `gateways`, connected first; `missing_gateways` is what a forced save is
    saving past. A schema the control plane cannot use is null, with the reason in
    `schema_problems`: only the gateway's own `configure` validates those settings.
    """
    await authz.require(Action.ROUTING_READ)
    registry = await load_plugins(session, settings.scope)
    items = []
    for current_kind in [kind] if kind is not None else list(PluginKind):
        for name in registry.names(current_kind):
            current = registry.current(current_kind, name) or {}
            items.append(
                PluginOut(
                    kind=current_kind,
                    name=name,
                    title=current.get("title", ""),
                    description=current.get("description", ""),
                    builtin=name in registry.builtin_names(current_kind),
                    uses_weight=current.get("uses_weight", False),
                    needs=current.get("needs", []),
                    params_schema=current.get("params_schema"),
                    member_schema=current.get("member_schema"),
                    schema_problems=current.get("schema_problems", []),
                    wasm_sha256=current.get("wasm_sha256", ""),
                    gateways=[
                        PluginGatewayOut(
                            gateway_id=support.gateway_id,
                            instance_id=support.instance_id,
                            connected=support.connected,
                            digest=support.digest,
                            seen_at=support.seen_at,
                        )
                        for support in registry.support(current_kind, name)
                    ],
                    missing_gateways=registry.missing(current_kind, name),
                )
            )
    return Page[PluginOut](items=items, next_cursor=None)

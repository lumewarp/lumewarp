"""Zone rows declared by the environment, created at startup.

A zone exists because the control plane was started with its
`LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` (or because it is the default `LUMEWARP_ZONE`). There is
no zone-create API: removing the variable does not delete the zone, its keys, deployments and
usage stay, and the compiler keeps compiling it; nobody can fetch its config until the
variable returns.
"""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import Zone
from lumewarp_control_plane.settings import Settings

log = logging.getLogger(__name__)


async def ensure_zones(factory: SessionFactory, settings: Settings) -> None:
    """Create the default zone and one row per env-token zone if they do not exist.

    `ON CONFLICT DO NOTHING` leaves an existing row alone, so its name, URL, key rules and
    `source_revision` are not reset, and an archived zone is never un-archived; no config
    version is published by the insert. The Playground URL is seeded from
    `LUMEWARP_PLAYGROUND_GATEWAY_URL` on the default zone's creation only. An env token for
    an archived zone is kept (the zone still refuses handshakes) and logged.
    """
    zones = [settings.zone, *sorted(set(settings.gateway_zone_tokens) - {settings.zone})]
    async with factory() as session, session.begin():
        await session.execute(
            insert(Zone)
            .values(
                [
                    {
                        "id": zone,
                        "name": zone,
                        "playground_gateway_url": (
                            settings.playground_gateway_url if zone == settings.zone else None
                        ),
                    }
                    for zone in zones
                ]
            )
            .on_conflict_do_nothing()
        )
        archived = list(
            (
                await session.scalars(
                    select(Zone.id).where(
                        Zone.id.in_(settings.gateway_zone_tokens),
                        Zone.archived_at.is_not(None),
                    )
                )
            ).all()
        )
    for zone in archived:
        log.warning(
            "zone %s has a gateway token configured but is archived; its gateways are refused",
            zone,
        )

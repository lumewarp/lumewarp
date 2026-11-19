"""Two zones in one control plane: streams, quarantine and recompiles stay per zone."""

from __future__ import annotations

import asyncio

import grpc
import pytest
from sqlalchemy import select

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane.compiler import publish_pending
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import ConfigOutbox, GatewayQuarantine, Zone
from tests.grpc.test_config_service import _open_stream, _raw_stream, _seed, _start


def _zones() -> tuple[str, str]:
    suffix = uuid7().hex[-8:]
    return f"prod-{suffix}", f"dev-{suffix}"


async def _quarantined(factory: SessionFactory) -> set[str]:
    async with factory() as session:
        return set((await session.scalars(select(GatewayQuarantine.zone))).all())


@pytest.mark.asyncio
async def test_each_zone_stream_receives_its_own_snapshot(session_factory: SessionFactory):
    prod, dev = _zones()
    _, prod_tokens = await _seed(session_factory, scope_name=prod, versions=(1,))
    _, dev_tokens = await _seed(session_factory, scope_name=dev, versions=(2,))
    server, channel, stub = await _start(session_factory, prod)
    prod_call = await _open_stream(stub, prod_tokens["gateway-a"], "gw-prod", zone=prod)
    dev_call = await _open_stream(stub, dev_tokens["gateway-a"], "gw-dev", zone=dev)
    try:
        prod_update = await asyncio.wait_for(prod_call.read(), 2)
        dev_update = await asyncio.wait_for(dev_call.read(), 2)
        assert prod_update.WhichOneof("update") == "full"
        assert (prod_update.full.zone, prod_update.full.version) == (prod, 1)
        assert dev_update.WhichOneof("update") == "full"
        assert (dev_update.full.zone, dev_update.full.version) == (dev, 2)

        # A Hello of another zone cannot ride on this zone's metadata.
        crossed = await _raw_stream(stub, prod_tokens["gateway-a"], prod, hello_zone=dev)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await crossed.read()
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    finally:
        prod_call.cancel()
        dev_call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_a_nack_in_one_zone_does_not_quarantine_the_other(
    session_factory: SessionFactory,
):
    prod, dev = _zones()
    _, prod_tokens = await _seed(session_factory, scope_name=prod, versions=(1,))
    _, dev_tokens = await _seed(session_factory, scope_name=dev, versions=(1,))
    server, channel, stub = await _start(session_factory, prod)
    dev_call = await _open_stream(stub, dev_tokens["gateway-a"], "gw-dev", zone=dev)
    try:
        assert (await asyncio.wait_for(dev_call.read(), 2)).full.zone == dev
        await dev_call.write(
            cpb.GatewayMessage(
                nack=cpb.Nack(version=1, have_version=0, code=cpb.NACK_CODE_INVALID_CONFIG)
            )
        )
        async with asyncio.timeout(5):
            while not await _quarantined(session_factory):
                await asyncio.sleep(0.02)
        assert await _quarantined(session_factory) == {dev}

        # Prod is unaffected: a fresh prod gateway still gets its own version 1.
        prod_call = await _open_stream(stub, prod_tokens["gateway-a"], "gw-prod", zone=prod)
        try:
            prod_update = await asyncio.wait_for(prod_call.read(), 2)
            assert (prod_update.full.zone, prod_update.full.version) == (prod, 1)
        finally:
            prod_call.cancel()
    finally:
        dev_call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_a_global_write_publishes_in_every_zone_and_a_zone_write_in_one(
    session_factory: SessionFactory,
):
    prod, dev = _zones()
    await _seed(session_factory, scope_name=prod, versions=(1,))
    await _seed(session_factory, scope_name=dev, versions=(1,))
    async with session_factory() as session, session.begin():
        # The seeded rows claim revision 1; put the zone rows at the same point.
        for zone in (prod, dev):
            (await session.get(Zone, zone)).source_revision = 1

    # A hierarchy-wide write (org/team/grant change): one outbox row per live zone.
    async with session_factory() as session, session.begin():
        revision = await outbox.enqueue_recompile(session, "test.global")
    async with session_factory() as session:
        zones = set(
            (
                await session.scalars(
                    select(ConfigOutbox.zone).where(
                        ConfigOutbox.status == "pending", ConfigOutbox.zone.in_([prod, dev])
                    )
                )
            ).all()
        )
        assert zones == {prod, dev}
        assert (await session.get(Zone, prod)).source_revision == revision
        assert (await session.get(Zone, dev)).source_revision == revision

    assert await publish_pending(session_factory, prod, require_local_membership=True) == 2
    assert await publish_pending(session_factory, dev, require_local_membership=True) == 2

    # A zone-bound write recompiles only that zone.
    async with session_factory() as session, session.begin():
        await outbox.enqueue_recompile(session, "test.zone-only", prod)
    async with session_factory() as session:
        pending = (
            await session.scalars(
                select(ConfigOutbox.zone).where(
                    ConfigOutbox.status == "pending", ConfigOutbox.zone.in_([prod, dev])
                )
            )
        ).all()
        assert set(pending) == {prod}

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import uuid
from datetime import UTC, datetime

import grpc
import pytest
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    config_service_pb2 as cpb,
)
from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    config_service_pb2_grpc as cpb_grpc,
)
from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    snapshot_pb2,
)
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2, usage_pb2_grpc
from lumewarp_control_plane.compiler.plugins import (
    MAX_GATEWAY_PLUGINS,
    load_plugins,
)
from lumewarp_control_plane.compiler.publish import compact_versions
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.grpc import (
    ProbeGatewayGone,
    ProbeTimeout,
    ProbeUnavailable,
    bound_port,
    peer_address,
    registry_for,
    serve,
)
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ConfigVersion,
    ConfigVersionKind,
    GatewayPlugin,
    GatewayQuarantine,
    GatewaySession,
    PluginKind,
    Zone,
)
from lumewarp_control_plane.settings import Settings

CATASTROPHIC = json.dumps(
    {"type": "object", "properties": {"a": {"type": "string", "pattern": "^(a+)+$"}}}
)


# One fixed token per zone; `_seed` registers it so `_start` and `_open_stream` find it.
_ZONE_TOKEN: dict[str, str] = {}
_TOKEN_ZONE: dict[str, str] = {}


async def _seed(
    factory: SessionFactory,
    *,
    scope_name: str | None = None,
    versions: tuple[int, ...] = (1,),
    gateways: tuple[str, ...] = ("gateway-a",),
) -> tuple[str, dict[str, str]]:
    scope_name = scope_name or f"grpc-{uuid7().hex}"
    secret = secrets.token_urlsafe(32)
    _ZONE_TOKEN[scope_name] = secret
    _TOKEN_ZONE[secret] = scope_name
    tokens = {gateway_id: secret for gateway_id in gateways}
    async with factory() as session, session.begin():
        session.add(Zone(id=scope_name, name=scope_name))
        await session.flush()
        for version in versions:
            snapshot = snapshot_pb2.Snapshot(
                zone=scope_name,
                version=version,
                created_at_unix_ms=version,
            )
            payload = snapshot.SerializeToString(deterministic=True)
            session.add(
                ConfigVersion(
                    zone=scope_name,
                    version=version,
                    payload=payload,
                    digest=hashlib.sha256(payload).digest(),
                    size_bytes=len(payload),
                    key_count=0,
                    source_revision=version,
                    kind=ConfigVersionKind.COMPILE,
                )
            )
    return scope_name, tokens


async def _publish(factory: SessionFactory, scope: str, version: int) -> None:
    async with factory() as session, session.begin():
        snapshot = snapshot_pb2.Snapshot(
            zone=scope,
            version=version,
            created_at_unix_ms=version,
        )
        payload = snapshot.SerializeToString(deterministic=True)
        session.add(
            ConfigVersion(
                zone=scope,
                version=version,
                payload=payload,
                digest=hashlib.sha256(payload).digest(),
                size_bytes=len(payload),
                key_count=0,
                source_revision=version,
                kind=ConfigVersionKind.COMPILE,
            )
        )


def _stub_diff(monkeypatch):
    from lumewarp_control_plane import compiler

    def diff(old, new):
        return cpb.Delta(base_version=old.version, version=new.version)

    monkeypatch.setattr(compiler, "diff", diff)


def _settings(scope: str, **overrides) -> Settings:
    return Settings(
        zone=scope,
        grpc_bind="127.0.0.1:0",
        background_enabled=False,
        gateway_zone_tokens={
            zone: hashlib.sha256(token.encode()).digest() for zone, token in _ZONE_TOKEN.items()
        },
        **overrides,
    )


async def _start(factory: SessionFactory, scope: str, *, ack_timeout: float = 1.0):
    server = await serve(_settings(scope, ack_timeout_seconds=ack_timeout), factory)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    return server, channel, cpb_grpc.ConfigServiceStub(channel)


def _digest(scope: str, version: int) -> bytes:
    """The digest _seed/_publish store for a version."""
    payload = snapshot_pb2.Snapshot(
        zone=scope, version=version, created_at_unix_ms=version
    ).SerializeToString(deterministic=True)
    return hashlib.sha256(payload).digest()


class _Stream:
    """A raw gRPC call plus the gateway id the control plane assigned before any update."""

    def __init__(self, call, gateway_id: str) -> None:
        self.call = call
        self.gateway_id = gateway_id

    async def read(self):
        return await self.call.read()

    async def write(self, message):
        return await self.call.write(message)

    async def done_writing(self):
        return await self.call.done_writing()

    def cancel(self):
        return self.call.cancel()


def _metadata(token: str, zone: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"), ("x-lumewarp-zone", zone))


async def _open_stream(
    stub,
    token: str,
    gateway_id: str,
    have_version: int = 0,
    zone: str | None = None,
    have_digest: bytes | None = None,
    plugins: tuple[cpb.PluginDescriptor, ...] = (),
    capabilities: tuple[str, ...] = (),
    gateway_version: str = "test-gateway",
    shared_limits: cpb.SharedLimits | None = None,
):
    """Hello, then the Welcome; `gateway_id` is the hostname hint, not an id."""
    zone = zone or _TOKEN_ZONE[token]
    if have_digest is None:
        have_digest = _digest(zone, have_version) if have_version else b""
    call = stub.Stream(metadata=_metadata(token, zone))
    await call.write(
        cpb.GatewayMessage(
            hello=cpb.Hello(
                have_version=have_version,
                have_digest=have_digest,
                gateway_version=gateway_version,
                zone=zone,
                hostname=gateway_id,
                plugins=plugins,
                supported_capabilities=capabilities,
                shared_limits=shared_limits,
            )
        )
    )
    welcome = await asyncio.wait_for(call.read(), 2)
    assert welcome.WhichOneof("update") == "welcome"
    return _Stream(call, welcome.welcome.gateway_id)


async def _raw_stream(
    stub,
    token: str | None,
    zone: str,
    *,
    hello_zone: str | None = None,
    hostname: str = "gateway-a",
    metadata: tuple[tuple[str, str], ...] | None = None,
):
    """A stream whose Hello is not expected to be accepted."""
    call = stub.Stream(metadata=metadata if metadata is not None else _metadata(token, zone))
    await call.write(
        cpb.GatewayMessage(
            hello=cpb.Hello(
                zone=zone if hello_zone is None else hello_zone,
                hostname=hostname,
                gateway_version="test-gateway",
            )
        )
    )
    return call


@pytest.mark.asyncio
async def test_startup_disconnects_stale_mirrors_before_serving(session_factory: SessionFactory):
    scope, _ = await _seed(session_factory, versions=(1, 2))
    other_scope, _ = await _seed(session_factory)
    old_seen = datetime(2025, 1, 1, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        for name, connected in (("stale", True), ("offline", False)):
            session.add(
                GatewaySession(
                    zone=scope,
                    gateway_id=name,
                    connected=connected,
                    acked_version=1,
                    pending_version=1,
                    hello_have_version=1,
                    last_seen_at=old_seen,
                    disconnected_at=None if connected else old_seen,
                )
            )
        session.add(GatewaySession(zone=other_scope, gateway_id="other", connected=True))
    server, channel, _ = await _start(session_factory, scope)
    try:
        async with session_factory() as session, session.begin():
            stale = await session.get(GatewaySession, (scope, "stale"))
            assert stale.connected is False
            assert stale.disconnected_at > old_seen
            assert stale.last_seen_at == old_seen
            assert stale.acked_version == stale.pending_version == stale.hello_have_version == 1
            offline = await session.get(GatewaySession, (scope, "offline"))
            assert offline.disconnected_at == old_seen
            # Every zone's stale mirrors are disconnected: one process serves all zones.
            assert (await session.get(GatewaySession, (other_scope, "other"))).connected is False
            assert await compact_versions(session, scope, retain=1) == 1
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_disconnected_gateway_reconnects_with_compacted_base_gets_full(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory, versions=(1, 2))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
    try:
        assert (await asyncio.wait_for(call.read(), 2)).WhichOneof("update") == "delta"
        # Graceful request EOF closes the stream and persists the disconnected mirror.
        await call.done_writing()
        assert await asyncio.wait_for(call.read(), 2) is grpc.aio.EOF
        async with session_factory() as session, session.begin():
            mirror = await session.get(GatewaySession, (scope, call.gateway_id))
            assert mirror.connected is False
            assert mirror.hello_have_version == mirror.acked_version == 1
            assert await compact_versions(session, scope, retain=1) == 1
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
        update = await asyncio.wait_for(call.read(), 2)
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 2
        assert update.target_digest == _digest(scope, 2)
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_full_then_delta_after_ack_and_session_mirror(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        initial = await call.read()
        assert initial.WhichOneof("update") == "full"
        assert initial.full.version == 1
        assert initial.target_digest == _digest(scope, 1)
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))

        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        delta = await asyncio.wait_for(call.read(), 2)
        assert delta.WhichOneof("update") == "delta"
        assert delta.delta.base_version == 1
        assert delta.delta.version == 2
        assert delta.target_digest == _digest(scope, 2)
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=2)))
        await asyncio.sleep(0.05)

        async with session_factory() as session:
            status = await session.get(GatewaySession, (scope, call.gateway_id))
            assert status is not None
            assert status.connected is True
            assert status.acked_version == 2
            assert status.pending_version is None
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_only_one_update_pending_and_versions_coalesce(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        first = await call.read()
        assert first.full.version == 1
        waiting = asyncio.create_task(call.read())
        for version in (2, 3, 4):
            await _publish(session_factory, scope, version)
            registry_for(server).notify_published(scope, version)
        await asyncio.sleep(0.1)
        assert not waiting.done(), "a second update was sent before the first ACK"

        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        following = await asyncio.wait_for(waiting, 2)
        assert following.WhichOneof("update") == "delta"
        assert following.delta.base_version == 1
        assert following.delta.version == 4
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_two_gateways_get_updates_from_their_own_versions(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(
        session_factory,
        versions=(1, 2),
        gateways=("gateway-a", "gateway-b"),
    )
    server, channel, stub = await _start(session_factory, scope)
    a = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
    b = await _open_stream(stub, tokens["gateway-b"], "gateway-b", 0, scope)
    try:
        update_a, update_b = await asyncio.gather(a.read(), b.read())
        assert update_a.WhichOneof("update") == "delta"
        assert (update_a.delta.base_version, update_a.delta.version) == (1, 2)
        assert update_b.WhichOneof("update") == "full"
        assert update_b.full.version == 2
    finally:
        a.cancel()
        b.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_base_mismatch_resends_full(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        first = await call.read()
        assert first.full.version == 1
        await call.write(
            cpb.GatewayMessage(
                nack=cpb.Nack(
                    version=1,
                    have_version=0,
                    code=cpb.NACK_CODE_BASE_MISMATCH,
                    reason="ignored diagnostic",
                )
            )
        )
        retry = await asyncio.wait_for(call.read(), 2)
        assert retry.WhichOneof("update") == "full"
        assert retry.full.version == 1
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_invalid_version_is_quarantined_until_newer_publish(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", have_version=3, zone=scope)
    try:
        first = await call.read()
        await call.write(
            cpb.GatewayMessage(
                nack=cpb.Nack(version=1, code=cpb.NACK_CODE_INVALID_CONFIG, reason="unsafe text")
            )
        )
        waiting = asyncio.create_task(call.read())
        registry_for(server).notify_published(scope, 1)
        await asyncio.sleep(0.1)
        assert not waiting.done(), "quarantined version was resent"
        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        newer = await asyncio.wait_for(waiting, 2)
        assert newer.WhichOneof("update") == "full"
        assert newer.full.version == 2
        async with session_factory() as session:
            status = await session.get(GatewaySession, (scope, call.gateway_id))
            assert status is not None
            assert status.last_nack_version == first.full.version
            assert status.last_nack_reason == "invalid config"
            quarantined = await session.scalar(
                select(GatewayQuarantine.quarantined_version).where(GatewayQuarantine.zone == scope)
            )
            assert quarantined == first.full.version
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_persist_failed_retries_same_update_with_backoff(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope, ack_timeout=3)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        first = await call.read()
        await call.write(
            cpb.GatewayMessage(
                nack=cpb.Nack(version=1, code=cpb.NACK_CODE_PERSIST_FAILED, reason="disk")
            )
        )
        retry = await asyncio.wait_for(call.read(), 2)
        assert retry.SerializeToString() == first.SerializeToString()
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_ack_timeout_closes_stream(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope, ack_timeout=0.15)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        assert (await call.read()).full.version == 1
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await asyncio.wait_for(call.read(), 1)
        assert error.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_gateway_credentials_and_zone_metadata_are_required(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    token = tokens["gateway-a"]
    usage_stub = usage_pb2_grpc.UsageServiceStub(channel)

    async def refused(call):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await asyncio.wait_for(call.read(), 2)
        assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
        return error.value

    try:
        # Missing metadata, missing zone, unknown zone, wrong token and a duplicated zone
        # header all get the same opaque answer.
        missing = stub.Stream()
        await missing.write(cpb.GatewayMessage(hello=cpb.Hello(zone=scope)))
        await refused(missing)
        no_zone = stub.Stream(metadata=(("authorization", f"Bearer {token}"),))
        await no_zone.write(cpb.GatewayMessage(hello=cpb.Hello(zone=scope)))
        await refused(no_zone)
        await refused(await _raw_stream(stub, token, "no-such-zone"))
        error = await refused(await _raw_stream(stub, "x" * 40, scope))
        assert error.details() == "invalid gateway credentials"
        duplicated = await _raw_stream(
            stub,
            token,
            scope,
            metadata=(
                ("authorization", f"Bearer {token}"),
                ("x-lumewarp-zone", scope),
                ("x-lumewarp-zone", scope),
            ),
        )
        await refused(duplicated)

        # Usage: an empty id is allowed (events served before a handshake) and an invalid
        # non-empty one still rejects the batch; the zone comes from the metadata.
        metadata = _metadata(token, scope)
        accepted = await usage_stub.Report(
            usage_pb2.ReportRequest(gateway_id=""), metadata=metadata
        )
        assert accepted.accepted == 0
        with pytest.raises(grpc.aio.AioRpcError) as usage_invalid:
            await usage_stub.Report(usage_pb2.ReportRequest(gateway_id="bad/id"), metadata=metadata)
        assert usage_invalid.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as usage_unauthenticated:
            await usage_stub.Report(usage_pb2.ReportRequest(gateway_id="gateway-a"))
        assert usage_unauthenticated.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_zone_mismatch_is_refused_and_restored_higher_version_forces_full(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    try:
        # The metadata names `scope`; a Hello of another zone cannot present a snapshot
        # here.
        wrong_zone = await _raw_stream(stub, tokens["gateway-a"], scope, hello_zone="other-scope")
        with pytest.raises(grpc.aio.AioRpcError) as zone_error:
            await wrong_zone.read()
        assert zone_error.value.code() is grpc.StatusCode.FAILED_PRECONDITION

        future_version = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", have_version=2, zone=scope
        )
        restored = await future_version.read()
        assert restored.WhichOneof("update") == "full"
        assert restored.full.version == 1
        future_version.cancel()
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_an_archived_zone_refuses_handshakes(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    async with session_factory() as session, session.begin():
        (await session.get(Zone, scope)).archived_at = datetime.now(UTC)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _raw_stream(stub, tokens["gateway-a"], scope)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await call.read()
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert error.value.details() == "zone is archived"
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_quarantine_survives_gateway_reconnect(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    first_call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        assert (await first_call.read()).full.version == 1
        await first_call.write(
            cpb.GatewayMessage(nack=cpb.Nack(version=1, code=cpb.NACK_CODE_INVALID_CONFIG))
        )
        await asyncio.sleep(0.05)
        first_call.cancel()
        await asyncio.sleep(0.05)

        reconnected = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
        waiting = asyncio.create_task(reconnected.read())
        await asyncio.sleep(0.1)
        assert not waiting.done(), "quarantined version was resent after reconnect"
        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        update = await asyncio.wait_for(waiting, 2)
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 2
        reconnected.cancel()
    finally:
        first_call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_unsupported_feature_nack_skips_the_version_for_that_gateway_only(
    session_factory: SessionFactory,
):
    # A gateway missing a plugin rejects the version; the rest of the scope still gets it.
    scope, tokens = await _seed(session_factory, versions=(1,), gateways=("gateway-a", "gateway-b"))
    server, channel, stub = await _start(session_factory, scope)
    lacking = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    other = None
    try:
        assert (await lacking.read()).full.version == 1
        await lacking.write(
            cpb.GatewayMessage(nack=cpb.Nack(version=1, code=cpb.NACK_CODE_UNSUPPORTED_FEATURE))
        )
        await asyncio.sleep(0.05)
        waiting = asyncio.create_task(lacking.read())
        await asyncio.sleep(0.1)
        assert not waiting.done(), "the NACKing gateway was sent the version again"

        other = await _open_stream(stub, tokens["gateway-b"], "gateway-b", zone=scope)
        update = await asyncio.wait_for(other.read(), 2)
        assert update.full.version == 1
        async with session_factory() as session:
            assert (
                await session.scalar(
                    select(GatewayQuarantine.quarantined_version).where(
                        GatewayQuarantine.zone == scope
                    )
                )
                is None
            )
        waiting.cancel()
    finally:
        lacking.cancel()
        if other is not None:
            other.cancel()
        await channel.close()
        await server.stop(0)


async def _nack_invalid(call, version: int) -> None:
    await call.write(
        cpb.GatewayMessage(nack=cpb.Nack(version=version, code=cpb.NACK_CODE_INVALID_CONFIG))
    )


@pytest.mark.asyncio
async def test_restored_control_plane_with_divergent_digest_sends_full(
    session_factory: SessionFactory, monkeypatch
):
    # The gateway holds a v1/v2 from before a control-plane restore: same numbers, other content.
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(
        session_factory, versions=(1, 2), gateways=("gateway-a", "gateway-b")
    )
    server, channel, stub = await _start(session_factory, scope)
    behind = await _open_stream(
        stub, tokens["gateway-a"], "gateway-a", 1, scope, have_digest=b"\x01" * 32
    )
    current = await _open_stream(
        stub, tokens["gateway-b"], "gateway-b", 2, scope, have_digest=b"\x02" * 32
    )
    try:
        update_a, update_b = await asyncio.gather(
            asyncio.wait_for(behind.read(), 2), asyncio.wait_for(current.read(), 2)
        )
        assert update_a.WhichOneof("update") == "full"
        assert update_a.full.version == 2
        # Even at the same version number, a digest mismatch is divergent content.
        assert update_b.WhichOneof("update") == "full"
        assert update_b.full.version == 2
        assert update_b.target_digest == _digest(scope, 2)
    finally:
        behind.cancel()
        current.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_matching_digest_at_desired_version_sends_nothing(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
    try:
        waiting = asyncio.create_task(call.read())
        await asyncio.sleep(0.2)
        assert not waiting.done(), "up-to-date gateway received an update"
        waiting.cancel()
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_nacked_forced_full_is_never_followed_by_delta(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    # Gateway kept its own v2; the control plane was restored to v1.
    call = await _open_stream(
        stub, tokens["gateway-a"], "gateway-a", 2, scope, have_digest=b"\x03" * 32
    )
    try:
        first = await asyncio.wait_for(call.read(), 2)
        assert first.WhichOneof("update") == "full"
        assert first.full.version == 1
        await _nack_invalid(call, 1)

        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        update = await asyncio.wait_for(call.read(), 2)
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 2
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_idle_sessions_do_not_load_payloads_and_diffs_are_shared(
    session_factory: SessionFactory, monkeypatch
):
    from lumewarp_control_plane import compiler
    from lumewarp_control_plane import grpc as grpc_services

    monkeypatch.setattr(grpc_services, "_PUBLISH_POLL_SECONDS", 0.02)
    diffs = []

    def diff(old, new):
        diffs.append((old.version, new.version))
        return cpb.Delta(base_version=old.version, version=new.version)

    monkeypatch.setattr(compiler, "diff", diff)
    loads = []
    original = grpc_services.SessionRegistry._snapshot

    async def counting_snapshot(self, session, scope_name, version, *args):
        loads.append(version)
        return await original(self, session, scope_name, version, *args)

    monkeypatch.setattr(grpc_services.SessionRegistry, "_snapshot", counting_snapshot)
    parses = []
    original_parse = snapshot_pb2.Snapshot.FromString
    monkeypatch.setattr(
        snapshot_pb2.Snapshot,
        "FromString",
        staticmethod(lambda payload: parses.append(len(payload)) or original_parse(payload)),
    )

    scope, tokens = await _seed(
        session_factory, versions=(1,), gateways=("gateway-a", "gateway-b", "gateway-c")
    )
    server, channel, stub = await _start(session_factory, scope)
    calls = [
        await _open_stream(stub, tokens[g], g, 1, scope)
        for g in ("gateway-a", "gateway-b", "gateway-c")
    ]
    try:
        # Up to date and idle for ~15 poll ticks: nothing is loaded or parsed.
        await asyncio.sleep(0.3)
        assert loads == [] and parses == []

        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        updates = await asyncio.gather(*(asyncio.wait_for(c.read(), 2) for c in calls))
        assert all(u.delta.base_version == 1 and u.delta.version == 2 for u in updates)
        assert diffs == [(1, 2)], "each session recomputed the same diff"
        assert len(parses) == 2  # v1 and v2, once each
    finally:
        for c in calls:
            c.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("min_ping_interval_ms", "survives"),
    # 300 s is gRPC core's default policy: it proves the probe reproduces "too_many_pings".
    [(500, True), (300_000, False)],
)
async def test_keepalive_policy_accepts_idle_stream_pings(
    session_factory: SessionFactory, monkeypatch, min_ping_interval_ms: int, survives: bool
):
    from grpc_health.v1 import health_pb2, health_pb2_grpc

    from lumewarp_control_plane import grpc as grpc_module

    # Scaled down from the gateway's 30 s pings against the 10 s production minimum.
    monkeypatch.setattr(grpc_module, "_KEEPALIVE_MIN_PING_INTERVAL_MS", min_ping_interval_ms)
    server = await serve(
        Settings(
            zone=f"ka-{min_ping_interval_ms}", grpc_bind="127.0.0.1:0", background_enabled=False
        ),
        session_factory,
    )
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{bound_port(server)}",
        options=(
            ("grpc.keepalive_time_ms", 1000),
            ("grpc.keepalive_timeout_ms", 5000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ),
    )
    try:
        # A server stream that stays open with no data, like an idle config stream.
        call = health_pb2_grpc.HealthStub(channel).Watch(health_pb2.HealthCheckRequest(service=""))
        assert (await call.read()).status == health_pb2.HealthCheckResponse.SERVING
        if survives:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(call.read(), timeout=6)
        else:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await asyncio.wait_for(call.read(), timeout=15)
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
        call.cancel()
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_session_and_ack_metrics(session_factory: SessionFactory):
    from lumewarp_control_plane.metrics import Metrics

    metrics = Metrics()
    scope, tokens = await _seed(session_factory)
    server = await serve(_settings(scope), session_factory, metrics)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)

    def value(name, **labels):
        return metrics.value(name, labels)

    try:
        assert value("lumewarp_cp_gateway_sessions", zone=scope) == 0
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
        first = await call.read()
        assert first.full.version == 1
        assert value("lumewarp_cp_gateway_sessions", zone=scope) == 1
        await call.write(
            cpb.GatewayMessage(
                nack=cpb.Nack(version=1, have_version=0, code=cpb.NACK_CODE_BASE_MISMATCH)
            )
        )
        retry = await asyncio.wait_for(call.read(), 2)
        assert retry.full.version == 1
        assert value("lumewarp_cp_gateway_acks_total", result="nack") == 1
        assert value("lumewarp_cp_gateway_acks_total", result="ack") == 0
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with asyncio.timeout(2):
            while value("lumewarp_cp_gateway_acks_total", result="ack") != 1:
                await asyncio.sleep(0.01)
        # An invalid reply (no pending update) is a protocol error, not an ACK.
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(call.read(), 2)
        async with asyncio.timeout(2):
            while value("lumewarp_cp_gateway_sessions", zone=scope) != 0:
                await asyncio.sleep(0.01)
        assert value("lumewarp_cp_gateway_acks_total", result="ack") == 1
        assert value("lumewarp_cp_gateway_acks_total", result="nack") == 1
    finally:
        await channel.close()
        await server.stop(0)


def _descriptor(
    name: str,
    digest: str,
    params: str = '{"type":"object"}',
    *,
    kind: int = cpb.PLUGIN_KIND_ROUTER_STRATEGY,
    member: str = '{"type":"object"}',
) -> cpb.PluginDescriptor:
    router = (
        cpb.RouterStrategyInfo(uses_weight=False, needs=["latency"], member_schema_json=member)
        if kind == cpb.PLUGIN_KIND_ROUTER_STRATEGY
        else cpb.RouterStrategyInfo()
    )
    return cpb.PluginDescriptor(
        kind=kind,
        name=name,
        title=name.title(),
        description="A plugin.",
        params_schema_json=params,
        router=router,
        wasm_sha256="",
        digest=digest,
    )


def _descriptor_with_title(name: str, digest: str, title: str) -> cpb.PluginDescriptor:
    descriptor = _descriptor(name, digest)
    descriptor.title = title
    return descriptor


async def _stored(factory: SessionFactory, scope: str, gateway_id: str) -> dict[str, GatewayPlugin]:
    """The gateway's reported router strategies, by name."""
    async with factory() as session:
        rows = await session.scalars(
            select(GatewayPlugin).where(
                GatewayPlugin.zone == scope,
                GatewayPlugin.gateway_id == gateway_id,
                GatewayPlugin.kind == PluginKind.ROUTER_STRATEGY,
            )
        )
        return {row.name: row for row in rows}


async def _close(call) -> None:
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call.read()


async def test_hello_descriptors_are_stored_per_gateway(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("one", "aa"), _descriptor("two", "bb")),
        )
        assert (await call.read()).WhichOneof("update") == "full"
        assert re.fullmatch(r"gateway-a-[0-9a-f]{8}", call.gateway_id)
        stored = await _stored(session_factory, scope, call.gateway_id)
        assert {name: row.digest for name, row in stored.items()} == {"one": "aa", "two": "bb"}
        assert stored["one"].descriptor["needs"] == ["latency"]
        assert stored["one"].descriptor["params_schema"] == {"type": "object"}
        await _close(call)

        # The same host reconnecting is a new gateway with its own reports.
        changed = '{"type":"object","properties":{"n":{"type":"integer"}}}'
        other = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("one", "cc", changed), _descriptor("three", "dd")),
        )
        assert other.gateway_id != call.gateway_id
        await other.read()
        stored = await _stored(session_factory, scope, other.gateway_id)
        assert {name: row.digest for name, row in stored.items()} == {"one": "cc", "three": "dd"}
        assert "n" in stored["one"].descriptor["params_schema"]["properties"]
        await _close(other)
    finally:
        await channel.close()
        await server.stop(0)


async def test_a_hello_stores_plugins_of_every_kind(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(
                _descriptor("strategy_one", "aa"),
                _descriptor(
                    "auth_one",
                    "bb",
                    '{"type":"object","properties":{"ttl_ms":{"type":"integer"}}}',
                    kind=cpb.PLUGIN_KIND_UPSTREAM_AUTH,
                ),
                _descriptor("middleware_one", "cc", "{}", kind=cpb.PLUGIN_KIND_A2A_MIDDLEWARE),
            ),
        )
        assert (await call.read()).WhichOneof("update") == "full"
        async with session_factory() as session:
            rows = {
                (row.kind, row.name): row
                for row in await session.scalars(
                    select(GatewayPlugin).where(
                        GatewayPlugin.zone == scope,
                        GatewayPlugin.gateway_id == call.gateway_id,
                    )
                )
            }
            registry = await load_plugins(session, scope)
        assert set(rows) == {
            (PluginKind.ROUTER_STRATEGY, "strategy_one"),
            (PluginKind.UPSTREAM_AUTH, "auth_one"),
            (PluginKind.A2A_MIDDLEWARE, "middleware_one"),
        }
        auth = rows[(PluginKind.UPSTREAM_AUTH, "auth_one")].descriptor
        assert auth["params_schema"] == {
            "type": "object",
            "properties": {"ttl_ms": {"type": "integer"}},
        }
        # A member schema belongs to a router strategy only.
        assert auth["member_schema"] is None
        assert rows[(PluginKind.ROUTER_STRATEGY, "strategy_one")].descriptor["member_schema"] == {
            "type": "object"
        }
        assert registry.known(PluginKind.UPSTREAM_AUTH, "auth_one")
        assert registry.problems(PluginKind.UPSTREAM_AUTH, "auth_one", {"ttl_ms": "soon"}) == [
            "params.ttl_ms: must be of type integer"
        ]
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_hello_with_hostile_schemas_keeps_the_stream_working(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    oversized = json.dumps({"type": "object", "description": "x" * (17 * 1024)})
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(
                _descriptor("oversized", "a1", oversized),
                _descriptor("not_json", "a2", "{"),
                _descriptor("remote_ref", "a3", '{"$ref":"http://169.254.169.254/x"}'),
                _descriptor("catastrophic", "a4", CATASTROPHIC),
                _descriptor("nul", "a5", '{"type":"object","title":"a\\u0000b"}'),
                _descriptor("bad_digest", "not hex!"),
                _descriptor("Bad Name", "a7"),
                _descriptor_with_title("nul_title", "a8", "a\x00b"),
            ),
        )
        # The stream is unaffected: the snapshot arrives.
        assert (await call.read()).WhichOneof("update") == "full"
        stored = await _stored(session_factory, scope, call.gateway_id)
        assert "Bad Name" not in stored
        for name in ("oversized", "not_json", "remote_ref", "catastrophic", "nul"):
            assert stored[name].descriptor["params_schema"] is None, name
            assert stored[name].descriptor["schema_problems"], name
        assert stored["nul_title"].descriptor["title"] == "ab"
        assert len(stored["bad_digest"].digest) == 64
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_failure_to_store_descriptors_does_not_break_the_stream(
    session_factory: SessionFactory, monkeypatch
):
    import lumewarp_control_plane.grpc as module

    async def fail(*_args, **_kwargs):
        raise RuntimeError("database is unhappy")

    monkeypatch.setattr(module, "replace_gateway_plugins", fail)
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", plugins=(_descriptor("one", "aa"),)
        )
        assert (await call.read()).WhichOneof("update") == "full"
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def _session_row(factory: SessionFactory, scope: str, gateway_id: str) -> GatewaySession:
    async with factory() as session:
        return await session.get(GatewaySession, (scope, gateway_id))


async def test_capability_flood_is_bounded_counted_and_keeps_the_stream(
    session_factory: SessionFactory,
):
    from lumewarp_control_plane.metrics import Metrics

    metrics = Metrics()
    scope, tokens = await _seed(session_factory)
    server = await serve(_settings(scope), session_factory, metrics)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)
    names = [f"s_{i}" for i in range(MAX_GATEWAY_PLUGINS + 1)]
    illegal = ["", "Bad Name", "UPPER", "x" * 64, "a/b"]
    capabilities = tuple(f"strategy:{n}" for n in (*illegal, *names))
    try:
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", capabilities=capabilities)
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, call.gateway_id)
        assert len(row.capabilities) == MAX_GATEWAY_PLUGINS
        assert f"strategy:{names[-1]}" not in row.capabilities
        async with session_factory() as session:
            registry = await load_plugins(session, scope)
        assert set(names[:MAX_GATEWAY_PLUGINS]) <= set(registry.names(PluginKind.ROUTER_STRATEGY))
        assert not {f"strategy:{name}" for name in illegal} & set(row.capabilities)
        assert not registry.known(PluginKind.ROUTER_STRATEGY, names[-1])
        ignored = metrics.value(
            "lumewarp_cp_gateway_plugins_ignored_total", {"kind": "router-strategy"}
        )
        assert ignored == len(illegal) + 1
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_ignored_plugins_are_counted_per_kind(session_factory: SessionFactory):
    """Each kind has its own limit and its own counter series; a descriptor whose kind is
    unusable belongs to none of them."""
    from lumewarp_control_plane.metrics import Metrics

    metrics = Metrics()
    scope, tokens = await _seed(session_factory)
    server = await serve(_settings(scope), session_factory, metrics)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)
    auth = [f"a_{i}" for i in range(MAX_GATEWAY_PLUGINS + 1)]
    middleware = [f"m_{i}" for i in range(MAX_GATEWAY_PLUGINS + 1)]
    capabilities = (
        *(f"auth:{name}" for name in auth),
        *(f"a2a_middleware:{name}" for name in middleware),
        "strategy:Bad Name",
        "retries",
    )
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("no_kind", "a1", kind=cpb.PLUGIN_KIND_UNSPECIFIED),),
            capabilities=capabilities,
        )
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, call.gateway_id)
        # Each kind keeps its own 128; the other capability survives.
        assert row.capabilities.count("retries") == 1
        assert len([c for c in row.capabilities if c.startswith("auth:")]) == (MAX_GATEWAY_PLUGINS)
        assert len([c for c in row.capabilities if c.startswith("a2a_middleware:")]) == (
            MAX_GATEWAY_PLUGINS
        )
        assert [c for c in row.capabilities if c.startswith("strategy:")] == []
        ignored = lambda kind: metrics.value(  # noqa: E731
            "lumewarp_cp_gateway_plugins_ignored_total", {"kind": kind}
        )
        assert ignored("router-strategy") == 1
        assert ignored("upstream-auth") == 1
        assert ignored("a2a-middleware") == 1
        assert ignored("other") == 1
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_descriptors_and_capabilities_share_one_limit(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    described = tuple(_descriptor(f"d_{i}", "aa") for i in range(100))
    capabilities = tuple(f"strategy:c_{i}" for i in range(100))
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=described,
            capabilities=("retries", *capabilities),
        )
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, call.gateway_id)
        strategies = [c for c in row.capabilities if c.startswith("strategy:")]
        assert len(strategies) == MAX_GATEWAY_PLUGINS
        assert {f"strategy:d_{i}" for i in range(100)} <= set(strategies)
        assert "retries" in row.capabilities
        assert len(await _stored(session_factory, scope, call.gateway_id)) == 100
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_an_unreadable_hello_keeps_the_stream(session_factory: SessionFactory, monkeypatch):
    import lumewarp_control_plane.grpc as module

    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)

    def fail(_hello):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(module, "normalize_hello", fail)
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("one", "aa"),),
            capabilities=("retries",),
        )
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, call.gateway_id)
        assert row.connected is True
        assert row.capabilities == []
        assert await _stored(session_factory, scope, call.gateway_id) == {}
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_session_ids_always_move_forward(session_factory: SessionFactory, monkeypatch):
    import lumewarp_control_plane.grpc as module

    scope, _ = await _seed(session_factory)
    server, channel, _ = await _start(session_factory, scope)
    try:
        registry = registry_for(server)
        fixed = uuid7()
        monkeypatch.setattr(module, "uuid7", lambda: fixed)  # same millisecond, clock stuck
        first = registry._next_session_id(None)
        second = registry._next_session_id(None)
        assert first < second
        ahead = uuid.UUID(int=second.int + 1000)  # a stored id from before a clock step back
        assert registry._next_session_id(ahead) > ahead
    finally:
        await channel.close()
        await server.stop(0)


async def _status(factory: SessionFactory, scope: str, gateway_id: str) -> GatewaySession:
    async with factory() as session:
        return await session.get(GatewaySession, (scope, gateway_id))


@pytest.mark.asyncio
async def test_health_report_is_mirrored_any_time(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        # Before the ACK of the pending full: not a reply, never an ACK/NACK mismatch.
        await call.read()
        report = cpb.HealthReport(
            upstreams=[
                cpb.TargetHealth(name="vllm", healthy=False),
                cpb.TargetHealth(name="openai", healthy=True),
            ],
            models=[cpb.TargetHealth(name="qwen", healthy=False)],
        )
        await call.write(cpb.GatewayMessage(health=report))
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        for _ in range(100):
            status = await _status(session_factory, scope, call.gateway_id)
            if status.acked_version == 1 and status.upstream_health:
                break
            await asyncio.sleep(0.02)
        assert status.upstream_health == {"vllm": False, "openai": True}
        assert status.model_health == {"qwen": False}
        assert status.acked_version == 1
        # A later report replaces the previous one.
        await call.write(cpb.GatewayMessage(health=cpb.HealthReport()))
        for _ in range(100):
            status = await _status(session_factory, scope, call.gateway_id)
            if not status.upstream_health and not status.model_health:
                break
            await asyncio.sleep(0.02)
        assert status.upstream_health == {}
        assert status.model_health == {}
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


async def test_a_catalog_report_reaches_the_ingest_stub(
    session_factory: SessionFactory, monkeypatch
):
    """The report is handed to the MCP module, which owns what is stored; the stream keeps
    working, and a catalog report is never mistaken for an ACK/NACK."""
    import lumewarp_control_plane.grpc as module

    seen = []

    async def capture(session, gateway_id, report, *, zone=None):
        seen.append(
            (
                gateway_id,
                zone,
                [(d.server, d.kind, d.name, bytes(d.hash)) for d in report.definitions],
                [(c.agent, c.card_json, c.error) for c in report.cards],
            )
        )
        return module.catalog_ingest.IngestResult()

    monkeypatch.setattr(module.catalog_ingest, "ingest_report", capture)
    definition = cpb.ObservedDefinition(
        server="s1",
        kind=snapshot_pb2.MCP_DEFINITION_KIND_TOOL,
        name="echo",
        hash=hashlib.sha256(b'{"name":"echo"}').digest(),
        definition_json='{"name":"echo"}',
        auto_approved=False,
        observed_at_unix_ms=1_700_000_000_000,
    )
    card = cpb.AgentCardReport(
        agent="a1", card_json='{"name":"agent"}', error="", fetched_at_unix_ms=1_700_000_000_001
    )
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
        await call.read()
        await call.write(
            cpb.GatewayMessage(catalog=cpb.CatalogReport(definitions=[definition], cards=[card]))
        )
        async with asyncio.timeout(5):
            while not seen:
                await asyncio.sleep(0.01)
        assert seen == [
            (
                call.gateway_id,
                scope,
                [("s1", snapshot_pb2.MCP_DEFINITION_KIND_TOOL, "echo", definition.hash)],
                [("a1", '{"name":"agent"}', "")],
            )
        ]
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with asyncio.timeout(5):
            while (await _session_row(session_factory, scope, call.gateway_id)).acked_version != 1:
                await asyncio.sleep(0.01)
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.parametrize(
    "report",
    [
        cpb.CatalogReport(
            definitions=[
                cpb.ObservedDefinition(
                    server="s1", name="too_big", definition_json="x" * (64 * 1024 + 1)
                )
            ]
        ),
        cpb.CatalogReport(
            definitions=[cpb.ObservedDefinition(server="s1", name="t", hash=b"short")]
        ),
        cpb.CatalogReport(definitions=[cpb.ObservedDefinition(server="", name="t")]),
        cpb.CatalogReport(cards=[cpb.AgentCardReport(agent="a1", card_json="x" * (64 * 1024 + 1))]),
        cpb.CatalogReport(cards=[cpb.AgentCardReport(agent="")]),
        cpb.CatalogReport(cards=[cpb.AgentCardReport(agent="a1", error="e" * 501)]),
        # N2: uint64 on the wire, but a bigint in the database: the item must be dropped, not
        # become a query parameter that fails the whole report.
        cpb.CatalogReport(
            definitions=[cpb.ObservedDefinition(server="s1", name="t", config_version=2**64 - 1)]
        ),
        cpb.CatalogReport(cards=[cpb.AgentCardReport(agent="a1", config_version=2**64 - 1)]),
    ],
    ids=[
        "definition-too-large",
        "hash-not-sha256",
        "server-empty",
        "card-too-large",
        "agent-empty",
        "card-error-too-long",
        "definition-version-beyond-bigint",
        "card-version-beyond-bigint",
    ],
)
@pytest.mark.asyncio
async def test_a_bad_catalog_item_never_closes_the_stream(
    session_factory: SessionFactory, report: cpb.CatalogReport
):
    """A per-item problem is dropped and counted; the gateway keeps its stream (and its
    other, unrelated reports). See review-w1 M4."""
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        await call.read()
        await call.write(cpb.GatewayMessage(catalog=report))
        # The stream is still alive: a health report after it still lands.
        await call.write(
            cpb.GatewayMessage(
                health=cpb.HealthReport(upstreams=[cpb.TargetHealth(name="vllm", healthy=True)])
            )
        )
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        for _ in range(100):
            status = await _status(session_factory, scope, call.gateway_id)
            if status.acked_version == 1 and status.upstream_health:
                break
            await asyncio.sleep(0.02)
        assert status.acked_version == 1
        assert status.upstream_health == {"vllm": True}
    finally:
        await _close(call)
        await channel.close()
        await server.stop(0)


@pytest.mark.parametrize(
    "bad",
    [
        cpb.HealthReport(upstreams=[cpb.TargetHealth(name="x" * 201)]),
        cpb.HealthReport(models=[cpb.TargetHealth(name="x" * 401)]),
    ],
    ids=["upstream", "model"],
)
@pytest.mark.asyncio
async def test_oversized_health_report_closes_the_stream(
    session_factory: SessionFactory, bad: cpb.HealthReport
):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", zone=scope)
    try:
        await call.read()
        await call.write(cpb.GatewayMessage(health=bad))
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await asyncio.wait_for(call.read(), 2)
        assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


# --------------------------------------------------------------------------- gateways


@pytest.mark.asyncio
async def test_two_gateways_sharing_one_token_stay_connected_and_both_get_updates(
    session_factory: SessionFactory, monkeypatch
):
    """One token, two gateway processes: neither evicts the other, and both get updates."""
    from lumewarp_control_plane.metrics import Metrics

    _stub_diff(monkeypatch)
    metrics = Metrics()
    scope, tokens = await _seed(session_factory, gateways=("gateway-a", "gateway-b"))
    server = await serve(_settings(scope), session_factory, metrics)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)
    token = tokens["gateway-a"]
    try:
        first = await _open_stream(stub, token, "gateway-a")
        second = await _open_stream(stub, token, "gateway-b")
        updates = await asyncio.gather(
            asyncio.wait_for(first.read(), 2), asyncio.wait_for(second.read(), 2)
        )
        assert [update.full.version for update in updates] == [1, 1]
        await first.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        await second.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with asyncio.timeout(2):
            while metrics.value("lumewarp_cp_gateway_sessions", {"zone": scope}) != 2:
                await asyncio.sleep(0.01)
        for stream in (first, second):
            async with asyncio.timeout(2):
                while (await _status(session_factory, scope, stream.gateway_id)).acked_version != 1:
                    await asyncio.sleep(0.01)

        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        following = await asyncio.gather(
            asyncio.wait_for(first.read(), 2), asyncio.wait_for(second.read(), 2)
        )
        assert all(update.delta.version == 2 for update in following)
        assert metrics.value("lumewarp_cp_gateway_sessions", {"zone": scope}) == 2
        # A graceful end of the request stream lets each gateway write its disconnected row
        # before the server goes away.
        await first.done_writing()
        await second.done_writing()
        assert await asyncio.wait_for(first.read(), 2) is grpc.aio.EOF
        assert await asyncio.wait_for(second.read(), 2) is grpc.aio.EOF
    finally:
        await channel.close()
        await server.stop(0)
    async with asyncio.timeout(5):
        while True:
            async with session_factory() as session:
                rows = (
                    await session.scalars(
                        select(GatewaySession).where(
                            GatewaySession.zone == scope,
                        )
                    )
                ).all()
            if len(rows) == 2 and all(not row.connected for row in rows):
                break
            await asyncio.sleep(0.02)
    assert {row.gateway_id for row in rows} == {first.gateway_id, second.gateway_id}
    assert all(row.disconnected_at is not None for row in rows)


@pytest.mark.asyncio
async def test_a_nack_from_one_gateway_quarantines_the_version_for_the_scope(
    session_factory: SessionFactory,
):
    """Every gateway gets the same snapshot: one rejection withholds it scope-wide."""
    scope, tokens = await _seed(session_factory, versions=(1,), gateways=("gateway-a", "gateway-b"))
    server, channel, stub = await _start(session_factory, scope)
    nacker = await _open_stream(stub, tokens["gateway-a"], "gateway-a")
    try:
        first = await nacker.read()
        assert first.full.version == 1
        await _nack_invalid(nacker, 1)
        async with asyncio.timeout(5):
            while True:
                async with session_factory() as session:
                    quarantined = await session.scalar(
                        select(GatewayQuarantine.quarantined_version).where(
                            GatewayQuarantine.zone == scope
                        )
                    )
                if quarantined == 1:
                    break
                await asyncio.sleep(0.02)
        async with session_factory() as session:
            status = await session.get(GatewaySession, (scope, nacker.gateway_id))
            assert status.last_nack_version == 1
            assert status.last_nack_reason == "invalid config"

        # A gateway that connects afterwards starts with the quarantine: no v1.
        second = await _open_stream(stub, tokens["gateway-b"], "gateway-b")
        waiting = asyncio.create_task(second.read())
        await asyncio.sleep(0.2)
        assert not waiting.done(), "the quarantined version was sent to another gateway"
        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        newer = await asyncio.wait_for(waiting, 2)
        assert newer.WhichOneof("update") == "full"
        assert newer.full.version == 2
        await _close(nacker)
        await _close(second)

        # And a brand-new gateway, after both are gone, starts with it too.
        third = await _open_stream(stub, tokens["gateway-a"], "gateway-c")
        update = await asyncio.wait_for(third.read(), 2)
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 2
        await _close(third)
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_welcome_assigns_a_sanitized_hostname_id(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        for hostname, prefix in (
            ("", "local"),
            ("bad/id with space", "badidwithspace"),
            ("x" * 70, "x" * 60),
        ):
            call = await _open_stream(stub, tokens["gateway-a"], hostname)
            assert re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{8}", call.gateway_id)
            await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_shared_limits_are_mirrored_per_gateway(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory, versions=(1,), gateways=("gateway-a", "gateway-b"))
    server, channel, stub = await _start(session_factory, scope)
    shared = await _open_stream(
        stub,
        tokens["gateway-a"],
        "gateway-a",
        shared_limits=cpb.SharedLimits(enabled=True, namespace="team-ns"),
    )
    alone = await _open_stream(stub, tokens["gateway-b"], "gateway-b")
    try:
        await asyncio.gather(shared.read(), alone.read())
        async with asyncio.timeout(2):
            while (
                await _session_row(session_factory, scope, shared.gateway_id)
            ).shared_limits_namespace != "team-ns":
                await asyncio.sleep(0.01)
        row = await _session_row(session_factory, scope, shared.gateway_id)
        assert (row.shared_limits_enabled, row.shared_limits_namespace) == (True, "team-ns")
        row = await _session_row(session_factory, scope, alone.gateway_id)
        assert (row.shared_limits_enabled, row.shared_limits_namespace) == (False, "")
    finally:
        await _close(shared)
        await _close(alone)
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_stale_gateway_rows_and_their_plugins_are_deleted_after_a_day(
    session_factory: SessionFactory,
):
    from datetime import timedelta

    from lumewarp_control_plane.compiler.publish import delete_stale_gateway_sessions

    scope, tokens = await _seed(session_factory)
    now = datetime.now(UTC)
    gateways = (
        ("old", False, now - timedelta(hours=25)),
        ("recent", False, now - timedelta(hours=1)),
        ("live", True, None),
    )
    async with session_factory() as session, session.begin():
        for gateway_id, connected, disconnected_at in gateways:
            session.add(
                GatewaySession(
                    zone=scope,
                    gateway_id=gateway_id,
                    connected=connected,
                    disconnected_at=disconnected_at,
                    connected_at=now - timedelta(days=2),
                    last_seen_at=now - timedelta(days=2),
                )
            )
        await session.flush()
        for gateway_id, *_ in gateways:
            session.add(
                GatewayPlugin(
                    zone=scope,
                    gateway_id=gateway_id,
                    kind=PluginKind.ROUTER_STRATEGY,
                    name="sticky",
                    digest="aa",
                    descriptor={},
                    seen_at=now - timedelta(days=2),
                )
            )
    async with session_factory() as session, session.begin():
        assert await delete_stale_gateway_sessions(session, scope, now=now) == 1
    async with session_factory() as session:
        remaining = (
            await session.scalars(
                select(GatewaySession.gateway_id).where(GatewaySession.zone == scope)
            )
        ).all()
        plugins = (
            await session.scalars(
                select(GatewayPlugin.gateway_id).where(GatewayPlugin.zone == scope)
            )
        ).all()
    assert set(remaining) == {"recent", "live"}
    assert set(plugins) == {"recent", "live"}


@pytest.mark.asyncio
async def test_a_quarantine_that_lands_while_a_hello_is_read_still_applies(
    session_factory: SessionFactory, monkeypatch
):
    """The in-process quarantine closes the window between a NACK and a Hello's row read."""
    import lumewarp_control_plane.grpc as module

    real = module.normalize_hello
    entered, release = threading.Event(), threading.Event()

    def slow(hello):
        if hello.gateway_version == "slow-build":
            entered.set()
            assert release.wait(10)
        return real(hello)

    monkeypatch.setattr(module, "normalize_hello", slow)
    scope, tokens = await _seed(session_factory, versions=(1,), gateways=("gateway-a", "gateway-b"))
    server, channel, stub = await _start(session_factory, scope)
    registry = registry_for(server)
    try:
        # The Welcome is not sent until the Hello's plugins are read, so write it raw: the
        # NACK lands while this Hello is being normalized.
        raw = stub.Stream(metadata=_metadata(tokens["gateway-a"], scope))
        await raw.write(
            cpb.GatewayMessage(
                hello=cpb.Hello(zone=scope, hostname="gateway-a", gateway_version="slow-build")
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        # Another gateway's NACK of v1 is now in process, but this Hello already read the
        # quarantine row, before that transaction existed.
        registry.quarantine(scope, 1)
        release.set()
        welcome = await asyncio.wait_for(raw.read(), 2)
        assert welcome.WhichOneof("update") == "welcome"
        call = _Stream(raw, welcome.welcome.gateway_id)
        waiting = asyncio.create_task(call.read())
        await asyncio.sleep(0.2)
        assert not waiting.done(), "the quarantined version reached a gateway that missed it"
        await _publish(session_factory, scope, 2)
        registry.notify_published(scope, 2)
        update = await asyncio.wait_for(waiting, 2)
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 2
        await _close(call)
    finally:
        release.set()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_a_hello_version_is_only_shown_once_its_digest_is_verified(
    session_factory: SessionFactory,
):
    """A version the gateway merely claims is not an applied version (F7)."""
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    fresh = await _open_stream(stub, tokens["gateway-a"], "gateway-a")
    try:
        assert (await fresh.read()).full.version == 1
        row = await _session_row(session_factory, scope, fresh.gateway_id)
        assert row.hello_have_version == 0
        assert row.acked_version is None, "a fresh gateway with no snapshot is not at version 0"
        await fresh.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with asyncio.timeout(2):
            while (await _session_row(session_factory, scope, fresh.gateway_id)).acked_version != 1:
                await asyncio.sleep(0.01)
        await _close(fresh)

        # The same number with a digest this control plane never stored is a claim too.
        claimed = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            1,
            scope,
            have_digest=b"\x01" * 32,
        )
        assert (await claimed.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, claimed.gateway_id)
        assert row.hello_have_version == 1
        assert row.acked_version is None
        await _close(claimed)

        # A verified Hello is the applied version even though no update is sent: an
        # up-to-date gateway must not read as version 0 (or nothing) forever.
        current = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
        waiting = asyncio.create_task(current.read())

        async def verified() -> bool:
            row = await _session_row(session_factory, scope, current.gateway_id)
            return row is not None and row.acked_version == 1

        async with asyncio.timeout(2):
            while not await verified():
                await asyncio.sleep(0.01)
        assert not waiting.done(), "an up-to-date gateway received an update"
        await _close(current)
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_usage_is_attributed_to_the_reporting_gateway(
    session_factory: SessionFactory,
):
    """The token authenticates the scope; ReportRequest.gateway_id names the process."""
    from lumewarp_control_plane.models import UsageEvent as UsageEventRow
    from tests import factories

    scope, tokens = await _seed(session_factory)
    async with session_factory() as session, session.begin():
        team = await factories.make_team(session, zone=scope)
        user = await factories.make_user(session)
        await factories.make_member(session, team, user)
        key = await factories.make_key(session, team, user, zone=scope)
        key_id, team_id = key.id, team.id
    server, channel, _ = await _start(session_factory, scope)
    metadata = _metadata(tokens["gateway-a"], scope)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    event = usage_pb2.UsageEvent(
        request_id=str(uuid7()),
        timestamp_unix_ms=now_ms,
        key_id=str(key_id),
        team_id=str(team_id),
        requested="m",
        model="d",
        endpoint="chat_completions",
        status=200,
        prompt_tokens=1,
        completion_tokens=1,
        cost_nano_usd=1,
        cost_known=True,
        duration_ms=1,
        config_version=1,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )
    usage_stub = usage_pb2_grpc.UsageServiceStub(channel)
    try:
        response = await usage_stub.Report(
            usage_pb2.ReportRequest(gateway_id="reporter-b", events=[event]), metadata=metadata
        )
        assert response.accepted == 1
        async with session_factory() as session:
            stored = await session.scalar(
                select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
            )
        assert stored.gateway_id == "reporter-b"

        # Events served before any Welcome carry no id and are attributed to no gateway.
        unassigned = usage_pb2.UsageEvent()
        unassigned.CopyFrom(event)
        unassigned.request_id = str(uuid7())
        response = await usage_stub.Report(
            usage_pb2.ReportRequest(gateway_id="", events=[unassigned]), metadata=metadata
        )
        assert response.accepted == 1
        async with session_factory() as session:
            stored = await session.scalar(
                select(UsageEventRow).where(
                    UsageEventRow.request_id == uuid.UUID(unassigned.request_id)
                )
            )
        assert stored.gateway_id == ""
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("ipv4:10.0.0.1:5000", "10.0.0.1:5000"),
        ("ipv6:[::1]:5000", "[::1]:5000"),
        ("unix:/run/gw.sock", "unix:/run/gw.sock"),
        ("", None),
        (None, None),
    ],
)
def test_peer_is_stored_without_the_grpc_address_family(raw, shown):
    assert peer_address(raw) == shown


PROBE_PLUGIN = "hmac_signature"
PROBE_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"ttl_ms": {"type": "integer"}},
        "additionalProperties": False,
    }
)


def _probe_plugin() -> cpb.PluginDescriptor:
    return cpb.PluginDescriptor(
        kind=cpb.PLUGIN_KIND_UPSTREAM_AUTH,
        name=PROBE_PLUGIN,
        params_schema_json=PROBE_SCHEMA,
    )


@pytest.mark.asyncio
async def test_a_probe_request_reaches_a_capable_gateway_and_its_result_resolves(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(
        stub,
        tokens["gateway-a"],
        "gateway-a",
        zone=scope,
        plugins=(_probe_plugin(),),
        capabilities=("upstream_probe",),
    )
    try:
        assert (await asyncio.wait_for(call.read(), 2)).WhichOneof("update") == "full"
        registry = registry_for(server)
        assert registry is not None
        pending = asyncio.create_task(
            registry.probe(scope, PROBE_PLUGIN, "vllm", "dep", timeout_seconds=5)
        )
        update = await asyncio.wait_for(call.read(), 2)
        assert update.WhichOneof("update") == "probe"
        assert (update.probe.upstream, update.probe.model) == ("vllm", "dep")
        assert update.probe.request_id
        await call.write(
            cpb.GatewayMessage(
                probe_result=cpb.ProbeResult(
                    request_id=update.probe.request_id, ok=True, status=200, latency_ms=9
                )
            )
        )
        result = await asyncio.wait_for(pending, 2)
        assert (result.ok, result.status, result.latency_ms) == (True, 200, 9)
        assert result.request_id == update.probe.request_id
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_no_capable_gateway_is_probe_unavailable(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    # It reports the plugin but cannot run a probe.
    call = await _open_stream(
        stub, tokens["gateway-a"], "gateway-a", zone=scope, plugins=(_probe_plugin(),)
    )
    try:
        assert (await asyncio.wait_for(call.read(), 2)).WhichOneof("update") == "full"
        registry = registry_for(server)
        assert registry is not None
        with pytest.raises(ProbeUnavailable):
            await registry.probe(scope, PROBE_PLUGIN, "vllm", "", timeout_seconds=1)
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_an_unanswered_probe_times_out(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(
        stub,
        tokens["gateway-a"],
        "gateway-a",
        zone=scope,
        plugins=(_probe_plugin(),),
        capabilities=("upstream_probe",),
    )
    try:
        assert (await asyncio.wait_for(call.read(), 2)).WhichOneof("update") == "full"
        registry = registry_for(server)
        assert registry is not None
        with pytest.raises(ProbeTimeout):
            await registry.probe(scope, PROBE_PLUGIN, "vllm", "", timeout_seconds=0.05)
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_a_disconnect_cleans_up_a_pending_probe(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(
        stub,
        tokens["gateway-a"],
        "gateway-a",
        zone=scope,
        plugins=(_probe_plugin(),),
        capabilities=("upstream_probe",),
    )
    try:
        assert (await asyncio.wait_for(call.read(), 2)).WhichOneof("update") == "full"
        registry = registry_for(server)
        assert registry is not None
        pending = asyncio.create_task(
            registry.probe(scope, PROBE_PLUGIN, "vllm", "", timeout_seconds=10)
        )
        request = await asyncio.wait_for(call.read(), 2)
        assert request.WhichOneof("update") == "probe"
        call.cancel()
        with pytest.raises(ProbeGatewayGone):
            await asyncio.wait_for(pending, 3)
    finally:
        await channel.close()
        await server.stop(0)

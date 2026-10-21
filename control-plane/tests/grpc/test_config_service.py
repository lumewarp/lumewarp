from __future__ import annotations

import asyncio
import hashlib

import grpc
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.grpc import bound_port, registry_for, serve
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ConfigVersion,
    ConfigVersionKind,
    GatewaySession,
    GatewayToken,
    Scope,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from lumewarp_control_plane.settings import Settings


@pytest.fixture(scope="module")
async def c4_engine():
    from tests.conftest import migrated_database

    async with migrated_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()


@pytest.fixture(scope="module")
async def session_factory(c4_engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(c4_engine)


async def _seed(
    factory: SessionFactory,
    *,
    scope_name: str | None = None,
    versions: tuple[int, ...] = (1,),
    gateways: tuple[str, ...] = ("gateway-a",),
) -> tuple[str, dict[str, str]]:
    scope_name = scope_name or f"grpc-{uuid7().hex}"
    tokens: dict[str, str] = {}
    async with factory() as session, session.begin():
        session.add(Scope(name=scope_name))
        await session.flush()
        for gateway_id in gateways:
            generated = generate_secret(SecretKind.GATEWAY_TOKEN)
            tokens[gateway_id] = generated.secret
            session.add(
                GatewayToken(
                    scope=scope_name,
                    gateway_id=gateway_id,
                    name=gateway_id,
                    token_hash=generated.hash,
                    display_prefix=generated.display_prefix,
                )
            )
        for version in versions:
            snapshot = snapshot_pb2.Snapshot(
                scope=scope_name,
                version=version,
                created_at_unix_ms=version,
            )
            payload = snapshot.SerializeToString(deterministic=True)
            session.add(
                ConfigVersion(
                    scope=scope_name,
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
            scope=scope,
            version=version,
            created_at_unix_ms=version,
        )
        payload = snapshot.SerializeToString(deterministic=True)
        session.add(
            ConfigVersion(
                scope=scope,
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


async def _start(factory: SessionFactory, scope: str, *, ack_timeout: float = 1.0):
    server = await serve(
        Settings(
            scope=scope,
            grpc_bind="127.0.0.1:0",
            ack_timeout_seconds=ack_timeout,
            background_enabled=False,
        ),
        factory,
    )
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    return server, channel, cpb_grpc.ConfigServiceStub(channel)


async def _open_stream(stub, token: str, gateway_id: str, have_version: int = 0, scope: str = ""):
    call = stub.Stream(metadata=(("authorization", f"Bearer {token}"),))
    await call.write(
        cpb.GatewayMessage(
            hello=cpb.Hello(
                gateway_id=gateway_id,
                have_version=have_version,
                gateway_version="test-gateway",
                scope=scope,
            )
        )
    )
    return call


@pytest.mark.asyncio
async def test_full_then_delta_after_ack_and_session_mirror(
    session_factory: SessionFactory, monkeypatch
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
    try:
        initial = await call.read()
        assert initial.WhichOneof("update") == "full"
        assert initial.full.version == 1
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))

        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        delta = await asyncio.wait_for(call.read(), 2)
        assert delta.WhichOneof("update") == "delta"
        assert delta.delta.base_version == 1
        assert delta.delta.version == 2
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=2)))
        await asyncio.sleep(0.05)

        async with session_factory() as session:
            status = await session.get(GatewaySession, (scope, "gateway-a"))
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", have_version=3, scope=scope)
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
            status = await session.get(GatewaySession, (scope, "gateway-a"))
            assert status is not None
            assert status.quarantined_version == first.full.version
            assert status.last_nack_reason == "invalid config"
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_persist_failed_retries_same_update_with_backoff(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope, ack_timeout=3)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
async def test_token_and_gateway_id_spoofing_are_rejected(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(stub, tokens["gateway-a"], "spoofed-gateway", scope=scope)
        with pytest.raises(grpc.aio.AioRpcError) as hello_error:
            await call.read()
        assert hello_error.value.code() is grpc.StatusCode.PERMISSION_DENIED

        unauthenticated = stub.Stream()
        await unauthenticated.write(
            cpb.GatewayMessage(hello=cpb.Hello(gateway_id="gateway-a", scope=scope))
        )
        with pytest.raises(grpc.aio.AioRpcError) as token_error:
            await unauthenticated.read()
        assert token_error.value.code() is grpc.StatusCode.PERMISSION_DENIED

        usage_stub = usage_pb2_grpc.UsageServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as usage_spoof:
            await usage_stub.Report(
                usage_pb2.ReportRequest(gateway_id="spoofed-gateway"),
                metadata=(("authorization", f"Bearer {tokens['gateway-a']}"),),
            )
        assert usage_spoof.value.code() is grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.aio.AioRpcError) as usage_unauthenticated:
            await usage_stub.Report(usage_pb2.ReportRequest(gateway_id="gateway-a"))
        assert usage_unauthenticated.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_scope_mismatch_and_restored_higher_version_force_full(
    session_factory: SessionFactory,
):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    try:
        wrong_scope = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", have_version=1, scope="other-scope"
        )
        update = await wrong_scope.read()
        assert update.WhichOneof("update") == "full"
        assert update.full.version == 1
        wrong_scope.cancel()

        future_version = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", have_version=2, scope=scope
        )
        restored = await future_version.read()
        assert restored.WhichOneof("update") == "full"
        assert restored.full.version == 1
        future_version.cancel()
    finally:
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_quarantine_survives_gateway_reconnect(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    first_call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
    try:
        assert (await first_call.read()).full.version == 1
        await first_call.write(
            cpb.GatewayMessage(nack=cpb.Nack(version=1, code=cpb.NACK_CODE_INVALID_CONFIG))
        )
        await asyncio.sleep(0.05)
        first_call.cancel()
        await asyncio.sleep(0.05)

        reconnected = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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

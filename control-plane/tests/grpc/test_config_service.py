from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime

import grpc
import pytest
from sqlalchemy import event, select, update
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
from lumewarp_control_plane.compiler.plugins import (
    MAX_GATEWAY_PLUGINS,
    load_plugins,
)
from lumewarp_control_plane.compiler.publish import compact_versions
from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.grpc import bound_port, registry_for, serve
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ConfigVersion,
    ConfigVersionKind,
    GatewayPlugin,
    GatewaySession,
    GatewayToken,
    PluginKind,
    Scope,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from lumewarp_control_plane.settings import Settings

CATASTROPHIC = json.dumps(
    {"type": "object", "properties": {"a": {"type": "string", "pattern": "^(a+)+$"}}}
)


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


def _digest(scope: str, version: int) -> bytes:
    """The digest _seed/_publish store for a version."""
    payload = snapshot_pb2.Snapshot(
        scope=scope, version=version, created_at_unix_ms=version
    ).SerializeToString(deterministic=True)
    return hashlib.sha256(payload).digest()


async def _open_stream(
    stub,
    token: str,
    gateway_id: str,
    have_version: int = 0,
    scope: str = "",
    have_digest: bytes | None = None,
    plugins: tuple[cpb.PluginDescriptor, ...] = (),
    capabilities: tuple[str, ...] = (),
    gateway_version: str = "test-gateway",
):
    """Hello; by default have_digest is the stored digest of have_version (if any)."""
    if have_digest is None:
        have_digest = _digest(scope, have_version) if have_version and scope else b""
    call = stub.Stream(metadata=(("authorization", f"Bearer {token}"),))
    await call.write(
        cpb.GatewayMessage(
            hello=cpb.Hello(
                gateway_id=gateway_id,
                have_version=have_version,
                have_digest=have_digest,
                gateway_version=gateway_version,
                scope=scope,
                plugins=plugins,
                supported_capabilities=capabilities,
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
                    scope=scope,
                    gateway_id=name,
                    connected=connected,
                    acked_version=1,
                    pending_version=1,
                    hello_have_version=1,
                    last_seen_at=old_seen,
                    disconnected_at=None if connected else old_seen,
                    quarantined_version=1,
                )
            )
        session.add(GatewaySession(scope=other_scope, gateway_id="other", connected=True))
    server, channel, _ = await _start(session_factory, scope)
    try:
        async with session_factory() as session, session.begin():
            stale = await session.get(GatewaySession, (scope, "stale"))
            assert stale.connected is False
            assert stale.disconnected_at > old_seen
            assert stale.last_seen_at == old_seen
            assert stale.acked_version == stale.pending_version == stale.hello_have_version == 1
            assert stale.quarantined_version == 1
            offline = await session.get(GatewaySession, (scope, "offline"))
            assert offline.disconnected_at == old_seen
            assert (await session.get(GatewaySession, (other_scope, "other"))).connected is True
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
            mirror = await session.get(GatewaySession, (scope, "gateway-a"))
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
@pytest.mark.parametrize("hello", ["ahead_of_restore", "other_scope"])
async def test_nacked_forced_full_is_never_followed_by_delta(
    session_factory: SessionFactory, monkeypatch, hello: str
):
    _stub_diff(monkeypatch)
    scope, tokens = await _seed(session_factory, versions=(1,))
    server, channel, stub = await _start(session_factory, scope)
    if hello == "ahead_of_restore":
        # Gateway kept its own v2; the control plane was restored to v1.
        call = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", 2, scope, have_digest=b"\x03" * 32
        )
    else:
        call = await _open_stream(
            stub, tokens["gateway-a"], "gateway-a", 1, "other-scope", have_digest=_digest(scope, 1)
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
async def test_revoked_token_stream_is_closed_by_registry_hook(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
    try:
        assert (await call.read()).full.version == 1
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with session_factory() as session:
            token_id = (
                await session.execute(
                    select(GatewayToken.id).where(
                        GatewayToken.scope == scope, GatewayToken.gateway_id == "gateway-a"
                    )
                )
            ).scalar_one()
        assert registry_for(server).close_token(token_id) == 1
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await asyncio.wait_for(call.read(), 1)
        assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        call.cancel()
        await channel.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_revoked_token_stream_is_closed_by_periodic_recheck(
    session_factory: SessionFactory, monkeypatch
):
    from lumewarp_control_plane import grpc as grpc_services

    monkeypatch.setattr(grpc_services, "_TOKEN_RECHECK_SECONDS", 0.1)
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
    try:
        assert (await call.read()).full.version == 1
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with session_factory() as session, session.begin():
            await session.execute(
                update(GatewayToken)
                .where(GatewayToken.scope == scope, GatewayToken.gateway_id == "gateway-a")
                .values(revoked_at=datetime.now(UTC))
            )
        # A version published after revocation must not reach the stream.
        await _publish(session_factory, scope, 2)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            while True:
                update_msg = await asyncio.wait_for(call.read(), 1)
                assert update_msg.WhichOneof("update") is None, "revoked stream got an update"
        assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
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
async def test_replaced_stream_is_closed_without_further_updates(session_factory: SessionFactory):
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    old = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
    try:
        assert (await old.read()).full.version == 1
        await old.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        await asyncio.sleep(0.05)
        new = await _open_stream(stub, tokens["gateway-a"], "gateway-a", 1, scope)
        registry = registry_for(server)
        for _ in range(100):
            current = registry._sessions.get((scope, "gateway-a"))
            if current is not None and current.hello_version == 1:
                break
            await asyncio.sleep(0.01)
        await _publish(session_factory, scope, 2)
        registry_for(server).notify_published(scope, 2)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            while True:
                message = await asyncio.wait_for(old.read(), 2)
                assert message.WhichOneof("update") is None, "replaced stream got an update"
        assert error.value.code() is grpc.StatusCode.UNAVAILABLE
        assert (await asyncio.wait_for(new.read(), 2)).delta.version == 2
        new.cancel()
    finally:
        old.cancel()
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
            scope=f"ka-{min_ping_interval_ms}", grpc_bind="127.0.0.1:0", background_enabled=False
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
    server = await serve(
        Settings(scope=scope, grpc_bind="127.0.0.1:0", background_enabled=False),
        session_factory,
        metrics,
    )
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)

    def value(name, **labels):
        return metrics.value(name, labels)

    try:
        assert value("lumewarp_cp_gateway_sessions") == 0
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
        first = await call.read()
        assert first.full.version == 1
        assert value("lumewarp_cp_gateway_sessions") == 1
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
            while value("lumewarp_cp_gateway_sessions") != 0:
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
                GatewayPlugin.scope == scope,
                GatewayPlugin.gateway_id == gateway_id,
                GatewayPlugin.kind == PluginKind.ROUTER_STRATEGY,
            )
        )
        return {row.name: row for row in rows}


async def _close(call) -> None:
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call.read()


async def test_hello_descriptors_are_stored_and_replaced_on_reconnect(
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
        stored = await _stored(session_factory, scope, "gateway-a")
        assert {name: row.digest for name, row in stored.items()} == {"one": "aa", "two": "bb"}
        assert stored["one"].descriptor["needs"] == ["latency"]
        assert stored["one"].descriptor["params_schema"] == {"type": "object"}
        await _close(call)

        # A new build: one schema changed (new digest), one strategy gone, one added.
        changed = '{"type":"object","properties":{"n":{"type":"integer"}}}'
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("one", "cc", changed), _descriptor("three", "dd")),
        )
        await call.read()
        stored = await _stored(session_factory, scope, "gateway-a")
        assert {name: row.digest for name, row in stored.items()} == {"one": "cc", "three": "dd"}
        assert "n" in stored["one"].descriptor["params_schema"]["properties"]
        await _close(call)
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
                        GatewayPlugin.scope == scope,
                        GatewayPlugin.gateway_id == "gateway-a",
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
        stored = await _stored(session_factory, scope, "gateway-a")
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


STRICT = '{"type":"object","properties":{"n":{"type":"integer","maximum":1}}}'
LOOSE = '{"type":"object","properties":{"n":{"type":"integer","maximum":100}}}'


async def _session_row(factory: SessionFactory, scope: str, gateway_id: str) -> GatewaySession:
    async with factory() as session:
        return await session.get(GatewaySession, (scope, gateway_id))


async def _token_id(factory: SessionFactory, scope: str, gateway_id: str) -> uuid.UUID:
    async with factory() as session:
        return await session.scalar(
            select(GatewayToken.id).where(
                GatewayToken.scope == scope, GatewayToken.gateway_id == gateway_id
            )
        )


async def test_a_stale_hello_read_slowly_cannot_overwrite_a_newer_connection(
    session_factory: SessionFactory, monkeypatch
):
    """A's Hello is still being read when B connects with a stricter schema and finishes."""
    import lumewarp_control_plane.grpc as module

    real = module.normalize_hello
    entered, release = threading.Event(), threading.Event()

    def slow(hello):
        if hello.gateway_version == "old-build":
            entered.set()
            assert release.wait(10)
        return real(hello)

    monkeypatch.setattr(module, "normalize_hello", slow)
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    token = tokens["gateway-a"]
    try:
        old = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("loose", "aa", LOOSE), _descriptor("shared", "a1", LOOSE)),
            capabilities=("strategy:loose", "strategy:shared"),
            gateway_version="old-build",
        )
        assert await asyncio.to_thread(entered.wait, 10)
        new = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("shared", "bb", STRICT),),
            capabilities=("strategy:shared",),
            gateway_version="new-build",
        )
        assert (await asyncio.wait_for(new.read(), 5)).WhichOneof("update") == "full"
        release.set()
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await asyncio.wait_for(old.read(), 5)
        assert error.value.code() is grpc.StatusCode.UNAVAILABLE

        stored = await _stored(session_factory, scope, "gateway-a")
        assert {name: row.digest for name, row in stored.items()} == {"shared": "bb"}
        row = await _session_row(session_factory, scope, "gateway-a")
        assert row.connected is True
        assert row.gateway_version == "new-build"
        assert row.capabilities == ["strategy:shared"]
        async with session_factory() as session:
            registry = await load_plugins(session, scope)
        assert not registry.known(PluginKind.ROUTER_STRATEGY, "loose")
        assert registry.router_problems("shared", {"n": 50}, [])
        # And the live stream still is the new one.
        current = registry_for(server)._sessions[(scope, "gateway-a")]
        assert current.gateway_version == "new-build"
        await _close(new)
    finally:
        release.set()
        await channel.close()
        await server.stop(0)


async def test_hello_transaction_holds_the_row_lock_until_its_descriptors_are_written(
    session_factory: SessionFactory, monkeypatch
):
    """A sits inside its transaction with the row locked; B's Hello must queue behind it."""
    import lumewarp_control_plane.grpc as module

    real = module.replace_gateway_plugins
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_first(session, scope, gateway_id, token_id, reported, now):
        await real(session, scope, gateway_id, token_id, reported, now)
        if any(item.name == "loose" for item in reported):
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    monkeypatch.setattr(module, "replace_gateway_plugins", pause_first)
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    token = tokens["gateway-a"]
    try:
        old = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("loose", "aa", LOOSE),),
            gateway_version="old-build",
        )
        await asyncio.wait_for(entered.wait(), 10)
        new = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("strict", "bb", STRICT),),
            gateway_version="new-build",
        )
        first = asyncio.ensure_future(new.read())
        done, _ = await asyncio.wait({first}, timeout=0.5)
        assert not done, "B got past A's row lock"
        assert await _stored(session_factory, scope, "gateway-a") == {}
        release.set()
        assert (await asyncio.wait_for(first, 5)).WhichOneof("update") == "full"
        stored = await _stored(session_factory, scope, "gateway-a")
        assert {name: row.digest for name, row in stored.items()} == {"strict": "bb"}
        row = await _session_row(session_factory, scope, "gateway-a")
        assert row.gateway_version == "new-build"
        assert row.token_id == await _token_id(session_factory, scope, "gateway-a")
        assert all(
            r.token_id == row.token_id
            for r in (await _stored(session_factory, scope, "gateway-a")).values()
        )
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(old.read(), 5)
        await _close(new)
    finally:
        release.set()
        await channel.close()
        await server.stop(0)


async def test_router_validation_sees_one_hello_never_a_mix(
    session_factory: SessionFactory, c4_engine: AsyncEngine, monkeypatch
):
    """A new build drops a strategy and tightens another. Until its Hello commits a reader sees
    the old build whole, from then on the new build whole, in one statement.
    """
    import lumewarp_control_plane.grpc as module

    real = module.replace_gateway_plugins
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_new_build(session, scope, gateway_id, token_id, reported, now):
        await real(session, scope, gateway_id, token_id, reported, now)
        if any(item.digest == "b0" for item in reported):
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    monkeypatch.setattr(module, "replace_gateway_plugins", pause_new_build)
    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    token = tokens["gateway-a"]

    async def read():
        statements = []

        def count(_conn, _cursor, statement, *_rest):
            statements.append(statement)

        event.listen(c4_engine.sync_engine, "before_cursor_execute", count)
        try:
            async with session_factory() as session:
                registry = await load_plugins(session, scope)
        finally:
            event.remove(c4_engine.sync_engine, "before_cursor_execute", count)
        assert len(statements) == 1, statements
        return registry

    try:
        first = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("dropped", "a1"), _descriptor("kept", "a0", LOOSE)),
            capabilities=("strategy:dropped", "strategy:kept"),
        )
        await first.read()
        await _close(first)

        second = await _open_stream(
            stub,
            token,
            "gateway-a",
            plugins=(_descriptor("kept", "b0", STRICT),),
            capabilities=("strategy:kept",),
        )
        await asyncio.wait_for(entered.wait(), 10)
        during = await read()
        assert during.known(PluginKind.ROUTER_STRATEGY, "dropped")
        assert during.support(PluginKind.ROUTER_STRATEGY, "kept")[0].digest == "a0"
        assert during.router_problems("kept", {"n": 50}, []) == []
        release.set()
        assert (await asyncio.wait_for(second.read(), 5)).WhichOneof("update") == "full"
        after = await read()
        assert not after.known(PluginKind.ROUTER_STRATEGY, "dropped")
        assert after.support(PluginKind.ROUTER_STRATEGY, "kept")[0].digest == "b0"
        assert after.router_problems("kept", {"n": 50}, [])
        await _close(second)
    finally:
        release.set()
        await channel.close()
        await server.stop(0)


async def test_capability_flood_is_bounded_counted_and_keeps_the_stream(
    session_factory: SessionFactory,
):
    from lumewarp_control_plane.metrics import Metrics

    metrics = Metrics()
    scope, tokens = await _seed(session_factory)
    server = await serve(
        Settings(scope=scope, grpc_bind="127.0.0.1:0", background_enabled=False),
        session_factory,
        metrics,
    )
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    stub = cpb_grpc.ConfigServiceStub(channel)
    names = [f"s_{i}" for i in range(MAX_GATEWAY_PLUGINS + 1)]
    illegal = ["", "Bad Name", "UPPER", "x" * 64, "a/b"]
    capabilities = tuple(f"strategy:{n}" for n in (*illegal, *names))
    try:
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", capabilities=capabilities)
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, "gateway-a")
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
    server = await serve(
        Settings(scope=scope, grpc_bind="127.0.0.1:0", background_enabled=False),
        session_factory,
        metrics,
    )
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
        row = await _session_row(session_factory, scope, "gateway-a")
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
        row = await _session_row(session_factory, scope, "gateway-a")
        strategies = [c for c in row.capabilities if c.startswith("strategy:")]
        assert len(strategies) == MAX_GATEWAY_PLUGINS
        assert {f"strategy:d_{i}" for i in range(100)} <= set(strategies)
        assert "retries" in row.capabilities
        assert len(await _stored(session_factory, scope, "gateway-a")) == 100
        await _close(call)
    finally:
        await channel.close()
        await server.stop(0)


async def test_an_unreadable_hello_keeps_what_is_known_and_the_stream(
    session_factory: SessionFactory, monkeypatch
):
    import lumewarp_control_plane.grpc as module

    scope, tokens = await _seed(session_factory)
    server, channel, stub = await _start(session_factory, scope)
    try:
        call = await _open_stream(
            stub,
            tokens["gateway-a"],
            "gateway-a",
            plugins=(_descriptor("one", "aa"),),
            capabilities=("retries",),
        )
        await call.read()
        await _close(call)

        def fail(_hello):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(module, "normalize_hello", fail)
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a")
        assert (await call.read()).WhichOneof("update") == "full"
        row = await _session_row(session_factory, scope, "gateway-a")
        assert row.connected is True
        assert set(row.capabilities) == {"retries", "strategy:one"}
        assert set(await _stored(session_factory, scope, "gateway-a")) == {"one"}
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
            status = await _status(session_factory, scope, "gateway-a")
            if status.acked_version == 1 and status.upstream_health:
                break
            await asyncio.sleep(0.02)
        assert status.upstream_health == {"vllm": False, "openai": True}
        assert status.model_health == {"qwen": False}
        assert status.acked_version == 1
        # A later report replaces the previous one.
        await call.write(cpb.GatewayMessage(health=cpb.HealthReport()))
        for _ in range(100):
            status = await _status(session_factory, scope, "gateway-a")
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

    async def capture(session, gateway_id, report):
        seen.append(
            (
                gateway_id,
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
        call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
        await call.read()
        await call.write(
            cpb.GatewayMessage(catalog=cpb.CatalogReport(definitions=[definition], cards=[card]))
        )
        async with asyncio.timeout(5):
            while not seen:
                await asyncio.sleep(0.01)
        assert seen == [
            (
                "gateway-a",
                [("s1", snapshot_pb2.MCP_DEFINITION_KIND_TOOL, "echo", definition.hash)],
                [("a1", '{"name":"agent"}', "")],
            )
        ]
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=1)))
        async with asyncio.timeout(5):
            while (await _session_row(session_factory, scope, "gateway-a")).acked_version != 1:
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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
            status = await _status(session_factory, scope, "gateway-a")
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
    call = await _open_stream(stub, tokens["gateway-a"], "gateway-a", scope=scope)
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

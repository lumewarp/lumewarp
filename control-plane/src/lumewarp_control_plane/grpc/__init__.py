"""Authenticated gateway gRPC services and per-stream config synchronization.

Config/usage wire semantics are normative in ``docs/config-protocol.md``. The registry is
process-local for coordination; ``gateway_sessions`` is a status mirror, never session truth.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    config_service_pb2,
    config_service_pb2_grpc,
    snapshot_pb2,
)
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2, usage_pb2_grpc
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import ConfigVersion, GatewaySession, GatewayToken
from lumewarp_control_plane.security import SecretKind, hash_secret, parse_bearer, secret_kind
from lumewarp_control_plane.settings import Settings
from lumewarp_control_plane.usage import InvalidUsageBatch, ingest

log = logging.getLogger(__name__)
# Fallback only: in-process publishes wake streams via SessionRegistry.notify_published.
# Each poll reads version metadata, never payloads.
_PUBLISH_POLL_SECONDS = 2.0
# Bound on how long a revoked token's open stream survives if close_token was not called.
_TOKEN_RECHECK_SECONDS = 5.0
_SNAPSHOT_CACHE_ENTRIES = 4
_UPDATE_CACHE_ENTRIES = 8
_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 60.0
_GRPC_HARD_MESSAGE_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class GatewayIdentity:
    token_id: uuid.UUID
    gateway_id: str
    scope: str


async def authenticate_gateway(
    session: AsyncSession, token: str, scope: str
) -> GatewayIdentity | None:
    """Resolve a valid gateway token in the requested config scope, otherwise return None."""
    if secret_kind(token) is not SecretKind.GATEWAY_TOKEN:
        return None
    row = (
        await session.execute(
            select(GatewayToken).where(
                GatewayToken.token_hash == hash_secret(token),
                GatewayToken.scope == scope,
                GatewayToken.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return GatewayIdentity(token_id=row.id, gateway_id=row.gateway_id, scope=row.scope)


_identity_var: contextvars.ContextVar[GatewayIdentity | None] = contextvars.ContextVar(
    "lumewarp_gateway_identity", default=None
)


def _identity() -> GatewayIdentity | None:
    return _identity_var.get()


class _GatewayAuthInterceptor(grpc.aio.ServerInterceptor):
    """Authenticate only the gateway protocol services; standard health RPCs stay open."""

    def __init__(self, settings: Settings, session_factory: SessionFactory) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def _authenticate(self, context) -> GatewayIdentity:
        metadata = context.invocation_metadata() or ()
        values = [item.value for item in metadata if item.key.lower() == "authorization"]
        token = parse_bearer(values[0]) if len(values) == 1 else None
        identity = None
        if token is not None:
            async with self.session_factory() as session:
                identity = await authenticate_gateway(session, token, self.settings.scope)
        if identity is None:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "invalid gateway credentials")
        return identity

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None or not handler_call_details.method.startswith(
            ("/lumewarp.config.v1.ConfigService/", "/lumewarp.usage.v1.UsageService/")
        ):
            return handler

        if handler.request_streaming and handler.response_streaming:

            async def stream_stream(request_iterator, context):
                identity = await self._authenticate(context)
                marker = _identity_var.set(identity)
                try:
                    async for response in handler.stream_stream(request_iterator, context):
                        yield response
                finally:
                    _identity_var.reset(marker)

            return grpc.stream_stream_rpc_method_handler(
                stream_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.request_streaming:

            async def stream_unary(request_iterator, context):
                identity = await self._authenticate(context)
                marker = _identity_var.set(identity)
                try:
                    return await handler.stream_unary(request_iterator, context)
                finally:
                    _identity_var.reset(marker)

            return grpc.stream_unary_rpc_method_handler(
                stream_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.response_streaming:

            async def unary_stream(request, context):
                identity = await self._authenticate(context)
                marker = _identity_var.set(identity)
                try:
                    async for response in handler.unary_stream(request, context):
                        yield response
                finally:
                    _identity_var.reset(marker)

            return grpc.unary_stream_rpc_method_handler(
                unary_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        async def unary_unary(request, context):
            identity = await self._authenticate(context)
            marker = _identity_var.set(identity)
            try:
                return await handler.unary_unary(request, context)
            finally:
                _identity_var.reset(marker)

        return grpc.unary_unary_rpc_method_handler(
            unary_unary,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


@dataclass
class _PendingUpdate:
    message: config_service_pb2.ConfigUpdate
    version: int
    digest: bytes
    sent_at: float
    retry_at: float | None = None
    retry_delay: float = _RETRY_INITIAL_SECONDS


@dataclass
class _StreamState:
    identity: GatewayIdentity
    session_id: uuid.UUID
    hello_version: int
    hello_digest: bytes
    hello_scope_matches: bool
    gateway_version: str
    capabilities: list[str]
    peer: str | None
    connected_at: datetime
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # Last version the gateway reported or ACKed; status only, never a delta base by itself.
    acked_version: int = 0
    # (version, digest) the gateway is known to hold with this control plane's content: the
    # Hello's version once its digest is verified, then each ACKed version. None means the
    # next update must be full (no Hello match, BASE_MISMATCH, or a full not yet ACKed).
    base: tuple[int, bytes] | None = None
    hello_checked: bool = False
    pending: _PendingUpdate | None = None
    quarantined_versions: set[int] = field(default_factory=set)
    last_nack_version: int | None = None
    last_nack_code: str | None = None
    last_nack_reason: str | None = None
    # Set when the stream must end (replaced by a newer connection, token revoked).
    closed: tuple[grpc.StatusCode, str] | None = None

    @property
    def scope(self) -> str:
        return self.identity.scope

    @property
    def gateway_id(self) -> str:
        return self.identity.gateway_id

    def close(self, code: grpc.StatusCode, details: str) -> None:
        if self.closed is None:
            self.closed = (code, details)
        self.wake.set()


_UpdateKey = tuple[str, int, bytes, tuple[int, bytes] | None]


class SessionRegistry:
    """Track one active ACK/pending state machine per ``(scope, gateway_id)``.

    It also caches parsed snapshots and built updates keyed by ``(scope, version, digest)``
    (plus the delta base), so N gateways moving between the same versions cost one payload
    load and one diff.
    """

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self.session_factory = session_factory
        self._sessions: dict[tuple[str, str], _StreamState] = {}
        self._lock = asyncio.Lock()
        self._build_lock = asyncio.Lock()
        self._snapshots: OrderedDict[tuple[str, int, bytes], snapshot_pb2.Snapshot] = OrderedDict()
        self._updates: OrderedDict[_UpdateKey, config_service_pb2.ConfigUpdate] = OrderedDict()

    def notify_published(self, scope: str, version: int) -> None:
        """Wake all live streams in the changed scope; polling covers missed notifications."""
        for state in tuple(self._sessions.values()):
            if state.scope == scope:
                state.wake.set()

    def close_token(self, token_id: uuid.UUID) -> int:
        """End every live stream authenticated with ``token_id`` (PERMISSION_DENIED).

        Call it after the revocation commits; streams also re-check their token every
        ``_TOKEN_RECHECK_SECONDS``, so this only makes revocation immediate. Returns the
        number of streams closed.
        """
        closed = 0
        for state in tuple(self._sessions.values()):
            if state.identity.token_id == token_id:
                state.close(grpc.StatusCode.PERMISSION_DENIED, "gateway token revoked")
                closed += 1
        return closed

    async def _snapshot(
        self,
        session: AsyncSession,
        scope: str,
        version: int,
        digest: bytes,
        max_bytes: int,
    ) -> snapshot_pb2.Snapshot | None:
        """Parsed payload of a version, or None once it has been compacted."""
        key = (scope, version, digest)
        cached = self._snapshots.get(key)
        if cached is not None:
            self._snapshots.move_to_end(key)
            return cached
        row = (
            await session.execute(
                select(ConfigVersion.payload, ConfigVersion.digest).where(
                    ConfigVersion.scope == scope, ConfigVersion.version == version
                )
            )
        ).one_or_none()
        if row is None or row.payload is None:
            return None
        if row.digest != digest:
            raise RuntimeError("config version digest changed while building an update")
        if len(row.payload) > max_bytes:
            raise RuntimeError("published config snapshot exceeds configured byte limit")
        snapshot = snapshot_pb2.Snapshot.FromString(row.payload)
        if snapshot.version != version or snapshot.scope != scope:
            raise RuntimeError("stored config version payload metadata is inconsistent")
        self._snapshots[key] = snapshot
        while len(self._snapshots) > _SNAPSHOT_CACHE_ENTRIES:
            self._snapshots.popitem(last=False)
        return snapshot

    async def build_update(
        self,
        session: AsyncSession,
        settings: Settings,
        scope: str,
        version: int,
        digest: bytes,
        base: tuple[int, bytes] | None,
    ) -> config_service_pb2.ConfigUpdate:
        """Full (``base is None``) or delta update to ``version``, carrying its digest."""
        key: _UpdateKey = (scope, version, digest, base)
        async with self._build_lock:
            cached = self._updates.get(key)
            if cached is not None:
                self._updates.move_to_end(key)
                return cached
            max_bytes = min(settings.max_snapshot_bytes, _GRPC_HARD_MESSAGE_LIMIT)
            target = await self._snapshot(session, scope, version, digest, max_bytes)
            if target is None:
                raise RuntimeError("latest config version payload has been compacted")
            base_snapshot = None
            if base is not None:
                base_snapshot = await self._snapshot(session, scope, *base, max_bytes)
            if base_snapshot is None:
                # A base compacted after the retention check also lands here.
                key = (scope, version, digest, None)
                cached = self._updates.get(key)
                if cached is not None:
                    return cached
                update = config_service_pb2.ConfigUpdate(full=target, target_digest=digest)
            else:
                # C3 owns this semantic diff.
                from lumewarp_control_plane.compiler import diff

                update = config_service_pb2.ConfigUpdate(
                    delta=diff(base_snapshot, target), target_digest=digest
                )
            if update.ByteSize() > min(settings.grpc_max_message_bytes, _GRPC_HARD_MESSAGE_LIMIT):
                raise RuntimeError("config update exceeds configured gRPC message limit")
            self._updates[key] = update
            while len(self._updates) > _UPDATE_CACHE_ENTRIES:
                self._updates.popitem(last=False)
            return update

    async def _mirror(self, state: _StreamState, *, disconnected: bool = False) -> None:
        if self.session_factory is None:
            return
        now = datetime.now(UTC)
        pending = state.pending
        acked = state.acked_version if state.acked_version <= 2**63 - 1 else None
        values = {
            "scope": state.scope,
            "gateway_id": state.gateway_id,
            "session_id": state.session_id,
            "connected": not disconnected,
            "gateway_version": state.gateway_version[:100],
            "capabilities": state.capabilities,
            "peer": (state.peer or "")[:200] or None,
            "hello_have_version": state.hello_version if state.hello_version <= 2**63 - 1 else None,
            "acked_version": acked,
            "pending_version": pending.version if pending else None,
            "last_nack_version": state.last_nack_version,
            "last_nack_code": state.last_nack_code,
            "last_nack_reason": state.last_nack_reason,
            "quarantined_version": max(state.quarantined_versions, default=None),
            "connected_at": state.connected_at,
            "disconnected_at": now if disconnected else None,
            "last_seen_at": now,
        }
        stmt = pg_insert(GatewaySession).values(**values)
        updates = {
            key: getattr(stmt.excluded, key) for key in values if key not in ("scope", "gateway_id")
        }
        # A replaced stream must not overwrite the newer connection's status row on exit.
        # Session ids are UUIDv7 (time-ordered), so a newer session may take over the row
        # of an older one while an older session can never overwrite a newer one.
        stmt = stmt.on_conflict_do_update(
            index_elements=[GatewaySession.scope, GatewaySession.gateway_id],
            set_=updates,
            where=GatewaySession.session_id <= stmt.excluded.session_id,
        )
        async with self.session_factory() as session, session.begin():
            await session.execute(stmt)

    @asynccontextmanager
    async def stream(
        self,
        identity: GatewayIdentity,
        hello: config_service_pb2.Hello,
        peer: str | None,
    ) -> AsyncIterator[_StreamState]:
        quarantine: set[int] = set()
        last_nack_version = None
        last_nack_code = None
        last_nack_reason = None
        if self.session_factory is not None:
            async with self.session_factory() as session:
                previous = await session.get(GatewaySession, (identity.scope, identity.gateway_id))
                if previous is not None:
                    if previous.quarantined_version is not None:
                        quarantine.add(previous.quarantined_version)
                    last_nack_version = previous.last_nack_version
                    last_nack_code = previous.last_nack_code
                    last_nack_reason = previous.last_nack_reason
        state = _StreamState(
            identity=identity,
            session_id=uuid7(),
            hello_version=hello.have_version,
            hello_digest=bytes(hello.have_digest),
            hello_scope_matches=hello.scope == identity.scope,
            gateway_version=hello.gateway_version,
            capabilities=list(hello.supported_capabilities),
            peer=peer,
            connected_at=datetime.now(UTC),
            acked_version=hello.have_version,
            quarantined_versions=quarantine,
            last_nack_version=last_nack_version,
            last_nack_code=last_nack_code,
            last_nack_reason=last_nack_reason,
        )
        key = (identity.scope, identity.gateway_id)
        async with self._lock:
            old = self._sessions.get(key)
            if old is not None:
                # Replicas must not share a gateway_id/token: they would evict each other.
                log.warning(
                    "gateway %s/%s reconnected; replacing its previous stream (peer %s -> %s)",
                    identity.scope,
                    identity.gateway_id,
                    old.peer,
                    peer,
                )
                old.close(grpc.StatusCode.UNAVAILABLE, "gateway stream replaced")
            self._sessions[key] = state
        try:
            await self._mirror(state)
            yield state
        finally:
            async with self._lock:
                if self._sessions.get(key) is state:
                    self._sessions.pop(key, None)
            await self._mirror(state, disconnected=True)


async def _desired_update(
    state: _StreamState,
    settings: Settings,
    session_factory: SessionFactory,
    registry: SessionRegistry,
) -> config_service_pb2.ConfigUpdate | None:
    """Pick the next update for an idle session from version metadata; payloads are only
    loaded (through the registry cache) when an update is actually sent."""
    if state.pending is not None or state.closed is not None:
        return None
    async with session_factory() as session:
        latest = (
            await session.execute(
                select(ConfigVersion.version, ConfigVersion.digest)
                .where(ConfigVersion.scope == state.scope)
                .order_by(ConfigVersion.version.desc())
                .limit(1)
            )
        ).one_or_none()
        if latest is None:
            return None
        version, digest = latest.version, bytes(latest.digest)
        if version in state.quarantined_versions:
            return None
        if not state.hello_checked:
            # The Hello version is a delta base only if it is this scope's content: same
            # scope, not ahead of us (restore from backup) and the digest we stored for it.
            state.hello_checked = True
            if (
                state.hello_scope_matches
                and 0 < state.hello_version <= version
                and state.hello_digest
            ):
                stored = await session.scalar(
                    select(ConfigVersion.digest).where(
                        ConfigVersion.scope == state.scope,
                        ConfigVersion.version == state.hello_version,
                    )
                )
                if stored is not None and bytes(stored) == state.hello_digest:
                    state.base = (state.hello_version, state.hello_digest)
        base = state.base
        if base == (version, digest):
            return None
        if base is not None and base[0] < version:
            row = (
                await session.execute(
                    select(ConfigVersion.digest, ConfigVersion.compacted_at).where(
                        ConfigVersion.scope == state.scope, ConfigVersion.version == base[0]
                    )
                )
            ).one_or_none()
            if row is None or row.compacted_at is not None or bytes(row.digest) != base[1]:
                base = None
        else:
            base = None
        update = await registry.build_update(session, settings, state.scope, version, digest, base)
        if state.closed is not None or state.pending is not None:
            return None
        state.pending = _PendingUpdate(
            message=update, version=version, digest=digest, sent_at=time.monotonic()
        )
        if update.WhichOneof("update") == "full":
            # A NACKed full is never followed by a delta: no base until this one is ACKed.
            state.base = None
    await registry._mirror(state)
    return update


async def _token_active(session_factory: SessionFactory, token_id: uuid.UUID) -> bool:
    async with session_factory() as session:
        token = await session.get(GatewayToken, token_id)
        return token is not None and token.revoked_at is None


class _ConfigService(config_service_pb2_grpc.ConfigServiceServicer):
    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        registry: SessionRegistry,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.registry = registry

    async def Stream(self, request_iterator, context):
        identity = _identity()
        if identity is None:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "invalid gateway credentials")
        try:
            first = await anext(request_iterator)
        except StopAsyncIteration:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Hello is required")
        if first.WhichOneof("msg") != "hello":
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "first message must be Hello")
        hello = first.hello
        if hello.gateway_id != identity.gateway_id:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "gateway_id does not match token"
            )

        async with self.registry.stream(identity, hello, context.peer()) as state:
            update = await _desired_update(
                state, self.settings, self.session_factory, self.registry
            )
            if update is not None:
                yield update
            request_task = asyncio.create_task(anext(request_iterator))
            wake_task = None
            token_check_at = time.monotonic() + _TOKEN_RECHECK_SECONDS
            try:
                while True:
                    if state.closed is not None:
                        await context.abort(*state.closed)
                    if wake_task is None:
                        wake_task = asyncio.create_task(state.wake.wait())
                    pending = state.pending
                    now = time.monotonic()
                    deadlines = [_PUBLISH_POLL_SECONDS, max(0.0, token_check_at - now)]
                    if pending is not None:
                        if pending.retry_at is not None:
                            deadlines.append(max(0.0, pending.retry_at - now))
                        else:
                            deadlines.append(
                                max(
                                    0.0,
                                    self.settings.ack_timeout_seconds - (now - pending.sent_at),
                                )
                            )
                    done, _ = await asyncio.wait(
                        (request_task, wake_task),
                        timeout=min(deadlines),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # A replaced or revoked stream must not emit anything after its wake-up.
                    if state.closed is not None:
                        await context.abort(*state.closed)
                    if time.monotonic() >= token_check_at:
                        if not await _token_active(self.session_factory, identity.token_id):
                            await context.abort(
                                grpc.StatusCode.PERMISSION_DENIED, "gateway token revoked"
                            )
                        token_check_at = time.monotonic() + _TOKEN_RECHECK_SECONDS

                    if request_task in done:
                        try:
                            incoming = request_task.result()
                        except StopAsyncIteration:
                            return
                        request_task = asyncio.create_task(anext(request_iterator))
                        await self._handle_reply(state, incoming, context)
                        update = await _desired_update(
                            state, self.settings, self.session_factory, self.registry
                        )
                        if update is not None:
                            yield update
                        continue

                    if wake_task in done:
                        state.wake.clear()
                        wake_task = None
                    now = time.monotonic()
                    pending = state.pending
                    if pending is not None:
                        if pending.retry_at is not None and now >= pending.retry_at:
                            pending.retry_at = None
                            pending.sent_at = now
                            await self.registry._mirror(state)
                            yield pending.message
                        elif (
                            pending.retry_at is None
                            and now - pending.sent_at >= self.settings.ack_timeout_seconds
                        ):
                            await context.abort(
                                grpc.StatusCode.DEADLINE_EXCEEDED,
                                "config update ACK timeout",
                            )
                    else:
                        # Polling is deliberate: it recovers even when the compiler could
                        # not reach this process to deliver an in-memory wake-up.
                        update = await _desired_update(
                            state, self.settings, self.session_factory, self.registry
                        )
                        if update is not None:
                            yield update
            finally:
                request_task.cancel()
                if wake_task is not None:
                    wake_task.cancel()
                await asyncio.gather(
                    request_task,
                    *(tuple([wake_task]) if wake_task is not None else ()),
                    return_exceptions=True,
                )

    async def _handle_reply(self, state: _StreamState, incoming, context) -> None:
        kind = incoming.WhichOneof("msg")
        if kind == "hello":
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Hello may only be sent once")
        pending = state.pending
        if pending is None or kind not in ("ack", "nack"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "ACK/NACK without a pending update"
            )
        reply = getattr(incoming, kind)
        if reply.version != pending.version:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "ACK/NACK version does not match pending update",
            )
        if kind == "ack":
            state.acked_version = pending.version
            state.base = (pending.version, pending.digest)
            state.pending = None
            state.last_nack_version = None
            state.last_nack_code = None
            state.last_nack_reason = None
            await self.registry._mirror(state)
            return

        code = reply.code
        reasons = {
            config_service_pb2.NACK_CODE_BASE_MISMATCH: "base mismatch",
            config_service_pb2.NACK_CODE_INVALID_CONFIG: "invalid config",
            config_service_pb2.NACK_CODE_UNSUPPORTED_FEATURE: "unsupported feature",
            config_service_pb2.NACK_CODE_PERSIST_FAILED: "persist failed",
        }
        if code not in reasons:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unknown NACK code")
        state.last_nack_version = pending.version
        state.last_nack_code = config_service_pb2.NackCode.Name(code)
        state.last_nack_reason = reasons[code]
        if code == config_service_pb2.NACK_CODE_BASE_MISMATCH:
            state.acked_version = reply.have_version
            state.base = None
            state.pending = None
        elif code in (
            config_service_pb2.NACK_CODE_INVALID_CONFIG,
            config_service_pb2.NACK_CODE_UNSUPPORTED_FEATURE,
        ):
            # The gateway keeps its previous version, so a delta base (if any) still holds;
            # after a NACKed full, base is already None.
            state.quarantined_versions.add(pending.version)
            state.pending = None
        else:
            pending.retry_at = time.monotonic() + pending.retry_delay
            pending.retry_delay = min(pending.retry_delay * 2, _RETRY_MAX_SECONDS)
        await self.registry._mirror(state)


class _UsageService(usage_pb2_grpc.UsageServiceServicer):
    def __init__(self, settings: Settings, session_factory: SessionFactory) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def Report(self, request, context):
        identity = _identity()
        if identity is None:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "invalid gateway credentials")
        if request.gateway_id != identity.gateway_id:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "gateway_id does not match token"
            )
        try:
            async with self.session_factory() as session, session.begin():
                accepted = await ingest(
                    session,
                    identity.gateway_id,
                    request.events,
                    scope_name=identity.scope,
                    max_batch_events=self.settings.max_usage_batch_events,
                )
        except InvalidUsageBatch as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return usage_pb2.ReportResponse(accepted=accepted)


async def serve(settings: Settings, session_factory: SessionFactory) -> grpc.aio.Server:
    """Start ConfigService, UsageService and gRPC health with symmetric 64 MiB limits.

    If TLS cert and key are configured the listener uses TLS; otherwise it is plaintext and
    must be protected by a trusted local proxy/mesh. The returned server exposes its registry
    as ``server.session_registry`` (useful for publish wakeups) and its selected ephemeral
    port as ``server.bound_port`` when grpc_bind uses port 0.
    """
    if bool(settings.grpc_tls_cert_file) != bool(settings.grpc_tls_key_file):
        raise ValueError("both gRPC TLS certificate and private key must be configured")
    message_limit = min(settings.grpc_max_message_bytes, _GRPC_HARD_MESSAGE_LIMIT)
    if message_limit <= 0:
        raise ValueError("gRPC message limit must be positive")
    options = (
        ("grpc.max_send_message_length", message_limit),
        ("grpc.max_receive_message_length", message_limit),
    )
    registry = SessionRegistry(session_factory)
    server = grpc.aio.server(
        options=options,
        interceptors=(_GatewayAuthInterceptor(settings, session_factory),),
    )
    config_service_pb2_grpc.add_ConfigServiceServicer_to_server(
        _ConfigService(settings, session_factory, registry), server
    )
    usage_pb2_grpc.add_UsageServiceServicer_to_server(
        _UsageService(settings, session_factory), server
    )
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set(
        "lumewarp.config.v1.ConfigService", health_pb2.HealthCheckResponse.SERVING
    )
    await health_servicer.set(
        "lumewarp.usage.v1.UsageService", health_pb2.HealthCheckResponse.SERVING
    )

    if settings.grpc_tls_cert_file:
        with open(settings.grpc_tls_key_file, "rb") as key_file:
            private_key = key_file.read()
        with open(settings.grpc_tls_cert_file, "rb") as cert_file:
            certificate_chain = cert_file.read()
        credentials = grpc.ssl_server_credentials(((private_key, certificate_chain),))
        bound_port = server.add_secure_port(settings.grpc_bind, credentials)
    else:
        bound_port = server.add_insecure_port(settings.grpc_bind)
    if bound_port == 0:
        raise OSError(f"could not bind gRPC listener to {settings.grpc_bind}")
    # P1 runs one control-plane instance per scope. Its process-local registry starts
    # empty, so any connected mirror belongs to a previous process (possibly crashed).
    # Commit before accepting streams; stale mirrors must not pin payloads indefinitely.
    async with session_factory() as session, session.begin():
        await session.execute(
            update(GatewaySession)
            .where(GatewaySession.scope == settings.scope, GatewaySession.connected.is_(True))
            .values(connected=False, disconnected_at=func.now())
        )
    await server.start()
    # grpc.aio.Server currently permits attributes; keep a side table fallback for future
    # implementations that use slots.
    try:
        server.session_registry = registry
        server.bound_port = bound_port
    except AttributeError:
        _server_registries[id(server)] = registry
        _server_ports[id(server)] = bound_port
    return server


_server_registries: dict[int, SessionRegistry] = {}
_server_ports: dict[int, int] = {}


def registry_for(server: grpc.aio.Server) -> SessionRegistry | None:
    """Return the registry attached by ``serve`` for compiler wake-up wiring."""
    return getattr(server, "session_registry", _server_registries.get(id(server)))


def bound_port(server: grpc.aio.Server) -> int | None:
    """Return the port selected by ``serve`` when grpc_bind used port 0."""
    return getattr(server, "bound_port", _server_ports.get(id(server)))

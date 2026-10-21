"""Gateway-facing gRPC services (owner: C4): ConfigService and UsageService.

Behaviour is specified by docs/config-protocol.md. Both services authenticate the
`authorization: Bearer lwg-...` metadata against `gateway_tokens` (hash, not revoked,
scope == settings.scope) and use the token's gateway_id, never the one in the payload.
Message size limits come from settings.grpc_max_message_bytes on both directions.
"""

import uuid
from dataclasses import dataclass

import grpc
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.settings import Settings


@dataclass(frozen=True)
class GatewayIdentity:
    token_id: uuid.UUID
    gateway_id: str
    scope: str


async def authenticate_gateway(
    session: AsyncSession, token: str, scope: str
) -> GatewayIdentity | None:
    """Resolve a `lwg-` gateway token for `scope`, or None if unknown/revoked/other scope."""
    raise NotImplementedError


class SessionRegistry:
    """Per-gateway stream state for the session state machine (in memory).

    Tracks, per connected gateway: session id, ACKed version, the single pending
    (un-ACKed) version, quarantined version after INVALID_CONFIG/UNSUPPORTED_FEATURE,
    PERSIST_FAILED backoff. Mirrors changes into `gateway_sessions` for status/UI.
    `notify_published` wakes every session so it can send its next update.
    """

    def notify_published(self, scope: str, version: int) -> None:
        raise NotImplementedError


async def serve(settings: Settings, session_factory: SessionFactory) -> grpc.aio.Server:
    """Build and start the grpc.aio server on settings.grpc_bind (TLS if
    grpc_tls_cert_file/grpc_tls_key_file are set) with ConfigService and UsageService
    registered. Returns the started server; the caller stops it on shutdown.
    """
    raise NotImplementedError

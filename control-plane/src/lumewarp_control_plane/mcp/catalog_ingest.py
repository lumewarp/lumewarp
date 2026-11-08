"""CatalogReport ingestion: what a gateway observed at an MCP server or A2A agent.

The control plane cannot fetch these itself — it does not run WASM auth plugins and holds no
user's token — so a gateway reports the definitions it observed (`ObservedDefinition`) and
the agent cards it fetched (`AgentCardReport`) on the config stream, next to HealthReport.
Each report adds to what was reported before; unlike a health report it never replaces it.

The report is untrusted data and arrives bounded (`grpc._MAX_CATALOG_*`). Nothing here stores
it yet: the MCP/A2A data model and the review API land with the rest of the MCP work, which
implements this module. The gRPC arm already validates and hands the report over, so a
gateway can send one today without the stream failing.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2


async def ingest_report(
    session: AsyncSession, gateway_id: str, report: config_service_pb2.CatalogReport
) -> None:
    """Record one `CatalogReport` from `gateway_id` in the caller's transaction.

    Not implemented yet: the definitions and cards are dropped after the bounds check in
    `grpc.ConfigService`, and the gateway's next report brings them again (each definition is
    reported at most once per 10 minutes per gateway, a card every fetch).
    """

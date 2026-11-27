"""The user-facing MCP server (`/mcp`): Lumewarp's REST API as MCP tools for an MCP client.

Not to be confused with `lumewarp_control_plane.mcp`, which is the gateway's MCP proxy
catalog (upstream MCP servers the gateway serves to API keys).
"""

from lumewarp_control_plane.api_mcp.server import ApiMcp

__all__ = ["ApiMcp"]

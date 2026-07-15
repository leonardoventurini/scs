"""Independent headless MCP host for SCS code intelligence."""

from scs.mcp.gateway import SCSWireGateway, ServiceGateway
from scs.mcp.http import MCPHTTPServer
from scs.mcp.inventory import MOVED_TO_SCS_TOOLS, RETIRED_TOOLS
from scs.mcp.observability import ToolEvent, ToolRecorder
from scs.mcp.server import build_mcp

__all__ = [
    "MOVED_TO_SCS_TOOLS",
    "MCPHTTPServer",
    "RETIRED_TOOLS",
    "SCSWireGateway",
    "ServiceGateway",
    "ToolEvent",
    "ToolRecorder",
    "build_mcp",
]

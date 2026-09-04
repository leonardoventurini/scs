"""Independent headless MCP host for SCS code intelligence."""

from scs.mcp.gateway import SCSWireGateway, ServiceGateway
from scs.mcp.inventory import MCP_TOOL_NAMES
from scs.mcp.observability import ToolEvent, ToolRecorder
from scs.mcp.server import build_mcp

__all__ = [
    "MCP_TOOL_NAMES",
    "SCSWireGateway",
    "ServiceGateway",
    "ToolEvent",
    "ToolRecorder",
    "build_mcp",
]

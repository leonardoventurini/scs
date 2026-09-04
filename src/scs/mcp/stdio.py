"""Per-harness MCP stdio bridge for the shared lazy SCS daemon."""

from __future__ import annotations

import asyncio

from scs.config import SCSSettings
from scs.daemon import DaemonController
from scs.mcp.gateway import SCSWireGateway
from scs.mcp.server import build_mcp


async def serve_stdio(settings: SCSSettings | None = None) -> None:
    """Attach one bridge lease and serve MCP until its stdio peer exits."""

    resolved = settings or SCSSettings()
    await DaemonController(resolved).ensure_started()
    gateway = SCSWireGateway(resolved.paths.runtime / "scs.sock")
    await gateway.connect()
    try:
        await build_mcp(gateway).run_stdio_async()
    finally:
        await gateway.close()


def main() -> int:
    """Run the stdio bridge without writing diagnostics to stdout."""

    try:
        asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        return 130
    return 0

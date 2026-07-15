"""CLI entry point for the SCS MCP proxy daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Sequence

from .proxy import ProxyConfig, ProxyServer

_DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser used by :func:`main`."""
    parser = argparse.ArgumentParser(
        prog="scs-mcp-proxy",
        description=(
            "Localhost MCP reverse-proxy that owns the public port across daemon "
            "restarts. Queues requests during the restart blackout and replays "
            "them once the upstream returns."
        ),
    )
    parser.add_argument(
        "--public-host",
        default=ProxyConfig.public_host,
        help="Host to bind the public-facing MCP port on (default: %(default)s).",
    )
    parser.add_argument(
        "--public-port",
        type=int,
        default=ProxyConfig.public_port,
        help="Public MCP port (default: %(default)s).",
    )
    parser.add_argument(
        "--upstream-host",
        default=ProxyConfig.upstream_host,
        help="Host of the daemon's internal MCP server (default: %(default)s).",
    )
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=ProxyConfig.upstream_port,
        help="Port of the daemon's internal MCP server (default: %(default)s).",
    )
    parser.add_argument(
        "--wait-for-upstream-seconds",
        type=float,
        default=ProxyConfig.wait_for_upstream_seconds,
        help=(
            "How long to hold a request while retrying the initial upstream "
            "connection before returning 503 (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Proxy log verbosity (default: %(default)s).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> ProxyConfig:
    """Map CLI arguments onto an immutable :class:`ProxyConfig`."""
    return ProxyConfig(
        public_host=args.public_host,
        public_port=args.public_port,
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
        wait_for_upstream_seconds=args.wait_for_upstream_seconds,
    )


async def _run(config: ProxyConfig) -> None:
    """Serve the proxy until SIGINT/SIGTERM."""
    server = ProxyServer(config)
    await server.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        await server.stop()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point — returns the process exit code."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level), format=_DEFAULT_LOG_FORMAT
    )

    try:
        asyncio.run(_run(_config_from_args(args)))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

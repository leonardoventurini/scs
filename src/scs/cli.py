"""Headless command-line interface for SCS operations and ownership."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path

from scs.config import SCSSettings
from scs.main import serve
from scs.service import ServiceManager
from scs.wire.client import SCSClient


def build_parser() -> argparse.ArgumentParser:
    """Build the complete non-graphical SCS command contract."""

    parser = argparse.ArgumentParser(
        prog="scs", description="Semantic Code Search service"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="run the SCS daemon in the foreground")
    subcommands.add_parser("proxy", help="run the stable MCP proxy in the foreground")
    subcommands.add_parser("doctor", help="validate storage and daemon health")
    subcommands.add_parser("status", help="show daemon and launchd state")

    index = subcommands.add_parser("index", help="explicitly index a repository")
    index.add_argument("repo_path", type=Path)
    reindex = subcommands.add_parser(
        "reindex", help="explicitly rebuild a repository index"
    )
    reindex.add_argument("repo_path", type=Path)

    service = subcommands.add_parser("service", help="manage SCS user services")
    service.add_argument(
        "action",
        choices=("install", "start", "stop", "restart", "status", "uninstall"),
    )
    return parser


async def _call_daemon(
    method: str, params: dict[str, object] | None = None
) -> dict[str, object]:
    settings = SCSSettings()
    client = SCSClient(settings.paths.runtime / "scs.sock")
    return await client.call(method, params)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI command and return a process exit code."""

    arguments = build_parser().parse_args(argv)
    if arguments.command == "serve":
        asyncio.run(serve())
        return 0
    if arguments.command == "proxy":
        from scs_mcp_proxy.main import main as run_proxy

        return run_proxy(())
    if arguments.command == "service":
        manager = ServiceManager()
        action = getattr(manager, arguments.action)
        result = action()
        if result is not None:
            if not is_dataclass(result):
                raise TypeError("service command returned an unsupported result")
            print(json.dumps(asdict(result), sort_keys=True))
        return 0
    if arguments.command == "doctor":
        settings = SCSSettings()
        try:
            settings.paths.ensure()
        except Exception as error:
            print(
                json.dumps(
                    {
                        "storage": {
                            "available": False,
                            "error": type(error).__name__,
                            "message": str(error),
                        },
                        "daemon": {"available": False, "ready": False},
                    },
                    sort_keys=True,
                )
            )
            return 1
        try:
            result = asyncio.run(_call_daemon("system.health"))
        except Exception as error:
            print(
                json.dumps(
                    {
                        "storage": {
                            "available": True,
                            "home": str(settings.paths.home),
                        },
                        "daemon": {
                            "available": False,
                            "ready": False,
                            "error": type(error).__name__,
                            "message": str(error),
                        },
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "storage": {"available": True, "home": str(settings.paths.home)},
                    "daemon": {"available": True, **result},
                },
                sort_keys=True,
            )
        )
        return 0 if result.get("ready") is True else 1
    if arguments.command == "status":
        service_status = ServiceManager().status()
        try:
            daemon_result = asyncio.run(_call_daemon("system.health"))
            daemon_status = {"available": True, **daemon_result}
            exit_code = 0 if daemon_result.get("ready") is True else 1
        except Exception as error:
            daemon_status = {
                "available": False,
                "ready": False,
                "error": type(error).__name__,
                "message": str(error),
            }
            exit_code = 1
        print(
            json.dumps(
                {"launchd": asdict(service_status), "daemon": daemon_status},
                sort_keys=True,
            )
        )
        return exit_code
    if arguments.command in {"index", "reindex"}:
        method = f"repository.{arguments.command}"
        result = asyncio.run(
            _call_daemon(method, {"repo_path": str(arguments.repo_path)})
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    raise AssertionError(f"unhandled SCS command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare unified query evidence with versioned legacy route sequences."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

from scs.config import SCSSettings
from scs.daemon import DaemonController
from scs.evaluation.query import load_query_suite, run_query_evaluation
from scs.evaluation.search import wait_for_evaluation_daemon, wait_for_stable_index
from scs.wire.client import SCSConnection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate unified SCS code queries.")
    parser.add_argument("--suite", type=Path, default=Path("evals/scs-query-v1.json"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("fast", "balanced", "thorough"), default="balanced")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--wait-timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path)
    return parser


async def run(arguments: argparse.Namespace) -> dict[str, object]:
    settings = SCSSettings()
    suite = load_query_suite(cast(Path, arguments.suite))
    repo_path = str(cast(Path, arguments.repo).resolve())
    wait_timeout = cast(float, arguments.wait_timeout)
    await wait_for_evaluation_daemon(
        DaemonController(settings), timeout_seconds=wait_timeout
    )
    async with SCSConnection(settings.paths.runtime / "scs.sock") as connection:
        await wait_for_stable_index(
            connection, repo_path=repo_path, timeout_seconds=wait_timeout
        )
        return await run_query_evaluation(
            connection, suite, repo_path=repo_path,
            mode=cast(Literal["fast", "balanced", "thorough"], arguments.mode),
            k=cast(int, arguments.k), repeats=cast(int, arguments.repeats),
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    serialized = json.dumps(asyncio.run(run(arguments)), indent=2, sort_keys=True) + "\n"
    output = cast(Path | None, arguments.output)
    if output is None:
        print(serialized, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

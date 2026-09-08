#!/usr/bin/env python3
"""Run a versioned relevance suite against the public SCS search route."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scs.config import SCSSettings
from scs.daemon import DaemonController
from scs.evaluation.search import (
    ResultDetail,
    load_evaluation_suite,
    run_search_evaluation,
)
from scs.wire.client import SCSConnection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate SCS search relevance, payload size, and latency."
    )
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--result-detail", choices=("full", "compact"), default="compact"
    )
    parser.add_argument("--output", type=Path)
    return parser


async def run(arguments: argparse.Namespace) -> dict[str, object]:
    settings = SCSSettings()
    suite = load_evaluation_suite(cast(Path, arguments.suite))
    repo_path = str(cast(Path, arguments.repo).resolve())
    await DaemonController(settings).ensure_started()
    async with SCSConnection(settings.paths.runtime / "scs.sock") as connection:
        report = await run_search_evaluation(
            connection,
            suite,
            repo_path=repo_path,
            k=cast(int, arguments.k),
            repeats=cast(int, arguments.repeats),
            result_detail=cast(ResultDetail, arguments.result_detail),
            reranking_provider=settings.reranking_provider,
            reranking_model=settings.reranking_model,
        )
    return report.model_dump(mode="json")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    result = asyncio.run(run(arguments))
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    output = cast(Path | None, arguments.output)
    if output is None:
        print(serialized, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

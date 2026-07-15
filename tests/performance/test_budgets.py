"""Executable budgets for standalone SCS indexing, querying, and idle memory."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from scs.graph.native import NativeGraph
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline
from scs.providers.base import ProviderMetadata

INDEX_FILE_COUNT = 100
INDEX_BUDGET_SECONDS = 10.0
WARMED_QUERY_P95_BUDGET_SECONDS = 2.0
PRE_EMBEDDING_RSS_BUDGET_KIB = 300 * 1024
DAEMON_START_TIMEOUT_SECONDS = 15.0


def _graph(tmp_path: Path) -> NativeGraph:
    return NativeGraph(
        database_path=tmp_path / "index.db",
        vector_path=tmp_path / "index.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=ProviderMetadata(
            "disabled",
            "structural-only",
            2,
            available=False,
            reason="performance fixture disables embeddings",
        ),
    )


def test_structural_index_and_warmed_query_budgets(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for index in range(INDEX_FILE_COUNT):
        (repository / f"module_{index}.py").write_text(
            f"def symbol_{index}(value: int) -> int:\n    return value + {index}\n",
            encoding="utf-8",
        )
    graph = _graph(tmp_path / "storage")
    pipeline = IngestionPipeline(graph=graph, parser=NativeParser())

    started = time.perf_counter()
    result = pipeline.ingest(repository)
    index_seconds = time.perf_counter() - started

    assert result.files_changed == INDEX_FILE_COUNT
    assert result.files_failed == 0
    assert index_seconds <= INDEX_BUDGET_SECONDS

    graph.search_by_name_sync("symbol_42", limit=10)
    latencies: list[float] = []
    for _sample in range(40):
        started = time.perf_counter()
        matches = graph.search_by_name_sync("symbol_42", limit=10)
        latencies.append(time.perf_counter() - started)
        assert any(node.name == "symbol_42" for node in matches)
    p95 = sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1]
    assert p95 <= WARMED_QUERY_P95_BUDGET_SECONDS


@pytest.mark.skipif(shutil.which("ps") is None, reason="ps is required for RSS evidence")
def test_pre_embedding_daemon_rss_budget(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-rss-", dir="/tmp"))
    environment = os.environ.copy()
    environment.update(
        {
            "SCS_HOME": str(tmp_path / "home"),
            "SCS_MODEL_CACHE": str(tmp_path / "models"),
            "SCS_RUNTIME_DIR": str(runtime),
            "SCS_LOG_DIR": str(tmp_path / "logs"),
            "SCS_MCP_INTERNAL_PORT": "0",
            "SCS_EMBEDDING_DIMENSION": "2",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "scs.cli", "serve"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    identity = runtime / "daemon-service.json"
    deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECONDS
    try:
        while not identity.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise AssertionError(f"SCS daemon exited during RSS setup: {stderr}")
            time.sleep(0.05)
        assert identity.exists(), "SCS daemon did not publish identity before RSS deadline"
        completed = subprocess.run(
            [shutil.which("ps") or "ps", "-o", "rss=", "-p", str(process.pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        rss_kib = int(completed.stdout.strip())
        assert rss_kib <= PRE_EMBEDDING_RSS_BUDGET_KIB
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        identity_remained = identity.exists()
        shutil.rmtree(runtime, ignore_errors=True)

    assert not identity_remained

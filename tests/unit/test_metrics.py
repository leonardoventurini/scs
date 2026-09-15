from __future__ import annotations

import os
from pathlib import Path

from scs.metrics import AggregateMetrics


def test_metrics_persist_only_allowlisted_aggregate_dimensions(tmp_path: Path) -> None:
    metrics = AggregateMetrics(tmp_path / "metrics.db", tmp_path / "metrics.key")

    metrics.record(
        "knowledge.search",
        {
            "repo_path": "/private/repository",
            "query": "secret source phrase",
            "search_mode": "balanced",
            "result_detail": "compact",
        },
        status="ok",
        duration_ms=12.5,
    )

    report = metrics.report(days=1)
    assert report["totals"] == {"calls": 1, "errors": 0}
    row = report["operations"][0]
    assert row["method"] == "knowledge.search"
    assert row["search_mode"] == "balanced"
    assert row["result_detail"] == "compact"
    assert row["repo_id"] != "/private/repository"
    assert "secret source phrase" not in str(report)
    assert (tmp_path / "metrics.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "metrics.db").stat().st_mode & 0o777 == 0o600


def test_metrics_recover_from_corruption_and_remain_fail_open(tmp_path: Path) -> None:
    database = tmp_path / "metrics.db"
    database.write_bytes(b"not sqlite")

    metrics = AggregateMetrics(database, tmp_path / "metrics.key")
    metrics.record("system.health", {}, status="ok", duration_ms=1.0)

    assert metrics.report(days=1)["totals"] == {"calls": 1, "errors": 0}
    assert list(tmp_path.glob("metrics.db.corrupt-*"))


def test_metrics_retention_and_row_bound_are_enforced(tmp_path: Path) -> None:
    metrics = AggregateMetrics(
        tmp_path / "metrics.db",
        tmp_path / "metrics.key",
        retention_days=30,
        max_rows=2,
    )

    for method in ("one", "two", "three"):
        metrics.record(method, {}, status="ok", duration_ms=1.0)

    assert len(metrics.report(days=30)["operations"]) == 2


def test_metrics_key_is_not_made_more_permissive(tmp_path: Path) -> None:
    key = tmp_path / "metrics.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o644)

    AggregateMetrics(tmp_path / "metrics.db", key)

    assert key.stat().st_mode & 0o777 == 0o600

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scs.evaluation.search import (
    EvaluationCase,
    EvaluationSuite,
    RelevanceJudgment,
    aggregate_metrics,
    evaluate_case,
    load_evaluation_suite,
    run_search_evaluation,
    wait_for_evaluation_daemon,
    wait_for_stable_index,
)


def result(
    qualified_name: str,
    *,
    file_path: str,
    compact: bool = False,
) -> dict[str, object]:
    if compact:
        return {"qualified_name": qualified_name, "file_path": file_path}
    return {
        "metadata": {
            "qualified_name": qualified_name,
            "file_path": file_path,
        }
    }


def test_evaluate_case_computes_standard_rank_metrics() -> None:
    case = EvaluationCase(
        query="find parser",
        relevant=[
            RelevanceJudgment(
                qualified_name="package.parser", file_path="parser.py", relevance=3
            ),
            RelevanceJudgment(
                qualified_name="package.loader", file_path="loader.py", relevance=1
            ),
        ],
    )
    results = [
        result("package.loader", file_path="loader.py"),
        result("package.parser", file_path="parser.py", compact=True),
        result("package.other", file_path="other.py"),
    ]

    metrics = evaluate_case(
        case,
        results,
        k=2,
        latency_ms=12.5,
        response_bytes=640,
    )

    expected_dcg = 1.0 + 7.0 / math.log2(3)
    expected_ideal = 7.0 + 1.0 / math.log2(3)
    assert metrics.recall_at_k == 1.0
    assert metrics.reciprocal_rank == 1.0
    assert metrics.ndcg_at_k == pytest.approx(expected_dcg / expected_ideal)
    assert metrics.latency_ms == 12.5
    assert metrics.latency_samples_ms == [12.5]
    assert metrics.response_bytes == 640
    assert metrics.retrieved_relevant == 2


def test_evaluate_case_honors_file_scoped_judgments() -> None:
    case = EvaluationCase(
        query="find duplicated symbol",
        relevant=[
            RelevanceJudgment(
                qualified_name="package.run",
                file_path="wanted.py",
                relevance=2,
            )
        ],
    )

    metrics = evaluate_case(
        case,
        [
            result("package.run", file_path="other.py"),
            result("package.run", file_path="wanted.py"),
        ],
        k=1,
        latency_ms=1.0,
        response_bytes=10,
    )

    assert metrics.recall_at_k == 0.0
    assert metrics.reciprocal_rank == 0.5
    assert metrics.ndcg_at_k == 0.0


def test_aggregate_metrics_reports_macro_means_and_nearest_rank_p95() -> None:
    case = EvaluationCase(
        query="query",
        relevant=[RelevanceJudgment(qualified_name="package.target")],
    )
    samples = [
        evaluate_case(
            case,
            [result("package.target", file_path="target.py")],
            k=1,
            latency_ms=float(latency),
            response_bytes=latency * 10,
        )
        for latency in range(1, 21)
    ]

    aggregate = aggregate_metrics(samples)

    assert aggregate.query_count == 20
    assert aggregate.mean_recall_at_k == 1.0
    assert aggregate.mean_reciprocal_rank == 1.0
    assert aggregate.mean_ndcg_at_k == 1.0
    assert aggregate.mean_response_bytes == 105.0
    assert aggregate.mean_latency_ms == 10.5
    assert aggregate.p95_latency_ms == 19.0


def test_load_evaluation_suite_rejects_unknown_schema(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps({"schema_version": 2, "name": "future", "cases": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="evaluation suite"):
        load_evaluation_suite(suite_path)


@pytest.mark.asyncio
async def test_run_search_evaluation_uses_public_routes_and_repeats() -> None:
    class Caller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            active_params = params or {}
            self.calls.append((method, active_params))
            if method == "knowledge.stats":
                return {"status": "ready", "total_nodes": 3}
            return {
                "query": active_params["query"],
                "results": [
                    result("package.target", file_path="target.py", compact=True)
                ],
                "neighbors": [],
                "total": 1,
                "retrieval_mode": "hybrid_reranked",
            }

    caller = Caller()
    suite = EvaluationSuite(
        schema_version=1,
        name="synthetic",
        cases=[
            EvaluationCase(
                query="find target",
                relevant=[RelevanceJudgment(qualified_name="package.target")],
            )
        ],
    )

    report = await run_search_evaluation(
        caller,
        suite,
        repo_path="/repo",
        k=5,
        repeats=2,
        result_detail="compact",
        reranking_provider="omlx",
        reranking_model="test-reranker",
    )

    assert [method for method, _ in caller.calls] == [
        "knowledge.stats",
        "knowledge.search",
        "knowledge.search",
    ]
    assert caller.calls[1][1] == {
        "query": "find target",
        "limit": 5,
        "repo_path": "/repo",
        "result_detail": "compact",
    }
    assert report.suite_name == "synthetic"
    assert report.aggregate.mean_recall_at_k == 1.0
    assert report.queries[0].retrieval_mode == "hybrid_reranked"
    assert len(report.queries[0].latency_samples_ms) == 2
    assert report.queries[0].response_bytes > 0
    assert report.environment.reranking_provider == "omlx"


@pytest.mark.asyncio
async def test_wait_for_stable_index_observes_active_jobs_until_completion() -> None:
    class Caller:
        def __init__(self) -> None:
            self.calls = 0

        async def call(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            assert method == "jobs.recent"
            assert params == {"repo_path": "/repo", "limit": 20}
            self.calls += 1
            status = "running" if self.calls == 1 else "completed"
            return {"jobs": [{"status": status}]}

    caller = Caller()

    await wait_for_stable_index(
        caller,
        repo_path="/repo",
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert caller.calls == 2


@pytest.mark.asyncio
async def test_wait_for_evaluation_daemon_outlives_controller_start_deadline() -> None:
    class Status:
        def __init__(self, ready: bool) -> None:
            self.ready = ready

    class Controller:
        def __init__(self) -> None:
            self.status_calls = 0

        async def ensure_started(self) -> Status:
            raise TimeoutError("short controller deadline")

        async def status(self) -> Status:
            self.status_calls += 1
            return Status(ready=self.status_calls == 2)

    controller = Controller()

    await wait_for_evaluation_daemon(
        controller,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
    )

    assert controller.status_calls == 2

"""The query evaluation gate uses stable, independently checked metrics."""

import pytest
from typing import cast
from pydantic import ValidationError

from scs.evaluation.query import (
    QueryEvaluationCase,
    QueryEvaluationSuite,
    _identities,
    evaluate_observation,
    run_query_evaluation,
    summarize_observations,
)


def test_node_list_identity_uses_source_path_before_opaque_id() -> None:
    response = {
        "nodes": [
            {"id": "opaque", "metadata": {"file_path": "src/scs/main.py"}},
            {"id": "id-only", "metadata": {}},
        ]
    }

    assert _identities(response) == ["src/scs/main.py", "id-only"]


def test_ranked_evidence_and_calibration() -> None:
    case = QueryEvaluationCase.model_validate(
        {
            "goal": "Find parser",
            "expected_playbook": "DISCOVER",
            "relevant": [
                {"identity": "src/scs/parser.py", "grade": 3},
                {"identity": "src/scs/graph.py", "grade": 1},
            ],
            "baseline": [{"method": "knowledge.search", "params": {"query": "Find parser"}}],
        }
    )
    observed = evaluate_observation(
        case,
        ["src/scs/graph.py", "unsupported", "src/scs/parser.py"],
        playbook="DISCOVER",
        confidence=0.8,
        k=3,
        calls=1,
        response_bytes=100,
        latency_ms=10,
    )
    assert observed.recall_at_k == 1
    assert observed.reciprocal_rank == 1
    assert observed.unsupported_rate == 1 / 3
    assert round(observed.brier_score, 4) == 0.04
    assert 0 < observed.ndcg_at_k < 1


def test_aggregate_reports_per_playbook_and_latency_percentile() -> None:
    case = QueryEvaluationCase.model_validate(
        {
            "goal": "Find parser",
            "expected_playbook": "DISCOVER",
            "relevant": [{"identity": "a", "grade": 1}],
            "baseline": [{"method": "knowledge.search", "params": {"query": "a"}}],
        }
    )
    first = evaluate_observation(
        case, ["a"], playbook="DISCOVER", confidence=0.9,
        k=10, calls=1, response_bytes=100, latency_ms=10,
    )
    second = evaluate_observation(
        case, ["b"], playbook="IMPACT", confidence=0.9,
        k=10, calls=1, response_bytes=200, latency_ms=20,
    )
    summary = summarize_observations([first, second])
    assert summary["routing_accuracy"] == 0.5
    assert summary["per_playbook_recall"] == {"DISCOVER": 0.5}
    assert summary["mean_recall_at_k"] == 0.5
    assert summary["p95_latency_ms"] == 20
    assert summary["expected_calibration_error"] == 0.4


@pytest.mark.asyncio
async def test_runner_warms_both_paths_and_counts_measured_calls() -> None:
    class FakeCaller:
        def __init__(self) -> None:
            self.methods: list[str] = []

        async def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            self.methods.append(method)
            if method == "knowledge.query":
                return {
                    "routing": {"playbook": "DISCOVER", "confidence": 0.9},
                    "evidence": {"symbols": [{"file_path": "a.py"}]},
                    "trace": [], "timings": {"classification_ms": 3},
                    "complete": True,
                }
            return {"results": [{"file_path": "a.py"}]}

    suite = QueryEvaluationSuite.model_validate({
        "schema_version": 1,
        "name": "test",
        "cases": [{
            "goal": "Find a", "expected_playbook": "DISCOVER",
            "relevant": [{"identity": "a.py", "grade": 1}],
            "baseline": [{"method": "knowledge.search", "params": {"query": "a"}}],
        }],
    })
    caller = FakeCaller()
    report = await run_query_evaluation(
        caller, suite, repo_path="/repo", mode="balanced"
    )
    assert caller.methods == [
        "knowledge.query", "knowledge.search", "knowledge.query", "knowledge.search"
    ]
    assert report["warmup"] is True
    assert cast(dict[str, object], report["unified_summary"])["mean_recall_at_k"] == 1


def test_suite_anchor_cannot_override_repository_or_mode() -> None:
    with pytest.raises(ValidationError):
        QueryEvaluationCase.model_validate({
            "goal": "Find a", "expected_playbook": "DISCOVER",
            "anchors": {"repo_path": "/other"},
            "relevant": [{"identity": "a.py", "grade": 1}],
            "baseline": [{"method": "knowledge.search", "params": {"query": "a"}}],
        })

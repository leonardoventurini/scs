"""Versioned relevance suites and deterministic code-search metrics."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ResultDetail = Literal["full", "compact"]
ACTIVE_JOB_STATUSES: Final[frozenset[str]] = frozenset(
    {"queued", "retrying", "running", "cancelling"}
)


class _StrictModel(BaseModel):
    """Reject silent schema drift in reusable evaluation artifacts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)


class RelevanceJudgment(_StrictModel):
    """One graded relevant symbol, optionally disambiguated by source path."""

    qualified_name: str = Field(min_length=1)
    file_path: str | None = None
    relevance: int = Field(default=1, ge=1)


class EvaluationCase(_StrictModel):
    """One natural-language query and its graded relevance judgments."""

    query: str = Field(min_length=1)
    relevant: list[RelevanceJudgment] = Field(min_length=1)


class EvaluationSuite(_StrictModel):
    """Versioned collection of repository-specific search judgments."""

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    cases: list[EvaluationCase] = Field(min_length=1)


class QueryMetrics(_StrictModel):
    """Quality, size, and latency measurements for one evaluation query."""

    query: str
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float
    retrieved_relevant: int
    relevant_total: int
    latency_ms: float
    latency_samples_ms: list[float] = Field(min_length=1)
    response_bytes: int
    retrieval_mode: str


class AggregateMetrics(_StrictModel):
    """Macro quality means and aggregate operational measurements."""

    query_count: int
    mean_recall_at_k: float
    mean_reciprocal_rank: float
    mean_ndcg_at_k: float
    mean_response_bytes: float
    mean_latency_ms: float
    p95_latency_ms: float


class EvaluationEnvironment(_StrictModel):
    """Configuration identity needed to compare evaluation runs."""

    graph_stats: dict[str, object]
    reranking_provider: str
    reranking_model: str


class SearchEvaluationReport(_StrictModel):
    """Machine-readable result of one complete search evaluation run."""

    schema_version: Literal[1] = 1
    generated_at: datetime
    suite_name: str
    repo_path: str
    k: int
    repeats: int
    result_detail: ResultDetail
    environment: EvaluationEnvironment
    queries: list[QueryMetrics]
    aggregate: AggregateMetrics


class SearchRouteCaller(Protocol):
    """Minimal public service client used by the evaluation runner."""

    async def call(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Call one public SCS route."""

        ...


def load_evaluation_suite(path: Path) -> EvaluationSuite:
    """Load and strictly validate one versioned JSON relevance suite."""

    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        return EvaluationSuite.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid search evaluation suite {path}: {exc}") from exc


def evaluate_case(
    case: EvaluationCase,
    results: Sequence[Mapping[str, object]],
    *,
    k: int,
    latency_ms: float,
    response_bytes: int,
    retrieval_mode: str = "unknown",
    latency_samples_ms: Sequence[float] | None = None,
) -> QueryMetrics:
    """Compute standard ranked-retrieval metrics for one observed response."""

    if k < 1:
        raise ValueError("evaluation cutoff k must be positive")

    matched: set[int] = set()
    grades: list[int] = []
    first_relevant_rank: int | None = None
    retrieved_at_k = 0
    for rank, result in enumerate(results, start=1):
        qualified_name, file_path = _result_identity(result)
        judgment_index = _matching_judgment(
            case.relevant,
            qualified_name=qualified_name,
            file_path=file_path,
            excluded=matched,
        )
        grade = (
            case.relevant[judgment_index].relevance
            if judgment_index is not None
            else 0
        )
        grades.append(grade)
        if judgment_index is not None:
            matched.add(judgment_index)
            if first_relevant_rank is None:
                first_relevant_rank = rank
            if rank <= k:
                retrieved_at_k += 1

    ideal_grades = sorted(
        (judgment.relevance for judgment in case.relevant), reverse=True
    )
    ideal_dcg = _discounted_gain(ideal_grades[:k])
    ndcg = _discounted_gain(grades[:k]) / ideal_dcg if ideal_dcg else 0.0
    samples = list(latency_samples_ms) if latency_samples_ms is not None else [latency_ms]
    if not samples:
        raise ValueError("latency samples cannot be empty")
    return QueryMetrics(
        query=case.query,
        recall_at_k=retrieved_at_k / len(case.relevant),
        reciprocal_rank=(
            1.0 / first_relevant_rank if first_relevant_rank is not None else 0.0
        ),
        ndcg_at_k=ndcg,
        retrieved_relevant=retrieved_at_k,
        relevant_total=len(case.relevant),
        latency_ms=latency_ms,
        latency_samples_ms=samples,
        response_bytes=response_bytes,
        retrieval_mode=retrieval_mode,
    )


def aggregate_metrics(metrics: Sequence[QueryMetrics]) -> AggregateMetrics:
    """Aggregate per-query observations with macro means and nearest-rank p95."""

    if not metrics:
        return AggregateMetrics(
            query_count=0,
            mean_recall_at_k=0.0,
            mean_reciprocal_rank=0.0,
            mean_ndcg_at_k=0.0,
            mean_response_bytes=0.0,
            mean_latency_ms=0.0,
            p95_latency_ms=0.0,
        )
    count = len(metrics)
    latencies = sorted(
        sample for metric in metrics for sample in metric.latency_samples_ms
    )
    p95_index = math.ceil(len(latencies) * 0.95) - 1
    return AggregateMetrics(
        query_count=count,
        mean_recall_at_k=sum(metric.recall_at_k for metric in metrics) / count,
        mean_reciprocal_rank=(
            sum(metric.reciprocal_rank for metric in metrics) / count
        ),
        mean_ndcg_at_k=sum(metric.ndcg_at_k for metric in metrics) / count,
        mean_response_bytes=(
            sum(metric.response_bytes for metric in metrics) / count
        ),
        mean_latency_ms=sum(latencies) / len(latencies),
        p95_latency_ms=latencies[p95_index],
    )


async def run_search_evaluation(
    caller: SearchRouteCaller,
    suite: EvaluationSuite,
    *,
    repo_path: str,
    k: int,
    repeats: int,
    result_detail: ResultDetail,
    reranking_provider: str,
    reranking_model: str,
) -> SearchEvaluationReport:
    """Execute a suite through public routes and return a comparable report."""

    if k < 1:
        raise ValueError("evaluation cutoff k must be positive")
    if repeats < 1:
        raise ValueError("evaluation repeats must be positive")

    graph_stats = await caller.call("knowledge.stats", {"repo_path": repo_path})
    observations: list[QueryMetrics] = []
    for case in suite.cases:
        latencies: list[float] = []
        sizes: list[int] = []
        response: dict[str, object] = {}
        for _ in range(repeats):
            started = time.perf_counter()
            response = await caller.call(
                "knowledge.search",
                {
                    "query": case.query,
                    "limit": k,
                    "repo_path": repo_path,
                    "result_detail": result_detail,
                },
            )
            latencies.append((time.perf_counter() - started) * 1_000.0)
            sizes.append(
                len(
                    json.dumps(
                        response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            )
        results = _response_results(response)
        retrieval_mode = response.get("retrieval_mode")
        observations.append(
            evaluate_case(
                case,
                results,
                k=k,
                latency_ms=sum(latencies) / len(latencies),
                response_bytes=round(sum(sizes) / len(sizes)),
                latency_samples_ms=latencies,
                retrieval_mode=(
                    retrieval_mode if isinstance(retrieval_mode, str) else "unknown"
                ),
            )
        )

    return SearchEvaluationReport(
        generated_at=datetime.now(UTC),
        suite_name=suite.name,
        repo_path=repo_path,
        k=k,
        repeats=repeats,
        result_detail=result_detail,
        environment=EvaluationEnvironment(
            graph_stats=graph_stats,
            reranking_provider=reranking_provider,
            reranking_model=reranking_model,
        ),
        queries=observations,
        aggregate=aggregate_metrics(observations),
    )


async def wait_for_stable_index(
    caller: SearchRouteCaller,
    *,
    repo_path: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.1,
) -> None:
    """Wait until no queued or running indexing job targets the repository."""

    if timeout_seconds <= 0:
        raise ValueError("index wait timeout must be positive")
    if poll_interval_seconds < 0:
        raise ValueError("index poll interval cannot be negative")

    deadline = time.monotonic() + timeout_seconds
    while True:
        response = await caller.call(
            "jobs.recent", {"repo_path": repo_path, "limit": 20}
        )
        raw_jobs = response.get("jobs")
        if not isinstance(raw_jobs, list):
            raise TypeError("jobs.recent response jobs must be a list")
        jobs = cast(list[object], raw_jobs)
        if not all(isinstance(job, Mapping) for job in jobs):
            raise TypeError("jobs.recent jobs must contain objects")
        active = any(
            cast(Mapping[str, object], job).get("status") in ACTIVE_JOB_STATUSES
            for job in jobs
        )
        if not active:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for repository indexing to become idle")
        await asyncio.sleep(poll_interval_seconds)


def _result_identity(result: Mapping[str, object]) -> tuple[str | None, str | None]:
    metadata_value = result.get("metadata")
    metadata: Mapping[str, object] = (
        cast(Mapping[str, object], metadata_value)
        if isinstance(metadata_value, Mapping)
        else cast(Mapping[str, object], {})
    )
    qualified_name = result.get("qualified_name", metadata.get("qualified_name"))
    file_path = result.get("file_path", metadata.get("file_path"))
    return (
        qualified_name if isinstance(qualified_name, str) else None,
        file_path if isinstance(file_path, str) else None,
    )


def _matching_judgment(
    judgments: Sequence[RelevanceJudgment],
    *,
    qualified_name: str | None,
    file_path: str | None,
    excluded: set[int],
) -> int | None:
    for index, judgment in enumerate(judgments):
        if index in excluded or qualified_name != judgment.qualified_name:
            continue
        if judgment.file_path is None or judgment.file_path == file_path:
            return index
    return None


def _discounted_gain(grades: Sequence[int]) -> float:
    return sum(
        ((2**grade) - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(grades, start=1)
    )


def _response_results(response: Mapping[str, object]) -> list[dict[str, object]]:
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        raise TypeError("knowledge.search response results must be a list")
    items = cast(list[object], raw_results)
    if not all(isinstance(item, dict) for item in items):
        raise TypeError("knowledge.search results must contain objects")
    return [cast(dict[str, object], item) for item in items]

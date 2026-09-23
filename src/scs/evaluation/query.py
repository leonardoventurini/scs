"""Versioned unified-query judgments and deterministic comparison metrics."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scs.orchestration.decision import Playbook


class StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)


class EvidenceJudgment(StrictModel):
    identity: str = Field(min_length=1)
    grade: int = Field(ge=1, le=3)


class BaselineCall(StrictModel):
    method: Literal[
        "knowledge.search", "knowledge.related", "knowledge.inspect_file",
        "knowledge.nodes.list", "knowledge.composite.regression_risk",
        "lsp.references", "knowledge.graph_context",
    ]
    params: dict[str, object]


class QueryEvaluationCase(StrictModel):
    goal: str = Field(min_length=1)
    anchors: dict[str, object] = Field(default_factory=dict)
    expected_playbook: Playbook
    relevant: list[EvidenceJudgment] = Field(min_length=1)
    baseline: list[BaselineCall] = Field(min_length=1)

    @field_validator("expected_playbook", mode="before")
    @classmethod
    def _parse_playbook(cls, value: object) -> Playbook:
        return Playbook(value)

    @field_validator("anchors")
    @classmethod
    def _validate_anchors(cls, value: dict[str, object]) -> dict[str, object]:
        allowed = {"node_type", "symbol_name", "node_ids", "file_paths", "source_position"}
        if set(value) - allowed:
            raise ValueError("evaluation anchors contain unsupported fields")
        return value


class QueryEvaluationSuite(StrictModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    cases: list[QueryEvaluationCase] = Field(min_length=1)


class Observation(StrictModel):
    goal: str
    expected_playbook: Playbook
    playbook: str
    correct_route: bool
    confidence: float
    provider: str = "unknown"
    model: str = "unknown"
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float
    unsupported_rate: float
    brier_score: float
    calls: int
    response_bytes: int
    latency_ms: float
    classifier_ms: float = 0
    fallback: bool = False
    timeout: bool = False
    truncated: bool = False
    degraded: bool = False


class QueryCaller(Protocol):
    async def call(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]: ...


def load_query_suite(path: Path) -> QueryEvaluationSuite:
    """Validate a reviewable, versioned repository-specific suite."""

    return QueryEvaluationSuite.model_validate_json(path.read_text(encoding="utf-8"))


def _score_at_k(identities: Sequence[str], grades: Mapping[str, int], k: int) -> tuple[float, float, float, float]:
    ranked = list(dict.fromkeys(identities))[:k]
    hits = [grades.get(identity, 0) for identity in ranked]
    recall = sum(grade > 0 for grade in hits) / len(grades)
    reciprocal_rank = next((1 / rank for rank, grade in enumerate(hits, 1) if grade), 0.0)

    def gain(grade: int, rank: int) -> float:
        return float((1 << grade) - 1) / math.log2(rank + 1)

    dcg = sum(gain(grade, rank) for rank, grade in enumerate(hits, 1))
    ideal = sum(
        gain(grade, rank)
        for rank, grade in enumerate(sorted(grades.values(), reverse=True)[:k], 1)
    )
    unsupported = sum(grade == 0 for grade in hits) / len(hits) if hits else 0.0
    return recall, reciprocal_rank, dcg / ideal if ideal else 0.0, unsupported


def evaluate_observation(
    case: QueryEvaluationCase,
    identities: Sequence[str],
    *,
    playbook: str,
    confidence: float,
    k: int,
    calls: int,
    response_bytes: int,
    latency_ms: float,
    classifier_ms: float = 0,
    provider: str = "unknown",
    model: str = "unknown",
    fallback: bool = False,
    timeout: bool = False,
    truncated: bool = False,
    degraded: bool = False,
) -> Observation:
    """Score identities and routing against judgments, independent of the runner."""

    if k < 1 or calls < 1 or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("invalid evaluation cutoff, call count, or confidence")
    grades = {item.identity: item.grade for item in case.relevant}
    recall, reciprocal_rank, ndcg, unsupported = _score_at_k(identities, grades, k)
    correct = playbook == case.expected_playbook.value
    return Observation(
        goal=case.goal,
        expected_playbook=case.expected_playbook,
        playbook=playbook,
        correct_route=correct,
        confidence=confidence,
        provider=provider,
        model=model,
        recall_at_k=recall,
        reciprocal_rank=reciprocal_rank,
        ndcg_at_k=ndcg,
        unsupported_rate=unsupported,
        brier_score=(confidence - float(correct)) ** 2,
        calls=calls,
        response_bytes=response_bytes,
        latency_ms=latency_ms,
        classifier_ms=classifier_ms,
        fallback=fallback,
        timeout=timeout,
        truncated=truncated,
        degraded=degraded,
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize_observations(observations: Sequence[Observation]) -> dict[str, object]:
    """Report macro quality, routing, efficiency, latency, and calibration."""

    if not observations:
        raise ValueError("evaluation requires observations")
    count = len(observations)

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    playbooks = sorted({item.expected_playbook.value for item in observations})
    # Ten fixed-width confidence buckets make calibration comparable across runs.
    buckets: dict[int, list[Observation]] = {}
    for item in observations:
        buckets.setdefault(min(9, int(item.confidence * 10)), []).append(item)
    ece = sum(
        len(bucket) / count * abs(
            mean([float(item.correct_route) for item in bucket])
            - mean([item.confidence for item in bucket])
        )
        for bucket in buckets.values()
    )
    return {
        "case_count": count,
        "routing_accuracy": mean([float(item.correct_route) for item in observations]),
        "per_playbook_recall": {
            name: mean([
                float(item.correct_route) for item in observations
                if item.expected_playbook.value == name
            ]) for name in playbooks
        },
        "mean_recall_at_k": mean([item.recall_at_k for item in observations]),
        "mean_reciprocal_rank": mean([item.reciprocal_rank for item in observations]),
        "mean_ndcg_at_k": mean([item.ndcg_at_k for item in observations]),
        "mean_unsupported_rate": mean([item.unsupported_rate for item in observations]),
        "mean_calls": mean([float(item.calls) for item in observations]),
        "mean_response_bytes": mean([float(item.response_bytes) for item in observations]),
        "p50_latency_ms": _percentile([item.latency_ms for item in observations], 0.5),
        "p95_latency_ms": _percentile([item.latency_ms for item in observations], 0.95),
        "p95_classifier_ms": _percentile([item.classifier_ms for item in observations], 0.95),
        "fallback_rate": mean([float(item.fallback) for item in observations]),
        "timeout_rate": mean([float(item.timeout) for item in observations]),
        "truncation_rate": mean([float(item.truncated) for item in observations]),
        "degradation_rate": mean([float(item.degraded) for item in observations]),
        "expected_calibration_error": ece,
        "mean_brier_score": mean([item.brier_score for item in observations]),
    }


def _identities(value: object) -> list[str]:
    """Extract ranked symbol/file identities from both public response shapes."""

    found: list[str] = []
    if isinstance(value, dict):
        item = cast(dict[str, object], value)
        identity = item.get("file_path") or item.get("qualified_name") or item.get("id")
        if isinstance(identity, str) and identity:
            found.append(identity)
        for key, child in item.items():
            if key not in {"routing", "trace", "timings", "metadata", "evidence"}:
                found.extend(_identities(child))
            elif key == "evidence":
                found.extend(_identities(child))
    elif isinstance(value, list):
        for child in cast(list[object], value):
            found.extend(_identities(child))
    return list(dict.fromkeys(found))


def _bytes(response: dict[str, object]) -> int:
    return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode())


async def run_query_evaluation(
    caller: QueryCaller,
    suite: QueryEvaluationSuite,
    *,
    repo_path: str,
    mode: Literal["fast", "balanced", "thorough"],
    k: int = 10,
    repeats: int = 1,
) -> dict[str, object]:
    """Run unified and versioned legacy sequences against the same index."""

    if repeats < 1:
        raise ValueError("repeats must be positive")
    unified: list[Observation] = []
    baseline: list[Observation] = []
    first_case = suite.cases[0]
    await caller.call("knowledge.query", {
        **first_case.anchors, "goal": first_case.goal, "repo_path": repo_path,
        "mode": mode, "limit": k,
    })
    for step in first_case.baseline:
        await caller.call(step.method, {**step.params, "repo_path": repo_path})
    for case in suite.cases:
        for _ in range(repeats):
            params: dict[str, object] = {
                **case.anchors, "goal": case.goal, "repo_path": repo_path,
                "mode": mode, "limit": k,
            }
            started = time.perf_counter()
            response = await caller.call("knowledge.query", params)
            latency = (time.perf_counter() - started) * 1_000
            routing = response.get("routing")
            route = cast(dict[str, object], routing) if isinstance(routing, dict) else {}
            timings = response.get("timings")
            times = cast(dict[str, object], timings) if isinstance(timings, dict) else {}
            trace = response.get("trace")
            stages = cast(list[object], trace) if isinstance(trace, list) else []
            raw_confidence = route.get("confidence")
            raw_classifier_ms = times.get("classification_ms")
            unified.append(evaluate_observation(
                case, _identities(response.get("evidence")),
                playbook=str(route.get("playbook", "unknown")),
                confidence=float(raw_confidence) if isinstance(raw_confidence, (int, float)) else 0,
                provider=str(route.get("provider", "unknown")),
                model=str(route.get("model", "unknown")),
                k=k, calls=1, response_bytes=_bytes(response), latency_ms=latency,
                classifier_ms=float(raw_classifier_ms) if isinstance(raw_classifier_ms, (int, float)) else 0,
                fallback=route.get("fallback_applied") is True,
                timeout=any(
                    isinstance(stage, dict)
                    and cast(dict[str, object], stage).get("status") == "timeout"
                    for stage in stages
                ),
                truncated=response.get("truncated") is True,
                degraded=response.get("complete") is not True,
            ))

            baseline_results: list[str] = []
            baseline_bytes = 0
            started = time.perf_counter()
            for step in case.baseline:
                arguments = {**step.params, "repo_path": repo_path}
                old_response = await caller.call(step.method, arguments)
                baseline_results.extend(_identities(old_response))
                baseline_bytes += _bytes(old_response)
            baseline_latency = (time.perf_counter() - started) * 1_000
            baseline.append(evaluate_observation(
                case, baseline_results,
                playbook=case.expected_playbook.value, confidence=1,
                provider="legacy", model="none",
                k=k, calls=len(case.baseline), response_bytes=baseline_bytes,
                latency_ms=baseline_latency,
            ))
    unified_summary = summarize_observations(unified)
    baseline_summary = summarize_observations(baseline)
    return {
        "schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
        "suite": suite.name, "repo_path": repo_path,
        "mode": mode, "k": k, "repeats": repeats, "warmup": True,
        "unified": [item.model_dump(mode="json") for item in unified],
        "baseline": [item.model_dump(mode="json") for item in baseline],
        "unified_summary": unified_summary,
        "baseline_summary": baseline_summary,
        "gates": {
            "routing_accuracy": cast(float, unified_summary["routing_accuracy"]) >= 0.9,
            "evidence_recall": cast(float, unified_summary["mean_recall_at_k"]) >= cast(float, baseline_summary["mean_recall_at_k"]) - 0.02,
            "evidence_ndcg": cast(float, unified_summary["mean_ndcg_at_k"]) >= cast(float, baseline_summary["mean_ndcg_at_k"]) - 0.02,
            "call_reduction": cast(float, unified_summary["mean_calls"]) <= cast(float, baseline_summary["mean_calls"]) * 0.5,
            "byte_reduction": cast(float, unified_summary["mean_response_bytes"]) <= cast(float, baseline_summary["mean_response_bytes"]) * 0.75,
            "per_playbook_recall": all(value >= 0.8 for value in cast(dict[str, float], unified_summary["per_playbook_recall"]).values()),
            "balanced_latency": mode != "balanced" or cast(float, unified_summary["p95_latency_ms"]) < 5_000,
            "classifier_latency": cast(float, unified_summary["p95_classifier_ms"]) < 500,
        },
    }

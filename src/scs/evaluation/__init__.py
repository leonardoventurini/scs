"""Search-quality evaluation contracts and metrics."""

from scs.evaluation.search import (
    EvaluationCase,
    EvaluationSuite,
    RelevanceJudgment,
    SearchEvaluationReport,
    load_evaluation_suite,
    run_search_evaluation,
)

__all__ = [
    "EvaluationCase",
    "EvaluationSuite",
    "RelevanceJudgment",
    "SearchEvaluationReport",
    "load_evaluation_suite",
    "run_search_evaluation",
]

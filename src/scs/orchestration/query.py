"""One classifier decision followed by a closed, bounded code-query playbook."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import ClassVar, Literal, TypedDict, cast

from pydantic import ConfigDict, Field, model_validator

from scs.graph.models import NodeType
from scs.orchestration.decision import (
    DecisionProvider,
    DeterministicDecisionProvider,
    Playbook,
    RoutingDecision,
    RoutingRequest,
    SourcePosition,
)
from scs.source_paths import validated_source_path

QueryMode = Literal["fast", "balanced", "thorough"]
ServiceCall = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class ModeBudget:
    """Fixed per-mode ceilings that a classifier cannot change."""

    classifier_seconds: float
    search_seeds: int
    graph_depth: int
    hydrated_files: int
    total_seconds: float


MODE_BUDGETS: dict[QueryMode, ModeBudget] = {
    "fast": ModeBudget(0.15, 5, 1, 1, 1.0),
    "balanced": ModeBudget(0.5, 10, 2, 3, 7.0),
    "thorough": ModeBudget(1.0, 20, 3, 5, 35.0),
}


class QueryRequest(RoutingRequest):
    """Validated public request, including repository-contained anchor paths."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    repo_path: str = Field(min_length=1, max_length=2048)
    mode: QueryMode = "balanced"
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _normalize_anchors(self) -> "QueryRequest":
        root = Path(self.repo_path).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("repo_path must be an existing absolute directory")
        root = root.resolve()
        self.repo_path = str(root)
        if self.node_type is not None:
            NodeType(self.node_type)
        self.node_ids: list[str] = list(dict.fromkeys(self.node_ids))
        if any(not identity or len(identity) > 256 for identity in self.node_ids):
            raise ValueError("node_ids must contain bounded non-empty identities")
        self.file_paths: list[str] = list(
            dict.fromkeys(self._relative_path(path, root) for path in self.file_paths)
        )
        if self.source_position is not None:
            self.source_position: SourcePosition | None = SourcePosition(
                file_path=self._relative_path(
                    self.source_position.file_path, root, require_file=True
                ),
                line=self.source_position.line,
            )
        return self

    @staticmethod
    def _relative_path(path: str, root: Path, *, require_file: bool = False) -> str:
        source = validated_source_path(path, str(root), require_file=require_file)
        return Path(source).relative_to(root).as_posix()

    def routing_request(self) -> RoutingRequest:
        """Expose only bounded caller data, without repository source or index data."""

        return RoutingRequest(
            goal=self.goal,
            node_type=self.node_type,
            symbol_name=self.symbol_name,
            node_ids=self.node_ids,
            file_paths=self.file_paths,
            source_position=self.source_position,
        )


class QueryCodeOutput(TypedDict):
    """Stable response envelope shared by all seven playbooks."""

    goal: str
    repo_path: str
    routing: dict[str, object]
    evidence: dict[str, list[dict[str, object]]]
    trace: list[dict[str, object]]
    complete: bool
    truncated: bool
    degraded_stages: list[str]
    timings: dict[str, float]


def _node_evidence(
    node: dict[str, object], *, kind: str, stage: str
) -> dict[str, object]:
    metadata = node.get("metadata")
    attributes = cast(dict[str, object], metadata) if isinstance(metadata, dict) else {}
    return {
        "kind": kind,
        "id": str(node.get("id", "")),
        "name": str(node.get("name", "")),
        "node_type": str(node.get("type", "")),
        "file_path": node.get("file_path", attributes.get("file_path")),
        "start_line": node.get("start_line", attributes.get("start_line")),
        "end_line": node.get("end_line", attributes.get("end_line")),
        "stage": stage,
    }


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        cast(dict[str, object], item)
        for item in cast(list[object], value)
        if isinstance(item, dict)
    ]


class QueryOrchestrator:
    """Run exactly one selected fixed playbook over finite service routes."""

    def __init__(
        self,
        *,
        provider: DecisionProvider,
        call: ServiceCall,
        classifier_timeout_seconds: float | None = None,
    ) -> None:
        self._provider: DecisionProvider = provider
        self._call: ServiceCall = call
        self._classifier_timeout: float | None = classifier_timeout_seconds
        self._fallback: DeterministicDecisionProvider = DeterministicDecisionProvider()

    async def query(self, params: dict[str, object]) -> QueryCodeOutput:
        request = QueryRequest.model_validate(params)
        budget = MODE_BUDGETS[request.mode]
        started = perf_counter()
        classification_started = perf_counter()
        degraded_reason: str | None = None
        try:
            decision = await asyncio.wait_for(
                self._provider.classify(request.routing_request()),
                timeout=min(
                    budget.classifier_seconds,
                    self._classifier_timeout or budget.classifier_seconds,
                ),
            )
            # A custom provider still has to cross the same strict boundary.
            decision = RoutingDecision.model_validate(decision)
        except Exception as error:
            decision = await self._fallback.classify(request.routing_request())
            degraded_reason = "classifier_timeout" if isinstance(error, TimeoutError) else "classifier_unavailable"
        classification_ms = (perf_counter() - classification_started) * 1_000
        selected = decision.playbook
        if not self._eligible(selected, request):
            selected = Playbook.DISCOVER
            degraded_reason = "ineligible_playbook"

        evidence: dict[str, list[dict[str, object]]] = {
            key: [] for key in ("symbols", "files", "relationships", "references", "test_targets")
        }
        trace: list[dict[str, object]] = []
        degraded_stages: list[str] = []
        truncated = False

        async def stage(
            name: str, method: str, arguments: dict[str, object]
        ) -> dict[str, object]:
            nonlocal truncated
            stage_started = perf_counter()
            try:
                response = await self._call(method, arguments)
                reason = response.get("degraded_reason")
                timed_out = response.get("timed_out") is True
                was_truncated = any(
                    response.get(key) is True
                    for key in (
                        "nodes_truncated", "edges_truncated",
                        "dependents_truncated", "test_targets_truncated",
                    )
                )
                truncated = truncated or was_truncated or timed_out
                if reason or timed_out:
                    degraded_stages.append(name)
                trace.append(
                    {
                        "stage": name,
                        "status": "degraded" if reason or timed_out else "ok",
                        "counts": {
                            key: len(cast(list[object], value))
                            for key, value in response.items()
                            if isinstance(value, list)
                        },
                        "truncated": was_truncated or timed_out,
                        "elapsed_ms": (perf_counter() - stage_started) * 1_000,
                    }
                )
                return response
            except Exception as error:
                degraded_stages.append(name)
                trace.append(
                    {
                        "stage": name,
                        "status": "timeout" if isinstance(error, TimeoutError) else "failed",
                        "counts": {},
                        "truncated": True,
                        "elapsed_ms": (perf_counter() - stage_started) * 1_000,
                    }
                )
                truncated = True
                return {}

        async def search() -> list[dict[str, object]]:
            response = await stage(
                "search",
                "knowledge.search",
                {
                    "query": request.goal,
                    "node_type": request.node_type,
                    "repo_path": request.repo_path,
                    "limit": min(request.limit, budget.search_seeds),
                    "search_mode": request.mode,
                    "result_detail": "compact",
                },
            )
            nodes = _objects(response.get("results"))
            self._append_nodes(evidence, nodes, "search")
            return nodes

        async def related(*, incoming: bool = False) -> None:
            identities = request.node_ids[:1]
            symbol = request.symbol_name if not identities else None
            if not identities and symbol is None:
                seeds = await search()
                identities = [str(seed.get("id", "")) for seed in seeds[:1] if seed.get("id")]
            if not identities and symbol is None:
                return
            response = await stage(
                "references" if incoming else "traversal",
                "knowledge.related",
                {
                    "node_id": identities[0] if identities else None,
                    "symbol_name": symbol,
                    "repo_path": request.repo_path,
                    "depth": budget.graph_depth,
                    "direction": "incoming" if incoming else "both",
                    "relationship": "references" if incoming else None,
                },
            )
            self._append_nodes(evidence, _objects(response.get("matches")), "traversal")
            for item in _objects(response.get("related")):
                raw_node = item.get("node")
                if isinstance(raw_node, dict):
                    node = cast(dict[str, object], raw_node)
                    self._append_nodes(evidence, [node], "traversal")
                else:
                    node = item
                relationship = {
                    "kind": "reference" if incoming else "relationship",
                    "id": str(item.get("id", node.get("id", ""))),
                    "seed_id": item.get("seed_id", identities[0] if identities else None),
                    "target_id": node.get("id"),
                    "relationship": item.get("relationship", "references" if incoming else None),
                    "direction": item.get("direction", "incoming" if incoming else "both"),
                    "file_path": node.get("file_path"),
                    "start_line": node.get("start_line"),
                    "end_line": node.get("end_line"),
                    "stage": "references" if incoming else "traversal",
                }
                evidence["references" if incoming else "relationships"].append(relationship)

        try:
            async with asyncio.timeout(budget.total_seconds):
                match selected:
                    case Playbook.DISCOVER:
                        await search()
                    case Playbook.UNDERSTAND:
                        await related()
                    case Playbook.RELATIONSHIPS:
                        await related()
                    case Playbook.REFERENCES:
                        if request.source_position is not None:
                            position = request.source_position
                            response = await stage(
                                "references", "lsp.references",
                                {
                                    "file_path": str(Path(request.repo_path) / position.file_path),
                                    "line": position.line,
                                },
                            )
                            raw_symbol = response.get("symbol")
                            if isinstance(raw_symbol, dict):
                                self._append_nodes(
                                    evidence, [cast(dict[str, object], raw_symbol)],
                                    "references",
                                )
                            self._append_nodes(
                                evidence, _objects(response.get("references")), "references",
                                category="references",
                            )
                        else:
                            await related(incoming=True)
                    case Playbook.INSPECT_FILES:
                        files = request.file_paths
                        if not files:
                            seeds = await search()
                            files = [
                                str(path) for seed in seeds
                                if (path := self._file_path(seed)) is not None
                            ]
                        unique_files = list(dict.fromkeys(files))
                        truncated = truncated or len(unique_files) > budget.hydrated_files
                        for file_path in unique_files[: budget.hydrated_files]:
                            response = await stage(
                                "inspect", "knowledge.inspect_file",
                                {
                                    "repo_path": request.repo_path,
                                    "file_path": file_path,
                                    "node_limit": min(request.limit, 50),
                                    "edge_limit": min(request.limit * 2, 100),
                                },
                            )
                            evidence["files"].append(
                                {"kind": "file", "file_path": file_path, "stage": "inspect"}
                            )
                            self._append_nodes(evidence, _objects(response.get("nodes")), "inspect")
                            raw_edges = response.get("edges")
                            edge_groups = (
                                cast(dict[str, object], raw_edges).values()
                                if isinstance(raw_edges, dict)
                                else ()
                            )
                            for edges in edge_groups:
                                for edge in _objects(edges):
                                    evidence["relationships"].append(
                                        {"kind": "relationship", "id": edge.get("id"),
                                         "source_id": edge.get("source_id"),
                                         "target_id": edge.get("target_id"),
                                         "relationship": edge.get("relationship"),
                                         "stage": "inspect"}
                                    )
                    case Playbook.IMPACT:
                        response = await stage(
                            "impact", "knowledge.composite.regression_risk",
                            {"repo_path": request.repo_path,
                             "file_paths": request.file_paths,
                             "dependent_limit": min(request.limit, 50),
                             "test_target_limit": min(request.limit, 50)},
                        )
                        self._append_nodes(evidence, _objects(response.get("dependents")), "impact")
                        for target in _objects(response.get("test_targets")):
                            evidence["test_targets"].append(
                                {"kind": "test_target", "file_path": target.get("file_path"),
                                 "evidence": target.get("evidence", []), "stage": "impact"}
                            )
                    case Playbook.INVENTORY:
                        response = await stage(
                            "inventory", "knowledge.nodes.list",
                            {"repo_path": request.repo_path,
                             "node_type": request.node_type or NodeType.FUNCTION.value,
                             "limit": request.limit, "offset": 0},
                        )
                        self._append_nodes(evidence, _objects(response.get("nodes")), "inventory")
                        raw_total = response.get("total")
                        total = raw_total if isinstance(raw_total, int) else 0
                        truncated = truncated or total > len(evidence["symbols"])
        except TimeoutError:
            degraded_stages.append("execution")
            truncated = True
            trace.append({"stage": "execution", "status": "timeout", "counts": {},
                          "truncated": True, "elapsed_ms": (perf_counter() - started) * 1_000})

        for category, items in evidence.items():
            evidence[category] = list(
                {self._evidence_key(item): item for item in items}.values()
            )
        total_ms = (perf_counter() - started) * 1_000
        return {
            "goal": request.goal,
            "repo_path": request.repo_path,
            "routing": {
                "requested_mode": request.mode,
                "playbook": selected.value,
                "provider": "laya" if decision.model.startswith("receptron/") else "deterministic",
                "model": decision.model,
                "confidence": decision.confidence,
                "probabilities": {key.value: value for key, value in decision.probabilities.items()},
                "fallback_applied": degraded_reason is not None,
                "degraded_reason": degraded_reason,
            },
            "evidence": evidence,
            "trace": trace,
            "complete": not truncated and not degraded_stages and degraded_reason is None,
            "truncated": truncated,
            "degraded_stages": degraded_stages,
            "timings": {
                "classification_ms": classification_ms,
                "execution_ms": total_ms - classification_ms,
                "total_ms": total_ms,
            },
        }

    @staticmethod
    def _eligible(playbook: Playbook, request: QueryRequest) -> bool:
        if playbook is Playbook.IMPACT:
            return bool(request.file_paths)
        if playbook is Playbook.REFERENCES:
            return bool(request.source_position or request.node_ids or request.symbol_name)
        if playbook is Playbook.INVENTORY and request.node_type is not None:
            return request.node_type in {
                NodeType.CLASS.value, NodeType.FUNCTION.value, NodeType.METHOD.value,
                NodeType.VARIABLE.value, NodeType.CONSTANT.value, NodeType.TYPE_ALIAS.value,
            }
        return True

    @staticmethod
    def _file_path(node: dict[str, object]) -> object | None:
        metadata = node.get("metadata")
        if node.get("file_path"):
            return node["file_path"]
        return cast(dict[str, object], metadata).get("file_path") if isinstance(metadata, dict) else None

    @staticmethod
    def _append_nodes(
        evidence: dict[str, list[dict[str, object]]],
        nodes: list[dict[str, object]],
        stage: str,
        *,
        category: str = "symbols",
    ) -> None:
        for node in nodes:
            if category == "symbols" and node.get("type") == NodeType.FILE.value:
                evidence["files"].append(
                    {"kind": "file", "id": node.get("id"),
                     "file_path": QueryOrchestrator._file_path(node), "stage": stage}
                )
                continue
            projected = _node_evidence(node, kind="reference" if category == "references" else "symbol", stage=stage)
            evidence[category].append(projected)

    @staticmethod
    def _evidence_key(item: dict[str, object]) -> tuple[str, str, str]:
        return (
            str(item.get("kind", "")),
            str(item.get("id", item.get("file_path", ""))),
            str(item.get("target_id", item.get("file_path", ""))),
        )

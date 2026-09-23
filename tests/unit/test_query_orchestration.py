"""Every model choice maps to one fixed, bounded service playbook."""

from __future__ import annotations

from pathlib import Path

import pytest

from scs.orchestration.decision import Playbook, RoutingDecision, RoutingRequest
from scs.orchestration.query import QueryOrchestrator


class FixedProvider:
    def __init__(self, playbook: Playbook) -> None:
        self.playbook = playbook
        self.calls = 0

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        self.calls += 1
        assert "repository bytes" not in request.model_dump_json()
        return RoutingDecision(
            playbook=self.playbook,
            model="test-provider",
            confidence=1.0,
            probabilities={
                label: float(label is self.playbook) for label in Playbook
            },
        )


class FakeRoutes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        node = {
            "id": "node-1", "type": "function", "name": "parse",
            "file_path": "parser.py", "start_line": 1, "end_line": 3,
        }
        match method:
            case "knowledge.search":
                return {"results": [node], "timed_out": False, "degraded_reason": None}
            case "knowledge.related":
                return {"matches": [node], "related": []}
            case "knowledge.inspect_file":
                return {"file_path": "parser.py", "nodes": [node], "edges": {},
                        "nodes_truncated": False, "edges_truncated": False}
            case "knowledge.nodes.list":
                return {"nodes": [node], "total": 1}
            case "knowledge.composite.regression_risk":
                return {"dependents": [node], "test_targets": [], "complete": True,
                        "dependents_truncated": False, "test_targets_truncated": False}
            case "lsp.references":
                return {"available": True, "symbol": node, "references": [node]}
            case _:
                raise AssertionError(f"unrecognized service route: {method}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("playbook", "anchors", "expected_route"),
    [
        (Playbook.DISCOVER, {}, "knowledge.search"),
        (Playbook.UNDERSTAND, {}, "knowledge.related"),
        (Playbook.RELATIONSHIPS, {"node_ids": ["node-1"]}, "knowledge.related"),
        (Playbook.REFERENCES, {"node_ids": ["node-1"]}, "knowledge.related"),
        (Playbook.INSPECT_FILES, {"file_paths": ["parser.py"]}, "knowledge.inspect_file"),
        (Playbook.IMPACT, {"file_paths": ["parser.py"]}, "knowledge.composite.regression_risk"),
        (Playbook.INVENTORY, {}, "knowledge.nodes.list"),
    ],
)
async def test_each_playbook_uses_only_its_fixed_routes(
    tmp_path: Path,
    playbook: Playbook,
    anchors: dict[str, object],
    expected_route: str,
) -> None:
    (tmp_path / "parser.py").write_text("def parse(): pass\n")
    provider = FixedProvider(playbook)
    routes = FakeRoutes()
    query = QueryOrchestrator(provider=provider, call=routes.call)

    result = await query.query({"goal": "find parser", "repo_path": str(tmp_path), **anchors})

    assert result["routing"]["playbook"] == playbook.value
    assert provider.calls == 1
    assert expected_route in [method for method, _params in routes.calls]
    assert result["trace"]
    assert set(result["evidence"]) == {
        "symbols", "files", "relationships", "references", "test_targets"
    }


@pytest.mark.asyncio
async def test_ineligible_and_failed_classifier_fall_back_once(tmp_path: Path) -> None:
    provider = FixedProvider(Playbook.IMPACT)
    routes = FakeRoutes()
    query = QueryOrchestrator(provider=provider, call=routes.call)

    result = await query.query({"goal": "find parser", "repo_path": str(tmp_path)})

    assert result["routing"]["playbook"] == Playbook.DISCOVER.value
    assert result["routing"]["degraded_reason"] == "ineligible_playbook"
    assert provider.calls == 1

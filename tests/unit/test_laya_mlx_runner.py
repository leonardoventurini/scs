"""Native Laya decisions cross a strict boundary and warm before serving."""

from __future__ import annotations

from pathlib import Path

import pytest

from scs.orchestration import laya_runner
from scs.orchestration.decision import Playbook, RoutingRequest


def prediction(choice: str = "IMPACT") -> dict[str, object]:
    probabilities = {
        playbook.value: 1.0 if playbook.value == choice else 0.0
        for playbook in Playbook
    }
    return {
        "answers": {
            "playbook": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": probabilities,
            }
        }
    }


def test_mlx_worker_warms_once_before_classifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.states: list[dict[str, object]] = []

        def predict(
            self, state: dict[str, object], questions: dict[str, object]
        ) -> dict[str, object]:
            self.states.append(state)
            assert set(questions) == {"playbook"}
            return prediction()

    agent = FakeAgent()
    monkeypatch.setattr(laya_runner, "verify_bundle", lambda _directory: True)
    monkeypatch.setattr(laya_runner, "_load_agent", lambda _directory: agent)

    runtime = laya_runner.LayaRuntime(tmp_path)
    assert len(agent.states) == 1
    assert set(agent.states[0]) <= {
        "goal", "node_type", "symbol_name", "node_ids", "file_paths", "source_position"
    }

    result = runtime.classify(
        RoutingRequest(goal="Which tests depend on this?", file_paths=["src/scs/main.py"])
    )

    assert result.playbook is Playbook.IMPACT
    assert len(agent.states) == 2
    assert agent.states[1]["file_paths"] == ["src/scs/main.py"]


@pytest.mark.parametrize("invalid", [
    prediction("UNKNOWN"),
    {"answers": {"playbook": {"type": "choice", "choice": "IMPACT", "confidence": 0.9,
                              "probabilities": {"IMPACT": 1.0}}}},
    {"answers": {"playbook": {"type": "choice", "choice": "IMPACT", "confidence": float("nan"),
                              "probabilities": {label.value: float(label is Playbook.IMPACT)
                                                for label in Playbook}}}},
])
def test_mlx_worker_rejects_invalid_model_output(invalid: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        laya_runner._decision_from_prediction(invalid)

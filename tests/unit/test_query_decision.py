"""Strict routing contracts keep model output outside route authority."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from scs.orchestration.decision import (
    DeterministicDecisionProvider,
    Playbook,
    RoutingDecision,
    RoutingRequest,
    eligible_playbooks,
)


@pytest.mark.parametrize(
    ("anchors", "unavailable"),
    [
        ({}, {Playbook.IMPACT, Playbook.REFERENCES}),
        ({"file_paths": ["parser.py"]}, {Playbook.REFERENCES}),
        ({"symbol_name": "parse"}, {Playbook.IMPACT}),
        ({"node_ids": ["node"]}, {Playbook.IMPACT}),
        ({"source_position": {"file_path": "parser.py", "line": 0}}, {Playbook.IMPACT}),
        ({"node_type": "file"}, {Playbook.IMPACT, Playbook.REFERENCES, Playbook.INVENTORY}),
        ({"file_paths": ["parser.py"], "symbol_name": "parse"}, set()),
    ],
)
def test_eligibility_requires_real_anchors(
    anchors: dict[str, object], unavailable: set[Playbook]
) -> None:
    request = RoutingRequest.model_validate({"goal": "Investigate parser", **anchors})

    assert eligible_playbooks(request) == frozenset(Playbook) - unavailable


def test_routing_decision_requires_exact_finite_probability_vector() -> None:
    probabilities = {playbook: 1 / len(Playbook) for playbook in Playbook}
    decision = RoutingDecision(
        playbook=Playbook.DISCOVER,
        model="fixture",
        confidence=0.5,
        probabilities=probabilities,
    )

    assert decision.playbook is Playbook.DISCOVER
    for invalid in (
        {**probabilities, Playbook.IMPACT: math.nan},
        {key: value for key, value in probabilities.items() if key != Playbook.IMPACT},
    ):
        with pytest.raises(ValidationError):
            RoutingDecision(
                playbook=Playbook.DISCOVER,
                model="fixture",
                confidence=0.5,
                probabilities=invalid,
            )


def test_routing_models_reject_unknown_fields_and_playbooks() -> None:
    with pytest.raises(ValidationError):
        RoutingRequest.model_validate({"goal": "find parser", "unknown": "route"})
    with pytest.raises(ValidationError):
        RoutingDecision.model_validate(
            {
                "playbook": "SHELL",
                "model": "fixture",
                "confidence": 1.0,
                "probabilities": {},
            }
        )


@pytest.mark.asyncio
async def test_deterministic_provider_selects_discovery() -> None:
    decision = await DeterministicDecisionProvider().classify(
        RoutingRequest(goal="find parser")
    )

    assert decision.playbook is Playbook.DISCOVER
    assert decision.probabilities[Playbook.DISCOVER] == 1.0

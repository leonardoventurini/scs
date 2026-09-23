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
)


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

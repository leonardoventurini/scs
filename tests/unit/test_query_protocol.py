"""Private runner messages reject malformed or unbounded model output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scs.orchestration.decision import DeterministicDecisionProvider, RoutingRequest
from scs.orchestration.protocol import RunnerRequest, RunnerResponse


@pytest.mark.asyncio
async def test_runner_response_requires_one_outcome_and_exact_identity() -> None:
    decision = await DeterministicDecisionProvider().classify(
        RoutingRequest(goal="find parser")
    )
    identity = "a" * 32

    assert RunnerResponse(request_id=identity, decision=decision).decision == decision
    for invalid in (
        {"request_id": identity},
        {"request_id": identity, "decision": decision, "error": "failed"},
        {"request_id": "wrong", "decision": decision},
        {"request_id": identity, "decision": decision, "unknown": "route"},
    ):
        with pytest.raises(ValidationError):
            RunnerResponse.model_validate(invalid)


def test_runner_request_rejects_oversized_goal_and_extra_fields() -> None:
    identity = "b" * 32
    for routing in (
        {"goal": "x" * 4097},
        {"goal": "find parser", "source": "repository bytes"},
    ):
        with pytest.raises(ValidationError):
            RunnerRequest.model_validate({"request_id": identity, "routing": routing})

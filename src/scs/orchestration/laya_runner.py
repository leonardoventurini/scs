"""Private offline MLX worker; stdout carries protocol messages only."""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

from scs.orchestration.bundle import (
    MODEL_DIGEST,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    verify_bundle,
)
from scs.orchestration.decision import (
    PLAYBOOK_DESCRIPTIONS,
    Playbook,
    RoutingDecision,
    RoutingRequest,
)
from scs.orchestration.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    QUESTION_SCHEMA_VERSION,
    RunnerHandshake,
    RunnerRequest,
    RunnerResponse,
)

QUESTION = "Select the single best code investigation workflow for this goal."
QUESTIONS: dict[str, object] = {
    "playbook": {
        "type": "choice",
        "instructions": QUESTION,
        "criteria": {
            playbook.value: PLAYBOOK_DESCRIPTIONS[playbook]
            for playbook in Playbook
        },
    },
}
WARMUP_GOAL = "Warm the local code-query classifier."


class LayaAgent(Protocol):
    """Only the private model method needed by this worker."""

    def predict(self, state: dict[str, object], questions: dict[str, object]) -> object: ...


def _load_agent(directory: Path) -> LayaAgent:
    """Import the optional dependency only after the verified bundle is present."""

    from laya_mlx import load

    return load(str(directory), dtype="float16", device="gpu")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Laya {label} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise ValueError(f"Laya {label} must have string keys")
    return cast(Mapping[str, object], items)


def _probability(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"Laya {label} must be numeric")
    return float(cast(int | float, value))


def _decision_from_prediction(prediction: object) -> RoutingDecision:
    """Treat library output as untrusted before it crosses the subprocess pipe."""

    response = _mapping(prediction, "prediction")
    answers = _mapping(response.get("answers"), "answers")
    answer = _mapping(answers.get("playbook"), "playbook")
    if answer.get("type") != "choice":
        raise ValueError("Laya playbook answer is not a choice")
    selected = answer.get("choice")
    if not isinstance(selected, str):
        raise ValueError("Laya playbook is not a string")
    raw_probabilities = _mapping(answer.get("probabilities"), "probabilities")
    if set(raw_probabilities) != {playbook.value for playbook in Playbook}:
        raise ValueError("Laya probabilities must cover every playbook")
    probabilities = {
        playbook: _probability(raw_probabilities[playbook.value], playbook.value)
        for playbook in Playbook
    }
    return RoutingDecision(
        playbook=Playbook(selected),
        model=f"{MODEL_REPOSITORY}@{MODEL_REVISION}",
        confidence=_probability(answer.get("confidence"), "confidence"),
        probabilities=probabilities,
    )


class LayaRuntime:
    """Keep one verified GPU model loaded and ready for bounded decisions."""

    def __init__(self, directory: Path) -> None:
        if not verify_bundle(directory):
            raise ValueError("Laya bundle is absent or failed checksum verification")
        self._agent: LayaAgent = _load_agent(directory)

        # MLX specializes GPU kernels on first use. Warm before the handshake
        # so a ready worker can meet the 150 ms fast-mode classifier deadline.
        self._agent.predict(
            RoutingRequest(goal=WARMUP_GOAL).model_dump(exclude_none=True),
            QUESTIONS,
        )

    def classify(self, request: RoutingRequest) -> RoutingDecision:
        prediction = self._agent.predict(request.model_dump(exclude_none=True), QUESTIONS)
        return _decision_from_prediction(prediction)


def main() -> int:
    """Serve bounded lines without logging goals, anchors, or model output."""

    parser = argparse.ArgumentParser()
    parser.add_argument("model_directory", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    runtime = LayaRuntime(cast(Path, args.model_directory))
    handshake = RunnerHandshake(
        protocol_version=PROTOCOL_VERSION,
        model_repository=MODEL_REPOSITORY,
        model_revision=MODEL_REVISION,
        model_digest=MODEL_DIGEST,
        runtime_version=version("laya-mlx"),
        question_schema_version=QUESTION_SCHEMA_VERSION,
    )
    output_lock = threading.Lock()

    def publish(response: RunnerResponse) -> None:
        with output_lock:
            sys.stdout.write(response.model_dump_json() + "\n")
            sys.stdout.flush()

    def completed(future: Future[RoutingDecision], request_id: str) -> None:
        try:
            response = RunnerResponse(request_id=request_id, decision=future.result())
        except Exception:
            response = RunnerResponse(request_id=request_id, error="classification_failed")
        publish(response)

    sys.stdout.write(handshake.model_dump_json() + "\n")
    sys.stdout.flush()
    with ThreadPoolExecutor(max_workers=max(1, min(cast(int, args.workers), 4))) as executor:
        while line := sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1):
            if len(line) > MAX_MESSAGE_BYTES:
                return 2
            try:
                request = RunnerRequest.model_validate_json(line)
            except Exception:
                return 2
            future = executor.submit(runtime.classify, request.routing)
            future.add_done_callback(
                lambda finished, identity=request.request_id: completed(finished, identity)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

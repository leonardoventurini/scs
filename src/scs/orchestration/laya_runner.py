"""Private offline ONNX worker; stdout carries protocol messages only."""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar, cast

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from pydantic import BaseModel, ConfigDict, Field

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
MASK_TOKEN = "[MASK]"


class LayaConfig(BaseModel):
    """The small trusted metadata file shipped with the pinned model."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", strict=True)

    max_len: int = Field(ge=1, le=4096)
    head_max_len: int = Field(ge=1, le=1024)
    temperature_by_options: dict[str, float]


def _sequence(
    tokenizer: Tokenizer,
    request: RoutingRequest,
    *,
    max_len: int,
    head_max_len: int,
) -> tuple[list[int], list[int]]:
    """Render Laya's choice question with fixed labels and bounded state."""

    def token_id(token: str) -> int:
        value = tokenizer.token_to_id(token)
        if value is None:
            raise ValueError("Laya tokenizer lacks a required special token")
        return value

    def encode(value: str) -> list[int]:
        return tokenizer.encode(value.replace(MASK_TOKEN, " "), add_special_tokens=False).ids

    options = [
        f"{playbook.value}: {PLAYBOOK_DESCRIPTIONS[playbook]}"
        for playbook in Playbook
    ]
    option_ids = [
        [token_id(MASK_TOKEN), *encode(" " + option)[:48]]
        for option in options
    ]
    option_budget = head_max_len - sum(len(option) for option in option_ids)
    if option_budget < 16:
        per_option = max(4, (head_max_len - 16) // len(option_ids))
        option_ids = [option[:per_option] for option in option_ids]
        option_budget = head_max_len - sum(len(option) for option in option_ids)
    header = encode(f"choice question: {QUESTION}")[: max(8, option_budget)]
    sequence = [token_id("[CLS]"), *header, token_id("[SEP]")]
    markers: list[int] = []
    for option in option_ids:
        markers.append(len(sequence))
        sequence.extend(option)
    sequence.append(token_id("[SEP]"))
    state = json.dumps(request.model_dump(exclude_none=True), ensure_ascii=False)
    sequence.extend(encode(state)[: max(0, max_len - len(sequence) - 1)])
    sequence.append(token_id("[SEP]"))
    if len(markers) != len(Playbook) or any(marker >= max_len for marker in markers):
        raise ValueError("Laya question exceeds the pinned head budget")
    return sequence[:max_len], markers


class LayaRuntime:
    """One local model session reused by all child-process requests."""

    def __init__(self, directory: Path) -> None:
        if not verify_bundle(directory):
            raise ValueError("Laya bundle is absent or failed checksum verification")
        config = LayaConfig.model_validate_json((directory / "laya_config.json").read_text())
        self._max_len: int = config.max_len
        self._head_max_len: int = config.head_max_len
        self._temperature: float = config.temperature_by_options["choice:6-10"]
        self._tokenizer: Tokenizer = Tokenizer.from_file(str(directory / "tokenizer/tokenizer.json"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self._session: ort.InferenceSession = ort.InferenceSession(
            str(directory / "laya.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def classify(self, request: RoutingRequest) -> RoutingDecision:
        sequence, markers = _sequence(
            self._tokenizer,
            request,
            max_len=self._max_len,
            head_max_len=self._head_max_len,
        )
        result = self._session.run(
            ["logits"],
            {
                "input_ids": np.asarray([sequence], dtype=np.int64),
                "attention_mask": np.ones((1, len(sequence)), dtype=np.int64),
                "marker_pos": np.asarray([markers], dtype=np.int64),
                "marker_mask": np.ones((1, len(markers)), dtype=np.bool_),
                "qtype": np.asarray([0], dtype=np.int64),
            },
        )
        logits = np.asarray(result[0], dtype=np.float32).reshape(-1)
        scores = [
            value / self._temperature
            for value in cast(list[float], logits[: len(Playbook)].tolist())
        ]
        maximum = max(scores)
        weights = [math.exp(score - maximum) for score in scores]
        denominator = sum(weights)
        probabilities = {
            playbook: weight / denominator
            for playbook, weight in zip(Playbook, weights, strict=True)
        }
        selected = max(Playbook, key=lambda playbook: probabilities[playbook])
        entropy = -sum(
            probability * math.log(max(probability, 1e-12))
            for probability in probabilities.values()
        )
        return RoutingDecision(
            playbook=selected,
            model=f"{MODEL_REPOSITORY}@{MODEL_REVISION}",
            confidence=max(0.0, min(1.0, 1.0 - entropy / math.log(len(Playbook)))),
            probabilities=probabilities,
        )


def main() -> int:
    """Serve bounded lines; never print goals or anchors to logs."""

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
        runtime_version=ort.__version__,
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

"""Strict, provider-neutral contracts for one code-query routing decision."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Playbook(StrEnum):
    """The only workflows a classifier may select."""

    DISCOVER = "DISCOVER"
    UNDERSTAND = "UNDERSTAND"
    RELATIONSHIPS = "RELATIONSHIPS"
    REFERENCES = "REFERENCES"
    INSPECT_FILES = "INSPECT_FILES"
    IMPACT = "IMPACT"
    INVENTORY = "INVENTORY"


PLAYBOOK_DESCRIPTIONS: dict[Playbook, str] = {
    Playbook.DISCOVER: "Find relevant code symbols or files for a broad goal.",
    Playbook.UNDERSTAND: "Explain how code works using search and graph context.",
    Playbook.RELATIONSHIPS: "Find dependencies and relationships of a symbol.",
    Playbook.REFERENCES: "Find incoming references to a symbol or source position.",
    Playbook.INSPECT_FILES: "Inspect indexed symbols and edges in source files.",
    Playbook.IMPACT: "Find affected dependents and tests for changed files.",
    Playbook.INVENTORY: "List indexed symbols of a specified type.",
}


class SourcePosition(BaseModel):
    """A zero-based location supplied by the caller."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    file_path: str = Field(min_length=1, max_length=2048)
    line: int = Field(ge=0)


class RoutingRequest(BaseModel):
    """Bounded goal and explicit anchors; contains no retrieved source data."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1, max_length=4096)
    node_type: str | None = Field(default=None, max_length=64)
    symbol_name: str | None = Field(default=None, max_length=256)
    node_ids: list[str] = Field(default_factory=list, max_length=20)
    file_paths: list[str] = Field(default_factory=list, max_length=20)
    source_position: SourcePosition | None = None


class RoutingDecision(BaseModel):
    """One complete, finite probability vector from a decision provider."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    playbook: Playbook
    model: str = Field(min_length=1, max_length=256)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[Playbook, float]

    @model_validator(mode="after")
    def _complete_probabilities(self) -> "RoutingDecision":
        if set(self.probabilities) != set(Playbook):
            raise ValueError("probabilities must cover every playbook exactly once")
        if any(not 0 <= value <= 1 or not _finite(value) for value in self.probabilities.values()):
            raise ValueError("probabilities must be finite values between zero and one")
        if abs(sum(self.probabilities.values()) - 1.0) > 0.01:
            raise ValueError("probabilities must sum to one")
        return self


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


class DecisionProvider(Protocol):
    """Classify a bounded request once without reading index evidence."""

    async def classify(self, request: RoutingRequest) -> RoutingDecision: ...


class DeterministicDecisionProvider:
    """Fail-open and test provider selecting the conservative discovery path."""

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        del request
        return RoutingDecision(
            playbook=Playbook.DISCOVER,
            model="deterministic-v1",
            confidence=1.0,
            probabilities={
                playbook: float(playbook is Playbook.DISCOVER)
                for playbook in Playbook
            },
        )

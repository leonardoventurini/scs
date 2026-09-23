"""Bounded private pipe protocol for the optional Laya decision process."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scs.orchestration.decision import RoutingDecision, RoutingRequest

PROTOCOL_VERSION = 1
QUESTION_SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 16_384


class RunnerHandshake(BaseModel):
    """Immutable model identity sent once before classification requests."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal[1]
    model_repository: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(min_length=40, max_length=40)
    model_digest: str = Field(min_length=64, max_length=64)
    runtime_version: str = Field(min_length=1, max_length=64)
    question_schema_version: Literal[1]


class RunnerRequest(BaseModel):
    """One bounded classification request with a caller-generated identity."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    routing: RoutingRequest


class RunnerResponse(BaseModel):
    """A successful decision or bounded error for exactly one request ID."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    decision: RoutingDecision | None = None
    error: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _one_outcome(self) -> "RunnerResponse":
        if (self.decision is None) == (self.error is None):
            raise ValueError("runner response requires exactly one outcome")
        return self

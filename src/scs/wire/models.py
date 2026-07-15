"""Discriminated SCSWire request, response, event, and error models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from scs.models import PROTOCOL_VERSION, StrictModel


class ErrorCode(StrEnum):
    """Stable machine-readable SCSWire error categories."""

    BAD_REQUEST = "bad_request"
    UNKNOWN_METHOD = "unknown_method"
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class WireError(StrictModel):
    """Typed error body returned without leaking implementation exceptions."""

    code: ErrorCode
    message: str
    retryable: bool = False


class WireRequest(StrictModel):
    """One finite SCSWire request."""

    kind: Literal["request"] = "request"
    version: int = PROTOCOL_VERSION
    id: str
    method: str
    params: dict[str, object] = Field(default_factory=dict)


class WireResponse(StrictModel):
    """Successful finite SCSWire response."""

    kind: Literal["response"] = "response"
    version: int = PROTOCOL_VERSION
    id: str
    result: dict[str, object]


class WireErrorResponse(StrictModel):
    """Failed finite SCSWire response."""

    kind: Literal["error"] = "error"
    version: int = PROTOCOL_VERSION
    id: str
    error: WireError


class WireEvent(StrictModel):
    """Monotonic background-work event delivered to subscribers."""

    kind: Literal["event"] = "event"
    version: int = PROTOCOL_VERSION
    topic: str
    sequence: int = Field(ge=0)
    terminal: bool = False
    payload: dict[str, object] = Field(default_factory=dict)


WireEnvelope = Annotated[
    WireRequest | WireResponse | WireErrorResponse | WireEvent,
    Field(discriminator="kind"),
]


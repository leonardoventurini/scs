"""Durable public contracts shared by SCS service boundaries."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class StrictModel(BaseModel):
    """Base contract that is immutable while accepting additive fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)


class ProtocolRange(StrictModel):
    """Inclusive SCSWire version range supported by one peer."""

    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)

    def overlaps(self, other: "ProtocolRange") -> bool:
        """Return whether two peers share at least one protocol version."""

        return max(self.minimum, other.minimum) <= min(self.maximum, other.maximum)


class ServiceState(StrEnum):
    """Observable daemon readiness without conflating degraded and failed."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPING = "stopping"


class ServiceIdentity(StrictModel):
    """Generation-scoped identity for one independently restarting process."""

    service: str
    pid: int = Field(gt=0)
    start_time: str
    generation: str
    artifact_sha256: str
    protocol_min: int = Field(ge=1)
    protocol_max: int = Field(ge=1)


class RepositoryIndexState(StrEnum):
    """Repository state exposed to explicit indexing clients."""

    UNINDEXED = "unindexed"
    QUEUED = "queued"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"


class RepositoryStatus(StrictModel):
    """Current SCS-owned index status for one canonical repository path."""

    repo_path: str
    state: RepositoryIndexState
    file_count: int = Field(default=0, ge=0)
    last_indexed: str | None = None
    active_job_id: str | None = None

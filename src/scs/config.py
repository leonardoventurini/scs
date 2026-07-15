"""Typed configuration for the independent SCS daemon."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scs.paths import SCSPaths

DEFAULT_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
DEFAULT_EMBEDDING_DIMENSION = 768


class SCSSettings(BaseSettings):
    """Environment-backed SCS settings with no External product configuration dependency."""

    model_config = SettingsConfigDict(env_prefix="SCS_", extra="ignore")

    home: Path = Field(default_factory=lambda: Path.home() / ".scs")
    model_cache: Path = Field(default_factory=lambda: Path.home() / ".cache" / "scs" / "models")
    runtime_dir: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Application Support" / "SCS"
    )
    log_dir: Path = Field(default_factory=lambda: Path.home() / "Library" / "Logs" / "SCS")
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, gt=0)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_concurrency: int = Field(default=1, ge=1, le=4)
    openai_api_key: str | None = None
    summarizer_model: str = "gpt-4.1-mini"
    summarizer_timeout_seconds: float = Field(default=45.0, gt=0, le=300)

    @cached_property
    def paths(self) -> SCSPaths:
        """Return validated product paths without creating them."""

        return SCSPaths.resolve(
            self.home,
            model_cache=self.model_cache,
            runtime=self.runtime_dir,
            logs=self.log_dir,
        )

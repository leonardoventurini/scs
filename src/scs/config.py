"""Typed configuration for the independent SCS daemon."""

from __future__ import annotations

from functools import cached_property
from ipaddress import ip_address
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scs.paths import SCSPaths

DEFAULT_EMBEDDING_PROVIDER = "omlx"
DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-8B-4bit-DWQ"
DEFAULT_EMBEDDING_DIMENSION = 4096
DEFAULT_OMLX_BASE_URL = "http://127.0.0.1:10000/v1"
DEFAULT_MCP_INTERNAL_HOST = "127.0.0.1"
DEFAULT_MCP_INTERNAL_PORT = 28465


class SCSSettings(BaseSettings):
    """Environment-backed SCS settings with no External product configuration dependency."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="SCS_", extra="ignore"
    )

    home: Path = Field(default_factory=lambda: Path.home() / ".scs")
    model_cache: Path = Field(
        default_factory=lambda: Path.home() / ".cache" / "scs" / "models"
    )
    runtime_dir: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Application Support" / "SCS"
    )
    log_dir: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Logs" / "SCS"
    )
    embedding_provider: Literal["omlx", "mlx"] = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, gt=0)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_concurrency: int = Field(default=1, ge=1, le=4)
    omlx_base_url: str = DEFAULT_OMLX_BASE_URL
    mcp_internal_host: str = DEFAULT_MCP_INTERNAL_HOST
    mcp_internal_port: int = Field(default=DEFAULT_MCP_INTERNAL_PORT, ge=0, le=65535)
    index_text_fallback: bool = True
    index_max_file_bytes: int = Field(default=1_048_576, ge=1)
    index_text_sample_bytes: int = Field(default=8_192, ge=1)
    index_large_dir_files: int = Field(default=10_000, ge=1)
    index_large_dir_bytes: int = Field(default=536_870_912, ge=1)
    auto_reindex_enabled: bool = True
    auto_reindex_active_seconds: float = Field(default=2.0, gt=0)
    auto_reindex_idle_seconds: float = Field(default=30.0, gt=0)
    auto_reindex_debounce_seconds: float = Field(default=0.5, ge=0)
    auto_reindex_git_timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _validate_index_limits(self) -> "SCSSettings":
        """Keep bounded text detection within the accepted file envelope."""

        if self.index_text_sample_bytes > self.index_max_file_bytes:
            raise ValueError("index text sample cannot exceed maximum file size")
        if self.auto_reindex_idle_seconds < self.auto_reindex_active_seconds:
            raise ValueError(
                "automatic reindex idle interval cannot be shorter than active interval"
            )
        return self

    @field_validator("omlx_base_url")
    @classmethod
    def _validate_omlx_base_url(cls, value: str) -> str:
        """Keep source-derived embedding requests on the local machine."""

        parsed = urlsplit(value)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("omlx_base_url must be an absolute http URL")
        host = parsed.hostname.lower()
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("omlx_base_url must use a loopback host")
        return value.rstrip("/")

    @cached_property
    def paths(self) -> SCSPaths:
        """Return validated product paths without creating them."""

        return SCSPaths.resolve(
            self.home,
            model_cache=self.model_cache,
            runtime=self.runtime_dir,
            logs=self.log_dir,
        )

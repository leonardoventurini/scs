"""Typed configuration for the independent SCS daemon."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
from ipaddress import ip_address
from pathlib import Path
import tomllib
from typing import ClassVar, Literal, cast, override
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from scs.paths import SCSPaths

DEFAULT_EMBEDDING_PROVIDER = "openai"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSION = 1536
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LOCAL_EMBEDDING_MODEL = "Qwen3-Embedding-8B-4bit-DWQ"
DEFAULT_LOCAL_EMBEDDING_DIMENSION = 4096
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
    embedding_provider: Literal["openai", "omlx", "mlx"] = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_OPENAI_EMBEDDING_MODEL
    embedding_dimension: int = Field(default=DEFAULT_OPENAI_EMBEDDING_DIMENSION, gt=0)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_concurrency: int = Field(default=1, ge=1, le=4)
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
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

    @model_validator(mode="before")
    @classmethod
    def _select_provider_defaults(cls, values: object) -> object:
        """Keep model and dimension defaults coherent with provider selection."""

        if not isinstance(values, Mapping):
            return values
        configured = dict(cast(Mapping[str, object], values))
        provider = configured.get("embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
        if provider in {"omlx", "mlx"}:
            configured.setdefault("embedding_model", DEFAULT_LOCAL_EMBEDDING_MODEL)
            configured.setdefault(
                "embedding_dimension", DEFAULT_LOCAL_EMBEDDING_DIMENSION
            )
        return configured

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load the SCS-owned TOML file below explicit values and environment."""

        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(
                settings_cls, Path.home() / ".scs" / "config.toml"
            ),
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _validate_index_limits(self) -> "SCSSettings":
        """Keep bounded text detection within the accepted file envelope."""

        if self.index_text_sample_bytes > self.index_max_file_bytes:
            raise ValueError("index text sample cannot exceed maximum file size")
        if self.auto_reindex_idle_seconds < self.auto_reindex_active_seconds:
            raise ValueError(
                "automatic reindex idle interval cannot be shorter than active interval"
            )
        config_path = Path.home() / ".scs" / "config.toml"
        if config_path.is_file():
            with config_path.open("rb") as config_file:
                config_values = tomllib.load(config_file)
            if "openai_api_key" in config_values:
                permissions = config_path.stat().st_mode & 0o777
                if permissions & 0o077:
                    raise ValueError(
                        "config.toml containing openai_api_key must have "
                        "owner-only permissions"
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

    @field_validator("openai_base_url")
    @classmethod
    def _validate_openai_base_url(cls, value: str) -> str:
        """Require TLS for source-bearing requests sent beyond the workstation."""

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("openai_base_url must be an absolute https URL")
        return value.rstrip("/")

    @property
    def effective_openai_api_key(self) -> str | None:
        """Expose credentials only when the selected provider may receive them."""

        if self.embedding_provider != "openai" or self.openai_api_key is None:
            return None
        return self.openai_api_key.get_secret_value()

    @cached_property
    def paths(self) -> SCSPaths:
        """Return validated product paths without creating them."""

        return SCSPaths.resolve(
            self.home,
            model_cache=self.model_cache,
            runtime=self.runtime_dir,
            logs=self.log_dir,
        )

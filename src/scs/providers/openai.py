"""OpenAI file summarization adapter with explicit unavailable state."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable, Mapping
from typing import Protocol, cast, runtime_checkable

from scs.providers.base import ProviderUnavailableError


class _ResponsesAPI(Protocol):
    """Subset of the OpenAI Responses API consumed by SCS."""

    def create(self, **kwargs: object) -> object:
        """Create a model response."""

        ...


class _OpenAIClient(Protocol):
    """Client surface that owns the Responses API."""

    responses: _ResponsesAPI


class _OpenAIModule(Protocol):
    """Constructor surface exposed by the optional OpenAI package."""

    OpenAI: Callable[..., _OpenAIClient]


@runtime_checkable
class _Response(Protocol):
    """Response field consumed by the summarizer."""

    output_text: object


class OpenAIFileSummarizer:
    """Summarize code files through an injected or lazily loaded client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "gpt-4.1-mini",
        timeout_seconds: float = 45.0,
        responses: _ResponsesAPI | None = None,
    ) -> None:
        self._api_key: str | None = api_key
        self._model: str = model
        self._timeout_seconds: float = timeout_seconds
        self._responses: _ResponsesAPI | None = responses

    @property
    def provider_name(self) -> str:
        """Return stable provenance for summaries persisted by SCS."""

        return f"openai:{self._model}"

    def _get_responses(self) -> _ResponsesAPI:
        if self._responses is not None:
            return self._responses
        if not self._api_key:
            raise ProviderUnavailableError(
                "OpenAI summarization is unavailable because SCS_OPENAI_API_KEY is not set"
            )
        try:
            module = cast(
                _OpenAIModule, cast(object, importlib.import_module("openai"))
            )
        except ImportError as exc:
            raise ProviderUnavailableError(
                "OpenAI summarization is unavailable because the optional openai package is not installed"
            ) from exc
        responses = module.OpenAI(
            api_key=self._api_key, timeout=self._timeout_seconds
        ).responses
        self._responses = responses
        return responses

    async def summarize_files(self, files: Mapping[str, str]) -> dict[str, str]:
        """Return validated path-to-summary JSON from the model."""

        if not files:
            return {}
        payload = json.dumps(files, ensure_ascii=False)
        responses = self._get_responses()
        response = await asyncio.to_thread(
            responses.create,
            model=self._model,
            input=(
                "Summarize each code file in one precise sentence. Return only a JSON "
                f"object keyed by the exact input paths. Files: {payload}"
            ),
        )
        raw = response.output_text if isinstance(response, _Response) else ""
        if not isinstance(raw, (str, bytes, bytearray)):
            raw = ""
        try:
            decoded = cast(object, json.loads(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenAI summarizer returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("OpenAI summarizer returned a non-object result")
        values = cast(dict[object, object], decoded)
        summaries: dict[str, str] = {}
        for path, summary in values.items():
            if (
                isinstance(path, str)
                and path in files
                and isinstance(summary, str)
                and summary.strip()
            ):
                summaries[path] = summary.strip()
        return summaries

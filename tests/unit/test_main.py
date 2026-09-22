from __future__ import annotations

from pathlib import Path

import pytest

from scs.config import SCSSettings
from scs.main import build_reranker
from scs.providers.openai_compatible_reranking import (
    OpenAICompatibleRerankingProvider,
)


def test_reranker_is_disabled_without_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))

    assert build_reranker(SCSSettings()) is None


def test_reranker_uses_configured_model_and_openai_compatible_base_url() -> None:
    reranker = build_reranker(
        SCSSettings(
            reranking_model="configured-reranker",
            openai_compatible_base_url="http://localhost:9000/v1",
        )
    )

    assert reranker is not None
    assert isinstance(reranker, OpenAICompatibleRerankingProvider)
    assert reranker.metadata.model == "configured-reranker"
    assert reranker.endpoint == "http://localhost:9000/v1/rerank"

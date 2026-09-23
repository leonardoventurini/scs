"""A private worker cannot control routing with a bad identity or leaked env."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scs.orchestration.bundle import MODEL_DIGEST, MODEL_REPOSITORY, MODEL_REVISION
from scs.orchestration.decision import (
    DeterministicDecisionProvider,
    Playbook,
    RoutingRequest,
)
from scs.orchestration.laya_provider import LayaDecisionProvider
from scs.orchestration.protocol import PROTOCOL_VERSION, QUESTION_SCHEMA_VERSION
from scs.orchestration.query import QueryOrchestrator


def fake_runner(tmp_path: Path, *, wrong_identity: bool = False) -> Path:
    """Generate a tiny protocol peer without ONNX or stored fixture payloads."""

    handshake = {
        "protocol_version": PROTOCOL_VERSION,
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "model_digest": MODEL_DIGEST,
        "runtime_version": "test",
        "question_schema_version": QUESTION_SCHEMA_VERSION,
    }
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path('starts').open('a').write('x')\n"
        f"print({json.dumps(json.dumps(handshake))}, flush=True)\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " probabilities = {label: float(label == 'DISCOVER') for label in "
        "('DISCOVER','UNDERSTAND','RELATIONSHIPS','REFERENCES','INSPECT_FILES','IMPACT','INVENTORY')}\n"
        " response = {'request_id': "
        + ("'f' * 32" if wrong_identity else "request['request_id']")
        + ", 'decision': {'playbook': 'DISCOVER', 'model': 'fake', "
        "'confidence': 1.0, 'probabilities': probabilities}}\n"
        " if os.environ.get('OPENAI_API_KEY'): response = {'request_id': "
        "request['request_id'], 'error': 'secret_leaked'}\n"
        " print(json.dumps(response), flush=True)\n"
    )
    return script


@pytest.mark.asyncio
async def test_laya_parent_reuses_one_worker_without_inheriting_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    provider = LayaDecisionProvider(
        tmp_path, runner_command=(sys.executable, str(fake_runner(tmp_path)))
    )
    try:
        first = await provider.classify(RoutingRequest(goal="find parser"))
        second = await provider.classify(RoutingRequest(goal="list symbols"))

        assert first.playbook is second.playbook is Playbook.DISCOVER
        assert (tmp_path / "starts").read_text() == "x"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_wrong_response_identity_fails_open_to_discovery(tmp_path: Path) -> None:
    provider = LayaDecisionProvider(
        tmp_path,
        runner_command=(sys.executable, str(fake_runner(tmp_path, wrong_identity=True))),
    )
    fallback = DeterministicDecisionProvider()

    async def unused_route(_method: str, _params: dict[str, object]) -> dict[str, object]:
        return {"results": []}

    query = QueryOrchestrator(provider=provider, call=unused_route)
    try:
        result = await query.query({"goal": "find parser", "repo_path": str(tmp_path)})

        assert result["routing"]["playbook"] == Playbook.DISCOVER.value
        assert result["routing"]["degraded_reason"] == "classifier_unavailable"
        assert (await fallback.classify(RoutingRequest(goal="find parser"))).playbook is Playbook.DISCOVER
    finally:
        await provider.close()

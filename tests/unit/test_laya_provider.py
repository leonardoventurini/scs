"""SCS sends only policy choices and validates external choice responses."""

from __future__ import annotations

import asyncio

import pytest

from scs.orchestration.decision import Playbook, RoutingRequest
from scs.orchestration.laya_provider import LayaDecisionProvider, MODEL_ID


def decision() -> dict[str, object]:
    return {
        "model": MODEL_ID,
        "choice": "UNDERSTAND",
        "confidence": 0.8,
        "probabilities": {
            playbook.value: 0.8 if playbook is Playbook.UNDERSTAND else 0.2 / 6
            for playbook in Playbook
        },
    }


@pytest.mark.asyncio
async def test_provider_sends_bounded_request_and_accepts_complete_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        _self: LayaDecisionProvider,
        method: str,
        route: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        calls.append((method, route, payload))
        if route == "/decisions/ready":
            return {"status": "ok", "model": MODEL_ID}
        return decision()

    monkeypatch.setattr(LayaDecisionProvider, "_request", request)
    provider = LayaDecisionProvider("http://127.0.0.1:10000/v1")
    try:
        await provider.start()
        answer = await provider.classify(RoutingRequest(goal="find parser"))
        assert answer.playbook is Playbook.UNDERSTAND
        method, route, payload = calls[-1]
        assert (method, route) == ("POST", "/decisions")
        assert payload is not None
        assert payload["state"] == {"goal": "find parser", "node_ids": [], "file_paths": []}
        assert payload["model"] == MODEL_ID
        question = payload["question"]
        assert isinstance(question, dict)
        assert set(question["criteria"]) == {playbook.value for playbook in Playbook}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_rejects_incompatible_service_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LayaDecisionProvider,
        "_request",
        lambda *_args, **_kwargs: {"status": "ok", "model": "wrong"},
    )
    provider = LayaDecisionProvider("http://127.0.0.1:10000/v1")
    with pytest.raises(RuntimeError, match="unavailable or incompatible"):
        await provider.start()
    await provider.close()


@pytest.mark.asyncio
async def test_provider_rejects_invalid_choice_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(
        _self: LayaDecisionProvider,
        _method: str,
        route: str,
        _payload: dict[str, object] | None = None,
    ) -> object:
        if route == "/decisions/ready":
            return {"status": "ok", "model": MODEL_ID}
        answer = decision()
        answer["choice"] = "ARBITRARY_TOOL"
        return answer

    monkeypatch.setattr(LayaDecisionProvider, "_request", request)
    provider = LayaDecisionProvider("http://127.0.0.1:10000/v1")
    try:
        await provider.start()
        with pytest.raises(ValueError):
            await provider.classify(RoutingRequest(goal="find parser"))
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_pauses_during_service_outage_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = True

    def request(
        _self: LayaDecisionProvider,
        _method: str,
        route: str,
        _payload: dict[str, object] | None = None,
    ) -> object:
        if route == "/decisions/ready":
            return {"status": "ok", "model": MODEL_ID} if healthy else None
        return decision()

    monkeypatch.setattr(LayaDecisionProvider, "_request", request)
    monkeypatch.setattr("scs.orchestration.laya_provider.HEALTH_INTERVAL_SECONDS", 0.01)
    provider = LayaDecisionProvider("http://127.0.0.1:10000/v1")
    try:
        await provider.start()
        healthy = False
        async with asyncio.timeout(1):
            while provider.is_ready:
                await asyncio.sleep(0.01)
        waiting = asyncio.create_task(provider.wait_ready())
        await asyncio.sleep(0.02)
        assert not waiting.done()
        healthy = True
        await asyncio.wait_for(waiting, timeout=1)
        assert provider.is_ready
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_shuts_down_after_bounded_failed_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = True
    unavailable = asyncio.Event()

    def request(
        _self: LayaDecisionProvider,
        _method: str,
        _route: str,
        _payload: dict[str, object] | None = None,
    ) -> object:
        return {"status": "ok", "model": MODEL_ID} if healthy else None

    monkeypatch.setattr(LayaDecisionProvider, "_request", request)
    monkeypatch.setattr("scs.orchestration.laya_provider.HEALTH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("scs.orchestration.laya_provider.RECOVERY_TIMEOUT_SECONDS", 0.02)
    provider = LayaDecisionProvider(
        "http://127.0.0.1:10000/v1", on_unavailable=unavailable.set
    )
    try:
        await provider.start()
        healthy = False
        await asyncio.wait_for(unavailable.wait(), timeout=1)
        assert not provider.is_ready
        with pytest.raises(RuntimeError, match="recovery failed"):
            await provider.wait_ready()
    finally:
        await provider.close()

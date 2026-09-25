"""Strict HTTP client for an external Laya choice service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import http.client
import json
from time import monotonic
from typing import ClassVar, Final, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from scs.orchestration.decision import (
    PLAYBOOK_DESCRIPTIONS,
    Playbook,
    RoutingDecision,
    RoutingRequest,
)

MODEL_REPOSITORY: Final[str] = "aac6fef/laya-mlx"
MODEL_REVISION: Final[str] = "20aed815fc6acde75733882e7ec0e3f28aeb9717"
MODEL_ID: Final[str] = f"{MODEL_REPOSITORY}@{MODEL_REVISION}"
QUESTION: Final[str] = "Select the single best code investigation workflow for this goal."
MAX_RESPONSE_BYTES = 16_384
HEALTH_INTERVAL_SECONDS = 0.25
RECOVERY_TIMEOUT_SECONDS = 30.0


class ChoiceResponse(BaseModel):
    """Validate the generic choice answer before mapping it to SCS policy."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    model: str
    choice: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[str, float]


class LayaDecisionProvider:
    """Require a healthy choice endpoint throughout daemon lifetime."""

    def __init__(
        self,
        base_url: str,
        *,
        max_concurrency: int = 1,
        on_unavailable: Callable[[], None] | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 4:
            raise ValueError("Laya concurrency must be between one and four")
        self._base_url: str = base_url.rstrip("/")
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)
        self._ready: bool = False
        self._closed: bool = False
        self._terminal_error: Exception | None = None
        self._condition: asyncio.Condition = asyncio.Condition()
        self._monitor: asyncio.Task[None] | None = None
        self._on_unavailable: Callable[[], None] | None = on_unavailable

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._closed and self._terminal_error is None

    def _request(self, method: str, route: str, payload: dict[str, object] | None = None) -> object:
        parsed = urlsplit(self._base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("decision base URL must be HTTP")
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        try:
            connection.request(
                method,
                f"{parsed.path}{route}",
                body=body,
                headers={"Content-Type": "application/json"} if body is not None else {},
            )
            response = connection.getresponse()
            contents = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or len(contents) > MAX_RESPONSE_BYTES:
                raise RuntimeError("decision service returned an invalid response")
            return cast(object, json.loads(contents))
        finally:
            connection.close()

    async def _probe(self) -> bool:
        try:
            payload = await asyncio.to_thread(self._request, "GET", "/decisions/ready")
            return payload == {"status": "ok", "model": MODEL_ID}
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            return False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Laya decision provider is closed")
        if not await self._probe():
            raise RuntimeError("Laya decision service is unavailable or incompatible")
        self._ready = True
        self._monitor = asyncio.create_task(self._watch_health())

    async def _watch_health(self) -> None:
        unavailable_since: float | None = None
        while not self._closed:
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            healthy = await self._probe()
            async with self._condition:
                self._ready = healthy
                self._condition.notify_all()
            if healthy:
                unavailable_since = None
            else:
                unavailable_since = unavailable_since or monotonic()
                if monotonic() - unavailable_since >= RECOVERY_TIMEOUT_SECONDS:
                    self._terminal_error = RuntimeError("Laya service recovery failed")
                    async with self._condition:
                        self._condition.notify_all()
                    if self._on_unavailable is not None:
                        self._on_unavailable()
                    return

    async def wait_ready(self) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self.is_ready or self._terminal_error is not None or self._closed
            )
        if self._terminal_error is not None:
            raise RuntimeError("Laya service recovery failed") from self._terminal_error
        if self._closed:
            raise RuntimeError("Laya decision provider is closed")

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        if not self.is_ready:
            raise RuntimeError("Laya decision service is unavailable")
        payload: dict[str, object] = {
            "model": MODEL_ID,
            "state": request.model_dump(exclude_none=True),
            "question": {
                "key": "playbook",
                "instructions": QUESTION,
                "criteria": {
                    playbook.value: PLAYBOOK_DESCRIPTIONS[playbook]
                    for playbook in Playbook
                },
            },
        }
        async with self._semaphore:
            raw = await asyncio.to_thread(self._request, "POST", "/decisions", payload)
        response = ChoiceResponse.model_validate(raw)
        if response.model != MODEL_ID:
            raise ValueError("Laya model identity differs from configuration")
        if set(response.probabilities) != {playbook.value for playbook in Playbook}:
            raise ValueError("Laya probabilities are incomplete")
        return RoutingDecision(
            playbook=Playbook(response.choice),
            model=response.model,
            confidence=response.confidence,
            probabilities={Playbook(key): value for key, value in response.probabilities.items()},
        )

    async def close(self) -> None:
        self._closed = True
        self._ready = False
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        async with self._condition:
            self._condition.notify_all()

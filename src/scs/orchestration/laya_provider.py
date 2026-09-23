"""Supervise the private local Laya subprocess and validate its responses."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic

from scs.orchestration.bundle import MODEL_DIGEST, MODEL_REPOSITORY, MODEL_REVISION
from scs.orchestration.decision import RoutingDecision, RoutingRequest
from scs.orchestration.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    QUESTION_SCHEMA_VERSION,
    RunnerHandshake,
    RunnerRequest,
    RunnerResponse,
)

MAX_WAITING_REQUESTS = 16
STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 35.0
MAX_WORKER_EXITS = 3
WORKER_EXIT_WINDOW_SECONDS = 30.0


class LayaDecisionProvider:
    """Own one warmed worker and recover it while the daemon is running."""

    def __init__(
        self,
        model_path: Path,
        *,
        max_concurrency: int = 1,
        runner_command: Sequence[str] | None = None,
        on_unavailable: Callable[[], None] | None = None,
    ) -> None:
        if not model_path.is_absolute():
            raise ValueError("Laya model path must be absolute")
        if not 1 <= max_concurrency <= 4:
            raise ValueError("Laya concurrency must be between one and four")
        self._model_path: Path = model_path
        self._max_concurrency: int = max_concurrency
        self._runner_command: tuple[str, ...] = tuple(
            runner_command or (sys.executable, "-m", "scs.orchestration.laya_runner")
        )
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)
        self._startup_lock: asyncio.Lock = asyncio.Lock()
        self._ready_condition: asyncio.Condition = asyncio.Condition()
        self._writer_lock: asyncio.Lock = asyncio.Lock()
        self._startup: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[RoutingDecision]] = {}
        self._abandoned: set[str] = set()
        self._waiting: int = 0
        self._closed: bool = False
        self._terminal_error: Exception | None = None
        self._on_unavailable: Callable[[], None] | None = on_unavailable
        self._recent_exits: deque[float] = deque(maxlen=MAX_WORKER_EXITS)

    @property
    def is_ready(self) -> bool:
        """True only while a verified worker generation is alive."""

        process = self._process
        return (
            not self._closed
            and self._terminal_error is None
            and process is not None
            and process.returncode is None
        )

    async def start(self) -> None:
        """Load and warm the worker before daemon readiness is published."""

        await self._ensure_started()

    async def wait_ready(self) -> None:
        """Hold a new query through automatic worker recovery."""

        async with self._ready_condition:
            await self._ready_condition.wait_for(
                lambda: (
                    self.is_ready or self._terminal_error is not None or self._closed
                )
            )
            if self._terminal_error is not None:
                raise RuntimeError(
                    "Laya worker recovery failed"
                ) from self._terminal_error
            if self._closed:
                raise RuntimeError("Laya decision provider is closed")

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        """Submit one request or fail so orchestration can discover safely."""

        if self._closed or self._waiting >= MAX_WAITING_REQUESTS:
            raise RuntimeError("Laya decision queue is unavailable")
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        identity = uuid.uuid4().hex
        future: asyncio.Future[RoutingDecision] = (
            asyncio.get_running_loop().create_future()
        )
        try:
            process = await self._ensure_started()
            if self._closed:
                raise RuntimeError("Laya decision provider is closed")
            request_line = (
                RunnerRequest(request_id=identity, routing=request)
                .model_dump_json()
                .encode()
                + b"\n"
            )
            if len(request_line) > MAX_MESSAGE_BYTES:
                raise ValueError("Laya routing request exceeds pipe limit")
            self._pending[identity] = future
            async with self._writer_lock:
                if process.stdin is None:
                    raise RuntimeError("Laya worker has no input pipe")
                process.stdin.write(request_line)
                await process.stdin.drain()
            return await future
        finally:
            if self._pending.pop(identity, None) is not None and (
                future.cancelled() or not future.done()
            ):
                if not future.done():
                    future.cancel()
                self._abandoned.add(identity)
                if len(self._abandoned) > MAX_WAITING_REQUESTS:
                    process = self._process
                    if process is not None and process.returncode is None:
                        process.kill()
            self._semaphore.release()

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        async with self._startup_lock:
            if self._closed:
                raise RuntimeError("Laya decision provider is closed")
            process = self._process
            if process is not None and process.returncode is None:
                return process
            if self._startup is None or self._startup.done():
                self._startup = asyncio.create_task(self._spawn())
            startup = self._startup
        return await asyncio.shield(startup)

    async def _spawn(self) -> asyncio.subprocess.Process:
        environment = {
            key: value
            for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")
            if (value := os.environ.get(key)) is not None
        }
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        process = await asyncio.create_subprocess_exec(
            *self._runner_command,
            str(self._model_path),
            "--workers",
            str(self._max_concurrency),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._model_path,
            env=environment,
            limit=MAX_MESSAGE_BYTES,
        )
        try:
            if process.stdout is None:
                raise RuntimeError("Laya worker has no output pipe")
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=STARTUP_TIMEOUT_SECONDS
            )
            handshake = RunnerHandshake.model_validate_json(line)
            if (
                handshake.protocol_version != PROTOCOL_VERSION
                or handshake.question_schema_version != QUESTION_SCHEMA_VERSION
                or handshake.model_repository != MODEL_REPOSITORY
                or handshake.model_revision != MODEL_REVISION
                or handshake.model_digest != MODEL_DIGEST
            ):
                raise RuntimeError("Laya worker handshake is incompatible")
        except Exception:
            process.kill()
            await process.wait()
            raise
        async with self._ready_condition:
            self._process = process
            self._terminal_error = None
            self._ready_condition.notify_all()
        self._reader = asyncio.create_task(self._read_responses(process))
        return process

    async def _read_responses(self, process: asyncio.subprocess.Process) -> None:
        try:
            if process.stdout is None:
                raise RuntimeError("Laya worker output pipe closed")
            while line := await process.stdout.readline():
                if len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("Laya response exceeds pipe limit")
                response = RunnerResponse.model_validate_json(line)
                future = self._pending.get(response.request_id)
                if future is None:
                    if response.request_id in self._abandoned:
                        self._abandoned.remove(response.request_id)
                        continue
                    raise ValueError("Laya response has unknown request identity")
                if future.cancelled():
                    continue
                if future.done():
                    raise ValueError("Laya worker returned a duplicate response")
                if response.error is not None:
                    future.set_exception(RuntimeError(response.error))
                elif response.decision is not None:
                    future.set_result(response.decision)
            raise RuntimeError("Laya worker exited")
        except Exception:
            async with self._ready_condition:
                if self._process is process:
                    self._process = None
                    self._startup = None
                self._ready_condition.notify_all()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Laya worker failed"))
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            self._abandoned.clear()
            if not self._closed:
                try:
                    now = monotonic()
                    self._recent_exits.append(now)
                    if (
                        len(self._recent_exits) == MAX_WORKER_EXITS
                        and now - self._recent_exits[0] < WORKER_EXIT_WINDOW_SECONDS
                    ):
                        raise RuntimeError("Laya worker exited repeatedly")
                    await self._ensure_started()
                except Exception as error:
                    async with self._ready_condition:
                        self._terminal_error = error
                        self._ready_condition.notify_all()
                    if self._on_unavailable is not None:
                        self._on_unavailable()

    async def close(self) -> None:
        """Drain bounded work and release the owned subprocess."""

        async with self._ready_condition:
            self._closed = True
            self._ready_condition.notify_all()
        startup = self._startup
        if startup is not None and not startup.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(startup), timeout=STARTUP_TIMEOUT_SECONDS
                )
            except Exception:
                startup.cancel()
        if self._pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending.values(), return_exceptions=True),
                    timeout=SHUTDOWN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                pass
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        reader = self._reader
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)

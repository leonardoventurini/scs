"""Bounded, fail-open telemetry for SCS MCP tool execution."""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import override

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult

DEFAULT_EVENT_CAPACITY = 2_000


@dataclass(frozen=True, slots=True)
class ToolEvent:
    """One bounded MCP execution fact without argument or result payloads."""

    tool_name: str
    started_at_unix_ms: int
    duration_ms: float
    status: str
    error_type: str | None


class ToolRecorder:
    """Retain a bounded process-local execution window."""

    def __init__(self, capacity: int = DEFAULT_EVENT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("MCP event capacity must be positive")
        self._events: deque[ToolEvent] = deque(maxlen=capacity)
        self._capacity: int = capacity
        self._dropped: int = 0
        self._lock: threading.Lock = threading.Lock()

    def record(self, event: ToolEvent) -> None:
        """Record one event and account for bounded-window eviction."""

        with self._lock:
            if len(self._events) == self._capacity:
                self._dropped += 1
            self._events.append(event)

    def snapshot(self, *, limit: int = 200) -> dict[str, object]:
        """Return recent events and status counts."""

        with self._lock:
            retained = list(self._events)
            dropped = self._dropped
        recent = retained[-max(1, min(limit, self._capacity)) :]
        return {
            "events": [asdict(event) for event in reversed(recent)],
            "status_counts": dict(Counter(event.status for event in retained)),
            "retained_event_count": len(retained),
            "dropped_event_count": dropped,
            "capacity": self._capacity,
        }


class ObservedMCPServer(MCPServer[None]):
    """MCP host whose telemetry cannot break tool execution."""

    def __init__(self, name: str, *, recorder: ToolRecorder) -> None:
        super().__init__(name)
        self.recorder: ToolRecorder = recorder

    @override
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: Context[None, object] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        started_at_unix_ms = int(time.time() * 1_000)
        started_at = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception as error:
            self._record_fail_open(
                ToolEvent(
                    name,
                    started_at_unix_ms,
                    (time.perf_counter() - started_at) * 1_000,
                    "error",
                    type(error).__name__,
                )
            )
            raise
        self._record_fail_open(
            ToolEvent(
                name,
                started_at_unix_ms,
                (time.perf_counter() - started_at) * 1_000,
                "ok",
                None,
            )
        )
        return result

    def _record_fail_open(self, event: ToolEvent) -> None:
        try:
            self.recorder.record(event)
        except Exception:
            return

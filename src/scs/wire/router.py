"""Typed method routing for finite SCSWire requests."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias

from pydantic import ValidationError

from scs.wire.models import ErrorCode, WireError

WireResult: TypeAlias = dict[str, object]
WireHandler: TypeAlias = Callable[[dict[str, object]], Awaitable[WireResult]]
OperationObserver: TypeAlias = Callable[
    [str, dict[str, object], str, float],
    None,
]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Internal success-or-error result used by server transports."""

    value: WireResult | None = None
    error: WireError | None = None


class Router:
    """Register and invoke SCSWire methods behind stable error categories."""

    def __init__(self, observer: OperationObserver | None = None) -> None:
        self._handlers: dict[str, WireHandler] = {}
        self._observer: OperationObserver | None = observer

    def method(self, name: str) -> Callable[[WireHandler], WireHandler]:
        """Register one async handler under a unique method name."""

        if not name or name.strip() != name:
            raise ValueError("method name must be non-empty and normalized")

        def register(handler: WireHandler) -> WireHandler:
            if name in self._handlers:
                raise ValueError(f"method already registered: {name}")
            if not inspect.iscoroutinefunction(handler):
                raise TypeError("SCSWire handlers must be async functions")
            self._handlers[name] = handler
            return handler

        return register

    async def dispatch(
        self,
        method: str,
        params: dict[str, object],
    ) -> DispatchResult:
        """Invoke a method while containing public validation and internal errors."""

        handler = self._handlers.get(method)
        if handler is None:
            return DispatchResult(
                error=WireError(
                    code=ErrorCode.UNKNOWN_METHOD,
                    message=f"unknown SCSWire method: {method}",
                )
            )
        started = time.perf_counter()
        try:
            result = DispatchResult(value=await handler(params))
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            result = DispatchResult(
                error=WireError(code=ErrorCode.BAD_REQUEST, message=str(error))
            )
        except Exception:
            result = DispatchResult(
                error=WireError(
                    code=ErrorCode.INTERNAL,
                    message="SCSWire method failed internally",
                )
            )
        self._observe_fail_open(
            method,
            params,
            "error" if result.error is not None else "ok",
            (time.perf_counter() - started) * 1_000,
        )
        return result

    def _observe_fail_open(
        self,
        method: str,
        params: dict[str, object],
        status: str,
        duration_ms: float,
    ) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            observer(method, params, status, duration_ms)
        except Exception:
            return

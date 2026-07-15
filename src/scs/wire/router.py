"""Typed method routing for finite SCSWire requests."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias

from pydantic import ValidationError

from scs.wire.models import ErrorCode, WireError

WireResult: TypeAlias = dict[str, object]
WireHandler: TypeAlias = Callable[[dict[str, object]], Awaitable[WireResult]]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Internal success-or-error result used by server transports."""

    value: WireResult | None = None
    error: WireError | None = None


class Router:
    """Register and invoke SCSWire methods behind stable error categories."""

    def __init__(self) -> None:
        self._handlers: dict[str, WireHandler] = {}

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
        try:
            return DispatchResult(value=await handler(params))
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            return DispatchResult(
                error=WireError(code=ErrorCode.BAD_REQUEST, message=str(error))
            )
        except Exception:
            return DispatchResult(
                error=WireError(
                    code=ErrorCode.INTERNAL,
                    message="SCSWire method failed internally",
                )
            )

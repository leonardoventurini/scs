"""Cross-thread admission control for native graph reads and deletes."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import Condition
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from scs.errors import ServiceBusyError

if TYPE_CHECKING:
    from scs.graph.native import NativeGraph

P = ParamSpec("P")
R = TypeVar("R")


class GraphBusyError(ServiceBusyError):
    """Raised when a read would collide with a potentially long mutation."""


class GraphMutationGate:
    """Reserve deletes before draining in-flight reads; reject later reads."""

    def __init__(self) -> None:
        self._condition: Condition = Condition()
        self._readers: int = 0
        self._mutation_reserved: bool = False
        self._mutation_active: bool = False

    @contextmanager
    def read(self) -> Generator[None]:
        """Enter only when no mutation is reserved; never wait behind deletes."""

        with self._condition:
            if self._mutation_reserved or self._mutation_active:
                raise GraphBusyError("graph index is temporarily busy")
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def mutation(self) -> Generator[None]:
        """Serialize mutations after waiting only for reads already in flight."""

        with self._condition:
            while self._mutation_reserved or self._mutation_active:
                self._condition.wait()
            self._mutation_reserved = True
            while self._readers > 0:
                self._condition.wait()
            self._mutation_reserved = False
            self._mutation_active = True
        try:
            yield
        finally:
            with self._condition:
                self._mutation_active = False
                self._condition.notify_all()


def graph_read(
    method: Callable[Concatenate[NativeGraph, P], R],
) -> Callable[Concatenate[NativeGraph, P], R]:
    """Run a native read only when no structural mutation is admitted."""

    def wrapper(graph: NativeGraph, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with graph.mutation_gate.read():
            return method(graph, *args, **kwargs)

    return wrapper


def graph_mutation(
    method: Callable[Concatenate[NativeGraph, P], R],
) -> Callable[Concatenate[NativeGraph, P], R]:
    """Reserve and serialize a potentially long structural mutation."""

    def wrapper(graph: NativeGraph, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with graph.mutation_gate.mutation():
            return method(graph, *args, **kwargs)

    return wrapper

"""In-process event fan-out for SCSWire background operations."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from scs.wire.models import WireEvent

EVENT_QUEUE_CAPACITY = 256


class EventSubscription:
    """Closable asynchronous iterator for one broker topic."""

    def __init__(
        self,
        broker: "EventBroker",
        topic: str,
        queue: asyncio.Queue[WireEvent],
    ) -> None:
        self._broker = broker
        self._topic = topic
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> "EventSubscription":
        return self

    async def __anext__(self) -> WireEvent:
        if self._closed:
            raise StopAsyncIteration
        event = await self._queue.get()
        if event.terminal:
            await self.aclose()
        return event

    async def aclose(self) -> None:
        """Detach this subscriber immediately and idempotently."""

        if self._closed:
            return
        self._closed = True
        self._broker._remove(self._topic, self._queue)


class EventBroker:
    """Publish monotonic topic events to independently closable subscribers."""

    def __init__(self) -> None:
        self._sequences: defaultdict[str, int] = defaultdict(int)
        self._subscribers: defaultdict[
            str, set[asyncio.Queue[WireEvent]]
        ] = defaultdict(set)

    def subscribe(self, topic: str) -> EventSubscription:
        """Attach a subscriber before returning so early events cannot be lost."""

        if not topic:
            raise ValueError("event topic must be non-empty")
        queue: asyncio.Queue[WireEvent] = asyncio.Queue(EVENT_QUEUE_CAPACITY)
        self._subscribers[topic].add(queue)
        return EventSubscription(self, topic, queue)

    async def publish(
        self,
        topic: str,
        payload: dict[str, object],
        *,
        terminal: bool = False,
    ) -> WireEvent:
        """Publish the next topic sequence, applying backpressure to producers."""

        self._sequences[topic] += 1
        event = WireEvent(
            topic=topic,
            sequence=self._sequences[topic],
            terminal=terminal,
            payload=payload,
        )
        subscribers = tuple(self._subscribers.get(topic, ()))
        for queue in subscribers:
            await queue.put(event)
        return event

    def subscriber_count(self, topic: str) -> int:
        """Return the current number of subscribers for diagnostics and tests."""

        return len(self._subscribers.get(topic, ()))

    def _remove(self, topic: str, queue: asyncio.Queue[WireEvent]) -> None:
        subscribers = self._subscribers.get(topic)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[topic]

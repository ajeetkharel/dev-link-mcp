"""Live notifications for open dashboard tabs: audit events, process changes, task changes, log growth."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

Message = tuple[str, dict[str, Any]]


class LiveFeed:
    """Fan-out of typed messages to every subscribed dashboard stream.

    A message is (type, data), where type is "audit", "process", "tasks" or "log". Publishers
    check `active` first so nothing is computed while no tab is open.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[Message]] = set()

    @property
    def active(self) -> bool:
        """True while at least one stream is subscribed."""
        return bool(self._subscribers)

    def subscribe(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Message]) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Queue `data` for every subscriber. Never blocks: a full queue drops the message."""
        for queue in list(self._subscribers):
            # A stalled browser tab must not block tool calls; it catches up when it reconnects.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((kind, data))

"""In-memory pub/sub event broker.

Each job gets a :class:`JobChannel` that buffers its events (so a subscriber
that connects late still replays the whole stream) and fans live events out to
any number of SSE / WebSocket subscribers. When a job finishes the channel is
closed; subscribers receive everything up to and including the terminal event,
then the iterator ends.

Channels are retained briefly after completion so late subscribers can replay
from memory; older finished channels are swept and callers fall back to reading
events from the database.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_SENTINEL = object()


class JobChannel:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.buffer: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.closed_at: float | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.buffer.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def close(self) -> None:
        self.done = True
        self.closed_at = time.time()
        for q in list(self._subscribers):
            q.put_nowait(_SENTINEL)

    async def subscribe(self, replay: bool = True):
        """Yield events: buffered (if replay) then live until the channel closes."""
        q: asyncio.Queue = asyncio.Queue()
        # Snapshot current buffer, then register, to avoid missing/dupe events.
        start_index = 0
        if replay:
            for ev in self.buffer:
                yield ev
            start_index = len(self.buffer)
        else:
            start_index = len(self.buffer)
        self._subscribers.add(q)
        try:
            # If events arrived between snapshot and registration, flush them.
            for ev in self.buffer[start_index:]:
                yield ev
            if self.done:
                return
            while True:
                item = await q.get()
                if item is _SENTINEL:
                    return
                yield item
        finally:
            self._subscribers.discard(q)


class EventBroker:
    def __init__(self, retain_s: float = 120.0) -> None:
        self._channels: dict[str, JobChannel] = {}
        self._retain_s = retain_s

    def get_or_create(self, job_id: str) -> JobChannel:
        ch = self._channels.get(job_id)
        if ch is None:
            ch = JobChannel(job_id)
            self._channels[job_id] = ch
            self._sweep()
        return ch

    def get(self, job_id: str) -> JobChannel | None:
        return self._channels.get(job_id)

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        self.get_or_create(job_id).publish(event)

    def close(self, job_id: str) -> None:
        ch = self._channels.get(job_id)
        if ch is not None:
            ch.close()

    def _sweep(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, ch in self._channels.items()
            if ch.done and ch.closed_at and (now - ch.closed_at) > self._retain_s
        ]
        for jid in stale:
            self._channels.pop(jid, None)

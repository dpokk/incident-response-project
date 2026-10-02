"""A small thread-safe event bus: background threads publish, browsers subscribe over Server-Sent Events.

Events are {"id", "type", "t", "data"}. A bounded history lets a page that connects (or reconnects) mid-incident
replay what already happened instead of starting blank.
"""
import asyncio
import itertools
import threading
import time
from collections import deque


class EventBus:
    def __init__(self, keep: int = 600):
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._history: deque = deque(maxlen=keep)
        self._ids = itertools.count(1)

    def publish(self, type_: str, data: dict | None = None, keep: bool = True) -> dict:
        ev = {"id": next(self._ids), "type": type_, "t": time.time(), "data": data or {}}
        with self._lock:
            if keep:
                self._history.append(ev)
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:          # the subscriber's loop has closed
                self.unsubscribe(q)
        return ev

    def subscribe(self) -> tuple[asyncio.Queue, list[dict]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
            history = list(self._history)
        return q, history

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lp, x) for lp, x in self._subs if x is not q]

    def clear_history(self, types: set[str]) -> None:
        with self._lock:
            self._history = deque((e for e in self._history if e["type"] not in types), maxlen=self._history.maxlen)

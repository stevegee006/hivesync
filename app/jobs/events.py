"""In-process pub/sub for live run output.

SSE subscribers read from here rather than from the database. A stream can stay
open for the length of a sync, and holding a SQLite session open that long would
block writers. The database is touched only at state transitions; everything in
between flows through this broker.

Single process only. That matches the deployment: one container, one uvicorn.
If this ever runs multiple workers, this becomes Redis or a table, and the
subscribe/publish shape stays the same.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Bounded so a subscriber that stops reading cannot grow without limit. A slow
# reader loses the oldest lines rather than the producer stalling on it: a sync
# must never be held up by a browser tab.
_QUEUE_SIZE = 500


@dataclass
class RunEvent:
    """One thing worth telling a watcher about."""

    kind: str  # "line", "status", "done"
    text: str = ""
    data: dict[str, object] = field(default_factory=dict)


class RunBroker:
    """Fan-out of run output to zero or more subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, list[queue.Queue[RunEvent | None]]] = {}
        # Recent lines, so a browser that connects mid-run sees context rather
        # than an empty pane until the next line arrives.
        self._backlog: dict[int, list[RunEvent]] = {}
        self._backlog_limit = 200

    def publish(self, run_id: int, event: RunEvent) -> None:
        with self._lock:
            backlog = self._backlog.setdefault(run_id, [])
            backlog.append(event)
            if len(backlog) > self._backlog_limit:
                del backlog[: len(backlog) - self._backlog_limit]
            subscribers = list(self._subscribers.get(run_id, []))

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # Drop the oldest and retry once. A wedged reader must not stall
                # the sync producing the output.
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def finish(self, run_id: int) -> None:
        """Signal end of stream to every subscriber and release the backlog."""
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, []))
        for subscriber in subscribers:
            with contextlib.suppress(queue.Full):
                subscriber.put_nowait(None)
        with self._lock:
            self._backlog.pop(run_id, None)

    def subscribe(self, run_id: int) -> Iterator[RunEvent]:
        """Yield events until the run finishes or the caller disconnects."""
        channel: queue.Queue[RunEvent | None] = queue.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            backlog = list(self._backlog.get(run_id, []))
            self._subscribers.setdefault(run_id, []).append(channel)
        try:
            yield from backlog
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    # A heartbeat, so a proxy in the middle does not time the
                    # connection out during a long quiet transfer.
                    yield RunEvent(kind="ping")
                    continue
                if event is None:
                    return
                yield event
        finally:
            with self._lock:
                channels = self._subscribers.get(run_id)
                if channels and channel in channels:
                    channels.remove(channel)
                if channels is not None and not channels:
                    self._subscribers.pop(run_id, None)


broker = RunBroker()

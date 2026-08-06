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
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Bounded so a subscriber that stops reading cannot grow without limit. A slow
# reader loses the oldest lines rather than the producer stalling on it: a sync
# must never be held up by a browser tab.
_QUEUE_SIZE = 500

# How many finished run ids to remember. Only needs to outlive the browsers that
# might still be connecting to a run that just ended.
_FINISHED_MEMORY = 500


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
        # Runs that have already ended. `finish` delivers its sentinel to the
        # subscribers that exist at that moment, so a subscriber arriving one
        # moment later would wait for an end signal that already happened and
        # hold the connection open forever. Browsers allow about six connections
        # per host, so a few of those and the whole UI stops responding.
        self._finished: OrderedDict[int, None] = OrderedDict()

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
            self._finished[run_id] = None
            while len(self._finished) > _FINISHED_MEMORY:
                self._finished.popitem(last=False)

    def backlog_for(self, run_id: int) -> list[RunEvent]:
        """Whatever has been published for a run, without subscribing to more.

        For a caller that already knows the run is over and only wants to show
        what it said.
        """
        with self._lock:
            return list(self._backlog.get(run_id, []))

    def subscribe(self, run_id: int) -> Iterator[RunEvent]:
        """Yield events until the run finishes or the caller disconnects."""
        channel: queue.Queue[RunEvent | None] = queue.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            backlog = list(self._backlog.get(run_id, []))
            already_finished = run_id in self._finished
            if not already_finished:
                self._subscribers.setdefault(run_id, []).append(channel)

        if already_finished:
            # Nothing more is coming. Hand over whatever is left and close,
            # rather than waiting for an end signal that has already been sent.
            yield from backlog
            return

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

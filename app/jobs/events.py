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
import time
from collections import OrderedDict, deque
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
        # The most recent stats event per run, held apart from the backlog. See
        # `publish`. Replayed on subscribe so a page opened mid transfer draws
        # its progress bars straight away rather than after the next tick.
        self._latest_stats: dict[int, RunEvent] = {}
        # Runs that have already ended. `finish` delivers its sentinel to the
        # subscribers that exist at that moment, so a subscriber arriving one
        # moment later would wait for an end signal that already happened and
        # hold the connection open forever. Browsers allow about six connections
        # per host, so a few of those and the whole UI stops responding.
        self._finished: OrderedDict[int, None] = OrderedDict()

    def publish(self, run_id: int, event: RunEvent) -> None:
        with self._lock:
            if event.kind == "stats":
                # Progress and the log are different things with different
                # retention. One stats event every few seconds would fill a
                # bounded backlog and evict the log lines, which are what
                # someone actually reads when a run fails: at --stats 5s a
                # twenty minute sync produces more stats events than the
                # backlog holds, so a browser joining mid-run would see a
                # progress bar and an empty log. Only the most recent one is
                # kept, and that is all a late joiner needs, since the next
                # event supersedes it anyway.
                self._latest_stats[run_id] = event
            else:
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
            self._latest_stats.pop(run_id, None)
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
            latest_stats = self._latest_stats.get(run_id)
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
            if latest_stats is not None:
                yield latest_stats
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


# One hour of samples at the 5 second stats interval, which is what the chart's
# widest window needs. Held in memory and lost on restart, exactly like the
# session figures it feeds.
_SAMPLE_MEMORY = (60 * 60) // 5


@dataclass(frozen=True)
class SpeedSample:
    """One point on the activity chart."""

    at: float  # Unix seconds
    speed: float  # bytes per second, summed across every active run


class ActivityRecorder:
    """Live progress, kept apart from the log-line backlog on purpose.

    Stats arrive every few seconds for the whole length of a sync. Folding them
    into the same bounded backlog as the log lines would evict the lines, which
    are the thing someone actually reads when a run goes wrong. So the latest
    stats live here, one per run, replaced rather than accumulated.

    Session totals cover the current burst of activity: they accumulate while
    something is running and clear once everything has finished. The lifetime
    figure comes from the database instead, where it belongs, and is untouched
    by any of this.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[int, object] = {}
        self._samples: deque[SpeedSample] = deque(maxlen=_SAMPLE_MEMORY)
        self._session_bytes = 0
        self._session_max_speed = 0.0
        self._counted: dict[int, int] = {}

    def record(self, run_id: int, stats: object, *, at: float | None = None) -> None:
        """Store the latest stats for a run and fold them into the session."""
        moment = at if at is not None else time.time()
        with self._lock:
            self._latest[run_id] = stats

            # Bytes are cumulative per run, so the session total takes the
            # increment rather than the running figure, or every sample would
            # count the same bytes again.
            done = int(getattr(stats, "bytes_done", 0) or 0)
            previous = self._counted.get(run_id, 0)
            if done >= previous:
                self._session_bytes += done - previous
            self._counted[run_id] = done

            total = sum(
                float(getattr(entry, "speed", 0.0) or 0.0) for entry in self._latest.values()
            )
            self._session_max_speed = max(self._session_max_speed, total)
            self._samples.append(SpeedSample(at=moment, speed=total))

    def forget(self, run_id: int) -> None:
        """Drop a finished run, and record the drop as a sample.

        Without the extra sample the chart holds its last speed flat until the
        next one arrives, which reads as "still transferring at 60 MB/s" for
        several seconds after a sync has stopped.

        **Going idle clears the session figures.** Session here means this burst
        of activity rather than the lifetime of the process, so once nothing is
        running there is no session to report and the panel returns to zero.
        The lifetime total is unaffected: it comes from the database and is the
        figure that answers "how much has this ever moved".
        """
        with self._lock:
            self._latest.pop(run_id, None)
            self._counted.pop(run_id, None)
            total = sum(
                float(getattr(entry, "speed", 0.0) or 0.0) for entry in self._latest.values()
            )
            self._samples.append(SpeedSample(at=time.time(), speed=total))
            if not self._latest:
                self._session_bytes = 0
                self._session_max_speed = 0.0
                self._counted.clear()

    def latest(self, run_id: int) -> object | None:
        with self._lock:
            return self._latest.get(run_id)

    def active(self) -> dict[int, object]:
        with self._lock:
            return dict(self._latest)

    def samples(self, *, since_seconds: float) -> list[SpeedSample]:
        cutoff = time.time() - since_seconds
        with self._lock:
            return [sample for sample in self._samples if sample.at >= cutoff]

    def reset_session(self) -> None:
        """Zero the session figures without disturbing anything in flight.

        Going idle clears them anyway; this is for clearing them part way
        through a long run. Anything in progress keeps its baseline, so only
        bytes from here on are counted rather than the run starting again from
        its own total.
        """
        with self._lock:
            self._session_bytes = 0
            self._session_max_speed = 0.0
            self._counted = {
                run_id: int(getattr(stats, "bytes_done", 0) or 0)
                for run_id, stats in self._latest.items()
            }

    def session(self) -> tuple[int, float]:
        """Bytes transferred and the peak speed seen since this process started."""
        with self._lock:
            return self._session_bytes, self._session_max_speed


activity = ActivityRecorder()

"""Parsing rclone's machine-readable output.

Every format here was read off rclone 1.74.4 and is recorded in CLAUDE.md. Two
things in SPEC section 8 turned out to be wrong, and both are load bearing:

1. The `--combined` legend is inverted in the spec. Real behaviour for
   `rclone check SRC DST` is `+` for path1-only and `-` for path2-only, so
   following the spec would label every file about to be created as "deleted"
   in the one screen whose job is preventing accidental deletion.

   This module therefore does not parse `--combined` at all. `check` has named
   per-category outputs, `--missing-on-dst` and friends, which say what they mean
   and cannot be inverted by a reader or by a future edit.

2. `check` compares hash and size and never compares modification times, so it
   cannot reproduce what `sync` would do for an mtime-based job. It is used here
   only to establish presence, which no comparison mode affects.

The authority on what would change is `sync --dry-run --use-json-log`, whose
lines carry a machine-readable `skipped` field rather than only a message string.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# `sync --dry-run --use-json-log` marks each intended operation with this field.
# Observed values, rclone 1.74.4:
#   {"level":"notice","msg":"Skipped copy as --dry-run is set (size 14)",
#    "skipped":"copy","size":14,"object":"changed.txt","objectType":"*local.Object"}
SKIPPED_COPY = "copy"
SKIPPED_DELETE = "delete"
# With --backup-dir there is no delete at all. Verified, rclone 1.74.4:
#   {"level":"notice","msg":"Skipped move into backup dir as --dry-run is set
#    (size 4)","skipped":"move into backup dir","object":"gone.txt"}
# and live, an info line reading "Moved into backup dir". Counting only
# "delete" would report every archived file as though nothing happened to it.
SKIPPED_ARCHIVE = "move into backup dir"

# `rclone check` exits non-zero when it finds differences. That is an ordinary
# outcome for us, not a failure, so it needs distinguishing from a real error.
CHECK_EXIT_DIFFERENCES = 1


@dataclass(frozen=True)
class PlannedOperation:
    """One operation rclone said it would perform."""

    path: str
    operation: str  # SKIPPED_COPY or SKIPPED_DELETE
    size: int | None = None


@dataclass
class DryRunLog:
    """Everything useful from a `sync --dry-run --use-json-log` stream."""

    operations: list[PlannedOperation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The final stats block, when rclone emitted one.
    stats: dict[str, object] | None = None
    # True when rclone aborted because the delete threshold was reached, which
    # means the operation list is truncated and must not be presented as complete.
    max_delete_hit: bool = False

    @property
    def copies(self) -> list[PlannedOperation]:
        return [op for op in self.operations if op.operation == SKIPPED_COPY]

    @property
    def deletes(self) -> list[PlannedOperation]:
        return [op for op in self.operations if op.operation == SKIPPED_DELETE]

    @property
    def archived(self) -> list[PlannedOperation]:
        return [op for op in self.operations if op.operation == SKIPPED_ARCHIVE]

    @property
    def removals(self) -> list[PlannedOperation]:
        """Everything that left the destination, however it left.

        An archived file is still gone from the destination, so it counts against
        the delete brake and belongs in the deleted total. Where it went is a
        separate question, answered by `archived`.
        """
        return self.deletes + self.archived


def iter_json_lines(text: str) -> Iterator[dict[str, object]]:
    """Yield the JSON objects from an rclone log stream, ignoring anything else.

    rclone can interleave non-JSON lines, and a malformed line must not abort the
    parse: losing one line is far better than losing the whole plan.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            logger.debug("Ignoring unparseable rclone log line")
            continue
        if isinstance(payload, dict):
            yield payload


def parse_dry_run(text: str) -> DryRunLog:
    """Parse the JSON log of a `sync --dry-run`."""
    result = DryRunLog()
    for payload in iter_json_lines(text):
        skipped = payload.get("skipped")
        obj = payload.get("object")

        if isinstance(skipped, str) and isinstance(obj, str) and obj:
            size = payload.get("size")
            result.operations.append(
                PlannedOperation(
                    path=obj,
                    operation=skipped,
                    size=size if isinstance(size, int) and size >= 0 else None,
                )
            )
            continue

        if payload.get("stats") is not None and isinstance(payload.get("stats"), dict):
            result.stats = payload["stats"]  # type: ignore[assignment]

        level = payload.get("level")
        message = payload.get("msg")
        if level == "error" and isinstance(message, str):
            if "max-delete threshold reached" in message:
                # rclone stops at the threshold, so the plan is partial.
                result.max_delete_hit = True
            detail = f"{obj}: {message}" if isinstance(obj, str) and obj else message
            result.errors.append(detail.strip())

    return result


@dataclass
class PresenceReport:
    """Which paths exist on which side, from `rclone check`.

    Presence does not depend on hashes or modification times, so this is reliable
    for every backend pairing, including two that expose no hashes at all.
    """

    missing_on_dest: set[str] = field(default_factory=set)
    missing_on_source: set[str] = field(default_factory=set)
    differing: set[str] = field(default_factory=set)
    matching: set[str] = field(default_factory=set)
    errored: set[str] = field(default_factory=set)

    @property
    def present_on_source(self) -> set[str]:
        return self.matching | self.differing | self.missing_on_dest

    @property
    def present_on_dest(self) -> set[str]:
        return self.matching | self.differing | self.missing_on_source

    @property
    def dest_file_count(self) -> int:
        """The denominator for the delete brake.

        SPEC section 6.4 expresses the brake as a percentage, but rclone's
        --max-delete takes a count, and this is where the count becomes knowable.
        """
        return len(self.present_on_dest)


def _read_paths(path: Path) -> set[str]:
    """Read one of check's category files. A missing file means an empty category."""
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    }


def parse_presence(
    *,
    missing_on_dest: Path,
    missing_on_source: Path,
    differing: Path,
    matching: Path,
    errored: Path,
) -> PresenceReport:
    """Read the per-category files written by `rclone check`.

    Named files rather than the `--combined` symbol stream on purpose: the symbol
    legend is easy to invert, and SPEC section 8 does invert it.
    """
    return PresenceReport(
        missing_on_dest=_read_paths(missing_on_dest),
        missing_on_source=_read_paths(missing_on_source),
        differing=_read_paths(differing),
        matching=_read_paths(matching),
        errored=_read_paths(errored),
    )


def summarise_errors(lines: Iterable[str], limit: int = 20) -> list[str]:
    """Cap an error list so one pathological run cannot fill a page or a column."""
    collected = list(dict.fromkeys(line for line in lines if line))
    if len(collected) <= limit:
        return collected
    remaining = len(collected) - limit
    return [*collected[:limit], f"and {remaining} more errors"]

"""The engine interface.

An engine turns a Job into operations against two endpoints. `plan()` says what
would change without changing anything; `execute()` performs it.

Only `plan()` is implemented at M2. `execute()` is declared here so the shape is
fixed before M3 builds run supervision against it, and RcloneEngine refuses it
with a message rather than pretending.

SPEC section 2.1 keeps engine and protocol separate: an engine is how bytes move,
a protocol is what is being talked to. LftpEngine arrives at M7 if at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.config import Settings
from app.crypto import SecretBox
from app.models import ChangeAction, Job


class EngineError(Exception):
    """The engine cannot carry out the request. The message is user facing."""


@dataclass(frozen=True)
class PlannedChange:
    """One file the plan says would change."""

    action: ChangeAction
    path: str
    size: int | None = None
    message: str | None = None


@dataclass
class Plan:
    """What a run would do, without having done any of it.

    `unchanged_count` is a count rather than a list on purpose. A plan over a
    100,000 file tree with three changes would otherwise write 100,000 rows that
    nobody reads, and the summary card only needs the number.
    """

    changes: list[PlannedChange] = field(default_factory=list)
    unchanged_count: int = 0
    bytes_to_transfer: int = 0
    # Files present on the side that would be modified. The denominator for the
    # delete brake, which SPEC section 6.4 states as a percentage while rclone's
    # --max-delete takes a count.
    dest_file_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Redacted, safe to store and display. SPEC section 6.1.
    commands: list[str] = field(default_factory=list)
    # True when the plan is known to be incomplete.
    truncated: bool = False

    def count(self, action: ChangeAction) -> int:
        return sum(1 for change in self.changes if change.action == action)

    @property
    def new_count(self) -> int:
        return self.count(ChangeAction.new)

    @property
    def updated_count(self) -> int:
        return self.count(ChangeAction.updated)

    @property
    def deleted_count(self) -> int:
        return self.count(ChangeAction.deleted)

    @property
    def error_count(self) -> int:
        return len(self.errors) + self.count(ChangeAction.error)


class SyncEngine(ABC):
    """How bytes move. Chosen per job. SPEC section 2.1."""

    name: str

    @abstractmethod
    def plan(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        """Report what a run would change, modifying nothing on either endpoint."""

    @abstractmethod
    def execute(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        """Perform the sync. Implemented at M3."""

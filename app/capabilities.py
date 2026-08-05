"""Backend capability interpretation and the two-endpoint intersection.

SPEC section 5.4 is explicit that a static per-protocol feature matrix is
unmaintainable once arbitrary rclone backends are allowed, so nothing here is
hardcoded per protocol. Everything is derived from the stored probe output.

The reason strings are the product, not a nicety. SPEC 5.4 requires every
disabled option to explain itself, and M1's fourth acceptance criterion is a
hash-less backend disabling checksum comparison with a visible reason. The same
strings are reused verbatim by the job editor later, so they live here rather
than in a template.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.models import Connection, utcnow

# SPEC 5.4: treat a probe older than this as stale on the job edit screen.
STALE_PROBE_DAYS = 30

# rclone reports this Precision when a backend cannot set modification times.
# There is no file level Features flag for it. See CLAUDE.md.
MODTIME_UNSUPPORTED_PRECISION = 9223372036854775807


@dataclass(frozen=True)
class Capabilities:
    """One endpoint's capabilities, read from a stored probe."""

    hashes: frozenset[str]
    features: dict[str, bool]
    precision_ns: int
    probed_at: datetime | None

    @property
    def can_set_modtime(self) -> bool:
        return self.precision_ns != MODTIME_UNSUPPORTED_PRECISION

    @property
    def supports_move(self) -> bool:
        return bool(self.features.get("Move"))

    @property
    def supports_empty_dirs(self) -> bool:
        return bool(self.features.get("CanHaveEmptyDirectories"))

    @property
    def case_insensitive(self) -> bool:
        return bool(self.features.get("CaseInsensitive"))

    @property
    def is_bucket_based(self) -> bool:
        return bool(self.features.get("BucketBased"))

    @property
    def is_stale(self) -> bool:
        if self.probed_at is None:
            return True
        return utcnow() - self.probed_at > timedelta(days=STALE_PROBE_DAYS)

    @property
    def probed(self) -> bool:
        return self.probed_at is not None


def empty() -> Capabilities:
    return Capabilities(
        hashes=frozenset(),
        features={},
        precision_ns=MODTIME_UNSUPPORTED_PRECISION,
        probed_at=None,
    )


def from_probe(payload: dict[str, Any], probed_at: datetime | None) -> Capabilities:
    """Interpret `rclone backend features` output."""
    raw_features = payload.get("Features") or {}
    features = {
        str(key): bool(value) for key, value in raw_features.items() if isinstance(value, bool)
    }
    hashes = frozenset(str(item) for item in (payload.get("Hashes") or []))
    precision = payload.get("Precision")
    precision_ns = int(precision) if isinstance(precision, int) else MODTIME_UNSUPPORTED_PRECISION
    return Capabilities(
        hashes=hashes,
        features=features,
        precision_ns=precision_ns,
        probed_at=probed_at,
    )


def for_connection(connection: Connection) -> Capabilities:
    stored = connection.capabilities
    if not isinstance(stored, dict):
        return empty()
    return from_probe(stored, connection.capabilities_probed_at)


@dataclass(frozen=True)
class OptionAvailability:
    """Whether a job option is available, and why not when it is not."""

    available: bool
    reason: str | None = None
    # A warning is shown even when the option is available, because the option
    # works but has a cost or a hazard worth stating up front.
    warning: str | None = None


@dataclass(frozen=True)
class Intersection:
    """What a job pairing two endpoints may do."""

    checksum: OptionAvailability
    bidirectional: OptionAvailability
    archive: OptionAvailability
    empty_dirs: OptionAvailability
    shared_hashes: frozenset[str]
    warnings: tuple[str, ...]
    stale: bool

    def blocked_reasons(self) -> list[str]:
        return [
            option.reason
            for option in (self.checksum, self.bidirectional, self.archive, self.empty_dirs)
            if not option.available and option.reason
        ]


def _describe(connection: Connection) -> str:
    return connection.name


def _unprobed(connection: Connection) -> str:
    return (
        f"Capabilities for '{_describe(connection)}' are unknown because it has "
        "not been tested yet. Run Test connection to find out what it supports."
    )


def intersect(
    source: Connection,
    dest: Connection,
    *,
    source_caps: Capabilities | None = None,
    dest_caps: Capabilities | None = None,
) -> Intersection:
    """Derive job constraints from two endpoints. SPEC 5.4's table.

    Order of checks matters: an unprobed endpoint is reported as unknown rather
    than as unsupported, because telling someone SMB has no hashes when the truth
    is that nobody has looked yet sends them debugging the wrong thing.
    """
    caps_a = source_caps or for_connection(source)
    caps_b = dest_caps or for_connection(dest)
    warnings: list[str] = []

    # Checksum comparison: both endpoints must share at least one hash type.
    shared = caps_a.hashes & caps_b.hashes
    if not caps_a.probed or not caps_b.probed:
        unprobed = source if not caps_a.probed else dest
        checksum = OptionAvailability(False, _unprobed(unprobed))
    elif shared:
        checksum = OptionAvailability(True)
    else:
        hashless = [
            _describe(connection)
            for connection, caps in ((source, caps_a), (dest, caps_b))
            if not caps.hashes
        ]
        if hashless:
            names = " and ".join(hashless)
            plural = "expose" if len(hashless) > 1 else "exposes"
            checksum = OptionAvailability(
                False,
                f"Checksum comparison unavailable: {names} {plural} no hash types. "
                "Files are compared on modification time and size instead.",
            )
        else:
            checksum = OptionAvailability(
                False,
                "Checksum comparison unavailable: the two endpoints share no common "
                "hash type. Files are compared on modification time and size instead.",
            )

    # Bidirectional: both endpoints must be able to write modification times.
    if not caps_a.probed or not caps_b.probed:
        unprobed = source if not caps_a.probed else dest
        bidirectional = OptionAvailability(False, _unprobed(unprobed))
    else:
        cannot = [
            _describe(connection)
            for connection, caps in ((source, caps_a), (dest, caps_b))
            if not caps.can_set_modtime
        ]
        if cannot:
            names = " and ".join(cannot)
            bidirectional = OptionAvailability(
                False,
                f"Bidirectional sync unavailable: {names} cannot write modification "
                "times, which bisync needs to tell which side changed.",
            )
        else:
            bidirectional = OptionAvailability(True)

    # Archiving: the side being modified must support server-side Move, or the
    # archive costs a full round trip. SPEC 7.3 requires warning about that.
    if not caps_b.probed:
        archive = OptionAvailability(False, _unprobed(dest))
    elif caps_b.supports_move:
        archive = OptionAvailability(True)
    else:
        archive = OptionAvailability(
            True,
            warning=(
                f"'{_describe(dest)}' does not support server-side move, so "
                "archiving a deleted file means downloading and re-uploading it. "
                "Expect archiving to be slow and to use bandwidth."
            ),
        )

    # Empty directories: both endpoints must support them.
    if not caps_a.probed or not caps_b.probed:
        empty_dirs = OptionAvailability(False, _unprobed(source if not caps_a.probed else dest))
    else:
        cannot_empty = [
            _describe(connection)
            for connection, caps in ((source, caps_a), (dest, caps_b))
            if not caps.supports_empty_dirs
        ]
        if cannot_empty:
            names = " and ".join(cannot_empty)
            empty_dirs = OptionAvailability(
                False,
                f"Preserving empty directories unavailable: {names} does not support them.",
            )
        else:
            empty_dirs = OptionAvailability(True)

    # SPEC 10.7: a case sensitive endpoint paired with a case insensitive one
    # produces a sync that never converges, and the symptom is baffling.
    if caps_a.probed and caps_b.probed and caps_a.case_insensitive != caps_b.case_insensitive:
        insensitive = source if caps_a.case_insensitive else dest
        sensitive = dest if caps_a.case_insensitive else source
        warnings.append(
            f"'{_describe(insensitive)}' is case insensitive but "
            f"'{_describe(sensitive)}' is case sensitive. Two files differing only "
            "in case will collide, and the sync may never settle."
        )

    if archive.warning:
        warnings.append(archive.warning)

    for connection, caps in ((source, caps_a), (dest, caps_b)):
        if caps.probed and caps.is_stale:
            warnings.append(
                f"Capabilities for '{_describe(connection)}' were last checked more "
                f"than {STALE_PROBE_DAYS} days ago. Test the connection again to refresh them."
            )

    return Intersection(
        checksum=checksum,
        bidirectional=bidirectional,
        archive=archive,
        empty_dirs=empty_dirs,
        shared_hashes=shared,
        warnings=tuple(warnings),
        stale=(caps_a.probed and caps_a.is_stale) or (caps_b.probed and caps_b.is_stale),
    )

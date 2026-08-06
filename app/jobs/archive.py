"""Deletion archiving: where a removed file goes instead of away.

SPEC section 7, corrected against rclone 1.74.4 where it was wrong.

**The spec's stated failure mode does not happen.** Section 7.1 says an archive
inside the sync destination makes rclone "see the archive as extra files on the
destination and delete or re-archive them on the next run". Verified: rclone
refuses outright with

    Failed to sync: destination and parameter to --backup-dir mustn't overlap

The instruction is still right, for a different reason. Without an exclude the
job simply will not run; with a matching exclude it works and is stable across
repeated runs. So the exclude is not a guard against silent corruption, it is
what makes a child archive possible at all.

**The sibling default is unusable at a share or bucket root.** For a destination
of `remote:Share/media` the sibling is `remote:Share/media.hivesync-archive`,
which is fine. For `remote:Share` it is `remote:Share.hivesync-archive`, a
*different share*, which does not exist and which rclone cannot create. Verified
against a real SMB server: rclone does not fail fast, it hangs retrying. This is
not a Synology quirk; no SMB server has a sibling of a share root. A destination
with no parent therefore archives into a child, with the exclude injected.

Archived deletions still count against `--max-delete`: with a brake of two and
ten files to remove, rclone archived two and aborted. Archiving does not smuggle
deletions past the brake.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from app.models import ArchiveLayout, Job, utcnow

logger = logging.getLogger(__name__)

ARCHIVE_SUFFIX = ".hivesync-archive"
CHILD_ARCHIVE_NAME = ".hivesync-archive"


class ArchiveError(ValueError):
    """An archive configuration that cannot work. The message is user facing."""


def slugify(name: str) -> str:
    """A job name reduced to something safe as a single path element.

    Guarantees: no separators, and never "." or ".." or anything containing a
    run of dots, so it cannot be read as a traversal wherever it is joined.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip())
    # Collapse dot runs, which is what would otherwise leave ".." intact.
    cleaned = re.sub(r"\.{2,}", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return cleaned.lower() or "job"


def _split_remote(spec: str) -> tuple[str, str]:
    """Split `remote:path` into its remote and path halves.

    A local path has no remote, and a Windows style drive letter is not a remote,
    so only a colon after at least two characters counts.
    """
    match = re.match(r"^([A-Za-z0-9_.\-]{2,}):(.*)$", spec)
    if match:
        return match.group(1), match.group(2)
    return "", spec


@dataclass(frozen=True)
class ArchivePlan:
    """Where deletions go, and what has to be excluded to make that work."""

    base: str
    # The directory actually handed to --backup-dir for this run, which for the
    # timestamped layout includes the job and run stamp.
    backup_dir: str
    # Injected when the archive sits inside the synced tree. Without it rclone
    # refuses the run entirely.
    exclude: str | None
    suffix: str | None
    inside_destination: bool


def default_base(destination: str) -> str:
    """Where an archive goes when the operator has not chosen.

    A sibling of the sync root, per SPEC 7.1, except when the root has no parent.
    A share or bucket root has no sibling that can be created, so the archive
    becomes a child and the caller injects the exclude.
    """
    remote, path = _split_remote(destination)
    # An absolute path stays absolute. A `remote:` prefix does not make the path
    # relative: every local endpoint is addressed as `alias:/absolute/path`, and
    # dropping the slash silently rebases the archive on rclone's working
    # directory. Found the hard way, with archived files landing under the
    # application's own directory instead of beside the destination.
    absolute = path.startswith("/")
    trimmed = path.strip("/")
    prefix = f"{remote}:" if remote else ""

    if not trimmed:
        # The whole remote, or the filesystem root. Nothing to be a sibling of.
        return f"{prefix}/{CHILD_ARCHIVE_NAME}" if absolute else f"{prefix}{CHILD_ARCHIVE_NAME}"

    if not absolute and len(trimmed.split("/")) == 1:
        # A share or bucket root: `remote:Share`. Its sibling would be another
        # share, which does not exist and cannot be created. Verified against a
        # real SMB server, where rclone hangs retrying rather than failing.
        # An absolute path is exempt: `/data` does have a creatable sibling.
        return f"{prefix}{trimmed}/{CHILD_ARCHIVE_NAME}"

    sibling = f"{trimmed}{ARCHIVE_SUFFIX}"
    return f"{prefix}/{sibling}" if absolute else f"{prefix}{sibling}"


def qualify(archive_base: str, destination: str) -> str:
    """Read an operator-entered archive path against the destination's remote.

    The operator types a path, not an rclone spec: they see "/mnt/tank/archive"
    or "Media/archive", never the synthetic `hs_dst:` alias this process invents
    for the run. A bare path therefore means "on the same connection", which is
    the only thing SPEC 7.1 allows anyway. A path that does name a remote keeps
    it, so naming the wrong one is still refused rather than quietly redirected.
    """
    remote, _ = _split_remote(destination)
    base_remote, _ = _split_remote(archive_base)
    if remote and not base_remote:
        return f"{remote}:{archive_base}"
    return archive_base


def is_inside(archive_base: str, destination: str) -> bool:
    """Whether the archive sits within the tree being synced."""
    archive_remote, archive_path = _split_remote(archive_base)
    dest_remote, dest_path = _split_remote(destination)
    if archive_remote != dest_remote:
        return False
    archive_norm = archive_path.strip("/")
    dest_norm = dest_path.strip("/")
    if not dest_norm:
        return bool(archive_norm)
    return archive_norm == dest_norm or archive_norm.startswith(dest_norm + "/")


def exclude_for(archive_base: str, destination: str) -> str | None:
    """The filter that lets a child archive work at all.

    Anchored to the root of the synced tree so it cannot match a
    similarly named directory deeper in.
    """
    if not is_inside(archive_base, destination):
        return None
    _, archive_path = _split_remote(archive_base)
    _, dest_path = _split_remote(destination)
    relative = archive_path.strip("/")
    prefix = dest_path.strip("/")
    if prefix and relative == prefix:
        relative = ""
    elif prefix and relative.startswith(prefix + "/"):
        relative = relative[len(prefix) + 1 :]
    relative = relative.strip("/")
    if not relative:
        raise ArchiveError(
            "The archive path is the same as the destination, so every file "
            "would be archived into the tree it came from. Choose a different path."
        )
    return f"/{relative}/**"


def validate(job: Job, destination: str, archive_base: str) -> None:
    """Refuse an archive configuration that cannot work, at save time.

    SPEC 7.1 requires the archive be on the same remote and the same share or
    bucket as the side being modified: a cross-share move is not server side and
    may silently fall back to copy plus delete.
    """
    archive_remote, archive_path = _split_remote(archive_base)
    dest_remote, dest_path = _split_remote(destination)

    if archive_remote != dest_remote:
        raise ArchiveError(
            "The archive must be on the same connection as the side being "
            f"changed. The destination is on '{dest_remote or 'the local disk'}' "
            f"and the archive is on '{archive_remote or 'the local disk'}'."
        )

    if archive_remote:
        archive_share = archive_path.strip("/").split("/")[0] if archive_path.strip("/") else ""
        dest_share = dest_path.strip("/").split("/")[0] if dest_path.strip("/") else ""
        if archive_share != dest_share:
            raise ArchiveError(
                f"The archive must be on the same share as the destination. The "
                f"destination is under '{dest_share}' and the archive is under "
                f"'{archive_share}'. Moving between shares is not a server side "
                "operation, so it would copy and delete every archived file "
                "instead of renaming it."
            )

    if is_inside(archive_base, destination):
        # Allowed, but only because the exclude is injected. Confirm it resolves.
        exclude_for(archive_base, destination)


def plan_for(job: Job, destination: str, *, now: datetime | None = None) -> ArchivePlan:
    """Resolve everything a run needs to archive its deletions."""
    chosen = (job.archive_base or "").strip()
    base = qualify(chosen, destination) if chosen else default_base(destination)
    validate(job, destination, base)

    stamp = (now or utcnow()).strftime("%Y-%m-%dT%H-%M-%SZ")
    exclude = exclude_for(base, destination)

    if job.archive_layout == ArchiveLayout.suffix:
        # SPEC 7.2: everything into the base, distinguished by a suffix.
        return ArchivePlan(
            base=base,
            backup_dir=base,
            exclude=exclude,
            suffix=f".{stamp}",
            inside_destination=exclude is not None,
        )

    # SPEC 7.2 default: <base>/<job-slug>/<run timestamp>/<relative path>.
    return ArchivePlan(
        base=base,
        backup_dir=f"{base.rstrip('/')}/{slugify(job.name)}/{stamp}",
        exclude=exclude,
        suffix=None,
        inside_destination=exclude is not None,
    )


def sync_args(plan: ArchivePlan) -> list[str]:
    """Flags for a one way sync. The exclude comes first so it wins."""
    args: list[str] = []
    if plan.exclude:
        args += ["--exclude", plan.exclude]
    args += ["--backup-dir", plan.backup_dir]
    if plan.suffix:
        args += ["--suffix", plan.suffix, "--suffix-keep-extension"]
    return args


def bisync_args(plan1: ArchivePlan, plan2: ArchivePlan) -> list[str]:
    """Flags for bisync, which archives each side locally.

    SPEC 7.2: never archive across remotes. rclone says the same, requiring each
    backup dir to be a non-overlapping path on the same remote as its path.
    """
    args: list[str] = []
    for plan in (plan1, plan2):
        if plan.exclude:
            args += ["--exclude", plan.exclude]
    args += ["--backup-dir1", plan1.backup_dir, "--backup-dir2", plan2.backup_dir]
    if plan1.suffix:
        args += ["--suffix", plan1.suffix, "--suffix-keep-extension"]
    return args

"""Archive path computation and validation.

The share-root case is the one that matters most. `remote:Share` has no sibling
on any SMB server, and rclone hangs retrying rather than failing when asked to
use one, so the default has to notice and archive into a child instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.jobs import archive
from app.jobs.archive import ArchiveError
from app.models import ArchiveLayout, DeleteMode, Job

AT = datetime(2026, 8, 5, 2, 30, 0, tzinfo=UTC)


def _job(**overrides: object) -> Job:
    job = Job(
        name="Nightly Media",
        delete_mode=DeleteMode.archive,
        archive_layout=ArchiveLayout.timestamped_dir,
        filters={},
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


# --------------------------------------------------------------------------
# Default placement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        # A path with a parent gets a sibling, per SPEC 7.1.
        ("synology:Media/photos", "synology:Media/photos.hivesync-archive"),
        ("synology:Media/a/b/c", "synology:Media/a/b/c.hivesync-archive"),
        ("/data/media", "/data/media.hivesync-archive"),
        ("/data/a/b", "/data/a/b.hivesync-archive"),
    ],
)
def test_sibling_is_used_when_there_is_a_parent(destination: str, expected: str) -> None:
    assert archive.default_base(destination) == expected


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        # Every local endpoint is addressed as alias:/absolute/path. Dropping the
        # leading slash rebases the archive on rclone's working directory, which
        # is how archived files first ended up inside the application directory
        # instead of beside the destination.
        ("hs_dst:/srv/media", "hs_dst:/srv/media.hivesync-archive"),
        ("hs_dst:/srv/a/b", "hs_dst:/srv/a/b.hivesync-archive"),
        # An absolute single element is not a share root: / is a real parent.
        ("hs_dst:/data", "hs_dst:/data.hivesync-archive"),
    ],
)
def test_an_absolute_path_under_a_remote_stays_absolute(destination: str, expected: str) -> None:
    assert archive.default_base(destination) == expected


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        # A share root has no sibling that can exist. Verified against real SMB:
        # rclone hangs retrying against the nonexistent share rather than failing.
        ("synology:Media", "synology:Media/.hivesync-archive"),
        ("nas:backups", "nas:backups/.hivesync-archive"),
        # The whole remote, with no path at all.
        ("synology:", "synology:.hivesync-archive"),
    ],
)
def test_share_root_falls_back_to_a_child(destination: str, expected: str) -> None:
    assert archive.default_base(destination) == expected


def test_a_child_default_carries_an_exclude() -> None:
    """Without it rclone refuses the run outright: destination and parameter to
    --backup-dir mustn't overlap."""
    destination = "synology:Media"
    base = archive.default_base(destination)
    assert archive.exclude_for(base, destination) == "/.hivesync-archive/**"


def test_a_sibling_default_needs_no_exclude() -> None:
    destination = "synology:Media/photos"
    base = archive.default_base(destination)
    assert archive.exclude_for(base, destination) is None


# --------------------------------------------------------------------------
# Overlap detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "destination", "inside"),
    [
        ("synology:Media/photos/.arc", "synology:Media/photos", True),
        ("synology:Media/photos", "synology:Media/photos", True),
        ("synology:Media/photos.hivesync-archive", "synology:Media/photos", False),
        # A prefix match that is not a path boundary must not count.
        ("synology:Media/photos-old", "synology:Media/photos", False),
        ("other:Media/photos/.arc", "synology:Media/photos", False),
    ],
)
def test_overlap_detection(base: str, destination: str, inside: bool) -> None:
    assert archive.is_inside(base, destination) is inside


def test_exclude_is_anchored_to_the_sync_root() -> None:
    """Anchored, so it cannot match a similarly named directory further down."""
    pattern = archive.exclude_for("synology:Media/photos/.arc", "synology:Media/photos")
    assert pattern == "/.arc/**"
    assert pattern.startswith("/")


def test_archive_equal_to_the_destination_is_refused() -> None:
    with pytest.raises(ArchiveError, match="same as the destination"):
        archive.exclude_for("synology:Media/photos", "synology:Media/photos")


# --------------------------------------------------------------------------
# Validation at save time
# --------------------------------------------------------------------------


def test_a_different_connection_is_refused() -> None:
    with pytest.raises(ArchiveError, match="same connection"):
        archive.validate(_job(), "synology:Media/photos", "backup:Archive")


def test_a_different_share_is_refused_with_the_reason() -> None:
    """SPEC 7.1: a cross-share move is not server side and would copy plus delete
    every archived file instead of renaming it."""
    with pytest.raises(ArchiveError) as excinfo:
        archive.validate(_job(), "synology:Media/photos", "synology:Backups/arc")
    message = str(excinfo.value)
    assert "same share" in message
    assert "server side" in message


def test_the_same_share_is_allowed() -> None:
    archive.validate(_job(), "synology:Media/photos", "synology:Media/archive")


def test_local_paths_are_not_treated_as_remotes() -> None:
    archive.validate(_job(), "/data/media", "/data/media.hivesync-archive")


# --------------------------------------------------------------------------
# Layouts
# --------------------------------------------------------------------------


def test_timestamped_layout_nests_job_and_run() -> None:
    """SPEC 7.2: <base>/<job-slug>/<run timestamp>/<original relative path>."""
    plan = archive.plan_for(_job(), "synology:Media/photos", now=AT)
    assert plan.backup_dir == (
        "synology:Media/photos.hivesync-archive/nightly-media/2026-08-05T02-30-00Z"
    )
    assert plan.suffix is None
    assert archive.sync_args(plan)[:2] == ["--backup-dir", plan.backup_dir]


def test_suffix_layout_uses_the_base_and_a_stamp() -> None:
    plan = archive.plan_for(
        _job(archive_layout=ArchiveLayout.suffix), "synology:Media/photos", now=AT
    )
    assert plan.backup_dir == "synology:Media/photos.hivesync-archive"
    assert plan.suffix == ".2026-08-05T02-30-00Z"
    args = archive.sync_args(plan)
    assert "--suffix" in args
    assert "--suffix-keep-extension" in args


def test_a_child_archive_puts_the_exclude_first() -> None:
    """The exclude has to be in effect for the run to be accepted at all."""
    plan = archive.plan_for(_job(), "synology:Media", now=AT)
    args = archive.sync_args(plan)
    assert args[0] == "--exclude"
    assert args[1] == "/.hivesync-archive/**"
    assert plan.inside_destination is True


def test_an_explicit_base_overrides_the_default() -> None:
    plan = archive.plan_for(
        _job(archive_base="synology:Media/attic"), "synology:Media/photos", now=AT
    )
    assert plan.base == "synology:Media/attic"
    assert plan.backup_dir.startswith("synology:Media/attic/nightly-media/")


# --------------------------------------------------------------------------
# An operator types a path, not an rclone spec
# --------------------------------------------------------------------------


def test_a_typed_path_is_read_against_the_destination_remote() -> None:
    """The operator never sees the synthetic hs_dst alias, so a bare path has to
    mean "on the same connection" rather than "on the local disk"."""
    plan = archive.plan_for(_job(archive_base="/srv/attic"), "hs_dst:/srv/media", now=AT)
    assert plan.base == "hs_dst:/srv/attic"
    assert plan.backup_dir == "hs_dst:/srv/attic/nightly-media/2026-08-05T02-30-00Z"


def test_a_typed_path_inside_the_destination_still_gets_its_exclude() -> None:
    plan = archive.plan_for(_job(archive_base="/srv/media/.attic"), "hs_dst:/srv/media", now=AT)
    assert plan.exclude == "/.attic/**"
    assert plan.inside_destination is True


def test_naming_a_different_remote_is_still_refused() -> None:
    """Qualifying a bare path must not turn into rewriting an explicit one."""
    with pytest.raises(ArchiveError, match="same connection"):
        archive.plan_for(_job(archive_base="backup:Archive"), "hs_dst:/srv/media", now=AT)


def test_qualify_leaves_an_unprefixed_destination_alone() -> None:
    assert archive.qualify("/data/attic", "/data/media") == "/data/attic"


def test_job_names_become_safe_path_elements() -> None:
    """The slug is joined into a path, so it must never be a separator or a
    traversal however the job was named."""
    assert archive.slugify("Nightly Media") == "nightly-media"
    assert archive.slugify("  ") == "job"
    for hostile in ("weird/../name", "..", "../..", "a/b/c", "...", "./."):
        slug = archive.slugify(hostile)
        assert "/" not in slug
        assert ".." not in slug
        assert slug not in (".", "..", "")


def test_bisync_archives_each_side_locally() -> None:
    """SPEC 7.2: never archive across remotes."""
    job = _job()
    plan1 = archive.plan_for(job, "hs_src:Media/one", now=AT)
    plan2 = archive.plan_for(job, "hs_dst:Media/two", now=AT)
    args = archive.bisync_args(plan1, plan2)
    assert args[args.index("--backup-dir1") + 1].startswith("hs_src:")
    assert args[args.index("--backup-dir2") + 1].startswith("hs_dst:")

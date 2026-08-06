"""Parsing rclone output.

The samples below are captured verbatim from rclone 1.74.4, not written from the
documentation. SPEC section 8's description of `--combined` is wrong, so a test
built from the spec would have locked the wrong behaviour in.
"""

from __future__ import annotations

from pathlib import Path

from app.engines import parsers

# Captured from: rclone sync /tmp/src /tmp/dst --dry-run --use-json-log -v
DRY_RUN_SAMPLE = """
{"time":"2026-08-05T18:39:50.002656Z","level":"notice","msg":"Skipped copy as --dry-run is set (size 14)","skipped":"copy","size":14,"object":"changed.txt","objectType":"*local.Object","source":"operations/operations.go:2629"}
{"time":"2026-08-05T18:39:50.002732Z","level":"notice","msg":"Skipped copy as --dry-run is set (size 4)","skipped":"copy","size":4,"object":"new.txt","objectType":"*local.Object","source":"operations/operations.go:2629"}
{"time":"2026-08-05T18:39:50.002903Z","level":"notice","msg":"Skipped delete as --dry-run is set (size 5)","skipped":"delete","size":5,"object":"deleted.txt","objectType":"*local.Object","source":"operations/operations.go:2629"}
{"time":"2026-08-05T18:39:50.003100Z","level":"notice","msg":"stats","stats":{"bytes":18,"checks":3,"deletes":1},"source":"accounting/stats.go:526"}
"""

MAX_DELETE_SAMPLE = """
{"time":"2026-08-05T18:41:13.0Z","level":"notice","msg":"Skipped delete as --dry-run is set (size 2)","skipped":"delete","size":2,"object":"del1.txt"}
{"time":"2026-08-05T18:41:13.1Z","level":"error","msg":"Got fatal error on delete: --max-delete threshold reached","object":"del2.txt"}
"""


def test_parses_copies_and_deletes_from_the_skipped_field() -> None:
    """`skipped` is machine readable. The message string is not parsed."""
    log = parsers.parse_dry_run(DRY_RUN_SAMPLE)
    assert {op.path for op in log.copies} == {"changed.txt", "new.txt"}
    assert {op.path for op in log.deletes} == {"deleted.txt"}


def test_sizes_are_captured() -> None:
    log = parsers.parse_dry_run(DRY_RUN_SAMPLE)
    sizes = {op.path: op.size for op in log.operations}
    assert sizes["changed.txt"] == 14
    assert sizes["new.txt"] == 4


def test_stats_block_is_captured() -> None:
    log = parsers.parse_dry_run(DRY_RUN_SAMPLE)
    assert log.stats is not None
    assert log.stats["bytes"] == 18


def test_max_delete_abort_is_detected() -> None:
    """rclone stops at the threshold even during a dry run, so the plan is
    partial and must not be presented as complete."""
    log = parsers.parse_dry_run(MAX_DELETE_SAMPLE)
    assert log.max_delete_hit is True
    assert len(log.deletes) == 1
    assert any("max-delete" in error for error in log.errors)


def test_non_json_lines_are_ignored() -> None:
    log = parsers.parse_dry_run(
        "2026/08/05 NOTICE: some plain text line\n" + DRY_RUN_SAMPLE + "\nnot json at all\n"
    )
    assert len(log.operations) == 3


def test_a_malformed_line_does_not_lose_the_rest() -> None:
    """Losing one line is much better than losing the whole plan."""
    log = parsers.parse_dry_run('{"broken": \n' + DRY_RUN_SAMPLE)
    assert len(log.operations) == 3


def test_empty_input_is_not_an_error() -> None:
    log = parsers.parse_dry_run("")
    assert log.operations == []
    assert log.max_delete_hit is False


def _write(directory: Path, name: str, paths: list[str]) -> Path:
    path = directory / name
    path.write_text("".join(f"{item}\n" for item in paths), encoding="utf-8")
    return path


def test_presence_from_named_category_files(tmp_path: Path) -> None:
    """Named outputs, not the --combined symbol stream.

    SPEC section 8 states `-` means path1-only and `+` path2-only. Verified
    against rclone 1.74.4, it is the other way round, so reading symbols would
    have swapped "will be created" and "will be deleted" in the review table.
    """
    report = parsers.parse_presence(
        missing_on_dest=_write(tmp_path, "mod", ["new.txt", "sub/nested.txt"]),
        missing_on_source=_write(tmp_path, "mos", ["deleted.txt"]),
        differing=_write(tmp_path, "diff", ["changed.txt"]),
        matching=_write(tmp_path, "match", ["identical.txt"]),
        errored=_write(tmp_path, "err", []),
    )
    assert report.missing_on_dest == {"new.txt", "sub/nested.txt"}
    assert report.missing_on_source == {"deleted.txt"}
    assert report.present_on_source == {
        "new.txt",
        "sub/nested.txt",
        "changed.txt",
        "identical.txt",
    }
    assert report.present_on_dest == {"deleted.txt", "changed.txt", "identical.txt"}


def test_dest_file_count_is_the_delete_brake_denominator(tmp_path: Path) -> None:
    report = parsers.parse_presence(
        missing_on_dest=_write(tmp_path, "mod", ["a"]),
        missing_on_source=_write(tmp_path, "mos", ["b", "c"]),
        differing=_write(tmp_path, "diff", ["d"]),
        matching=_write(tmp_path, "match", ["e", "f"]),
        errored=_write(tmp_path, "err", []),
    )
    # b, c, d, e, f exist on the destination. "a" does not.
    assert report.dest_file_count == 5


def test_missing_category_file_is_an_empty_category(tmp_path: Path) -> None:
    """rclone writes nothing when a category is empty, so absence is normal."""
    report = parsers.parse_presence(
        missing_on_dest=tmp_path / "absent",
        missing_on_source=tmp_path / "absent2",
        differing=tmp_path / "absent3",
        matching=tmp_path / "absent4",
        errored=tmp_path / "absent5",
    )
    assert report.dest_file_count == 0
    assert report.present_on_source == set()


def test_error_summary_is_capped() -> None:
    capped = parsers.summarise_errors([f"error {index}" for index in range(50)], limit=5)
    assert len(capped) == 6
    assert "45 more errors" in capped[-1]


def test_error_summary_deduplicates() -> None:
    assert parsers.summarise_errors(["same", "same", "other"]) == ["same", "other"]


# Captured from: rclone sync /tmp/s /tmp/d --backup-dir /tmp/arc --dry-run
# --use-json-log -v. There is no "delete" line at all when archiving.
ARCHIVE_DRY_RUN_SAMPLE = """
{"time":"2026-08-05T23:05:21.463155Z","level":"notice","msg":"Skipped move into backup dir as --dry-run is set (size 4)","skipped":"move into backup dir","size":4,"object":"gone.txt","objectType":"*local.Object","source":"operations/operations.go:2629"}
"""


def test_an_archived_file_is_a_removal_not_a_delete() -> None:
    """The whole reason parsers knows about archiving: a run with --backup-dir
    reports "move into backup dir", so counting only "delete" reports an
    archiving job as having done nothing to the file."""
    log = parsers.parse_dry_run(ARCHIVE_DRY_RUN_SAMPLE)
    assert [op.path for op in log.archived] == ["gone.txt"]
    assert log.deletes == []
    # It still left the destination, so it still counts against the brake.
    assert [op.path for op in log.removals] == ["gone.txt"]
    assert log.archived[0].size == 4


def test_a_plain_delete_is_not_counted_as_archived() -> None:
    log = parsers.parse_dry_run(DRY_RUN_SAMPLE)
    assert log.archived == []
    assert [op.path for op in log.removals] == ["deleted.txt"]

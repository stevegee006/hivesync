"""Planning a bidirectional job.

The output samples here are captured verbatim from rclone 1.74.4 rather than
written by hand, because the whole module is a parser for wording that only the
real binary defines. `tests/test_bisync_integration.py` runs the same paths
against the live fixtures.
"""

from __future__ import annotations

from app.engines import bisync

# --------------------------------------------------------------------------
# Planning a bidirectional job
#
# Until this existed, a dry run of a bidirectional job was refused, so the one
# direction that can damage both copies was the one you could not preview.
# --------------------------------------------------------------------------

# Captured verbatim from rclone 1.74.4 with the flags the application actually
# passes, which include --use-json-log. An earlier version of this sample was
# the plain text a human sees at a terminal; the parser passed against it and
# failed against the real thing, capturing the JSON tail as part of the
# filename. The format under test has to be the format that ships.
DRY_RUN_OUTPUT = """
{"time":"2026-08-10T10:20:19.159172661-06:00","level":"notice","msg":"- Path1             File was deleted                            - willgo.txt","source":"bisync/deltas.go:188"}
{"time":"2026-08-10T10:20:19.159209158-06:00","level":"notice","msg":"- Path1             File is new                                 - new1.txt","source":"bisync/deltas.go:259"}
{"time":"2026-08-10T10:20:19.159210000-06:00","level":"notice","msg":"- Path1             File changed: size (larger), time (newer)   - docs/mod1.txt","source":"bisync/deltas.go:259"}
{"time":"2026-08-10T10:20:19.159227957-06:00","level":"info","msg":"Path1:    3 changes:    1 new,    1 modified,    1 deleted","source":"bisync/deltas.go:126"}
{"time":"2026-08-10T10:20:19.159271755-06:00","level":"notice","msg":"- Path2             File is new                                 - new2.txt","source":"bisync/deltas.go:259"}
{"time":"2026-08-10T10:20:19.159303753-06:00","level":"info","msg":"Path2:    1 changes:    1 new,    0 modified,    0 deleted","source":"bisync/deltas.go:126"}
{"time":"2026-08-10T10:20:19.159400000-06:00","level":"notice","msg":"- Path1             Queue copy to Path2                         - /tmp/b2/new1.txt","source":"bisync/operations.go:1"}
{"time":"2026-08-10T10:20:19.159500000-06:00","level":"notice","msg":"new1.txt: Skipped copy as --dry-run is set (size 17)","source":"operations/operations.go:1"}
"""


def test_each_side_of_a_dry_run_is_classified() -> None:
    changes = bisync.parse_planned_changes(DRY_RUN_OUTPUT)

    # The side is where the change lands, not where it was detected: a
    # difference seen on Path1 is applied to Path2. See _LANDS_ON.
    assert [(c.side.value, c.action.value, c.path) for c in changes] == [
        ("dest", "deleted", "willgo.txt"),
        ("dest", "new", "new1.txt"),
        ("dest", "updated", "docs/mod1.txt"),
        ("source", "new", "new2.txt"),
    ]


def test_the_queue_lines_are_not_counted_twice() -> None:
    """They repeat the same changes as absolute destination paths. Parsing both
    would double every figure on the summary cards."""
    changes = bisync.parse_planned_changes(DRY_RUN_OUTPUT)

    # The Queue lines carry absolute destination paths; the diff lines carry
    # relative ones, and relative is what belongs on a screen.
    assert all(not c.path.startswith("/") for c in changes)
    assert len(changes) == 4


def test_what_changed_about_a_modified_file_is_kept() -> None:
    """rclone says which attributes moved. Flattening every variant to "updated"
    throws away the only clue about why a file is being copied."""
    changes = bisync.parse_planned_changes(DRY_RUN_OUTPUT)
    modified = [c for c in changes if c.action.value == "updated"]

    assert modified[0].message == "File changed: size (larger), time (newer)"


def test_a_path_with_spaces_survives() -> None:
    line = (
        '{"level":"notice","msg":"- Path2             File is new'
        '                                 - My Films/The Long One.mkv",'
        '"source":"bisync/deltas.go:259"}'
    )
    changes = bisync.parse_planned_changes(line)

    assert [c.path for c in changes] == ["My Films/The Long One.mkv"]


def test_unrecognised_wording_costs_detail_not_the_whole_plan() -> None:
    """A format change should degrade the preview, not block every bidirectional
    job. The per-side totals come from a separate line and still parse."""
    changed = DRY_RUN_OUTPUT.replace("File is new", "File is brand spanking new")

    changes = bisync.parse_planned_changes(changed)
    deltas = bisync.parse_deltas(changed)

    assert len(changes) == 2  # the deleted and the modified lines still match
    assert deltas.path1_new == 1  # and the totals are untouched
    assert deltas.path2_new == 1


def test_the_json_tail_is_not_mistaken_for_the_filename() -> None:
    """The bug this sample exists to prevent. The command passes
    --use-json-log, so the line to match is inside `msg`; matching the raw JSON
    line captured `","source":"bisync/deltas.go:259"}` as part of the path."""
    changes = bisync.parse_planned_changes(DRY_RUN_OUTPUT)

    assert all('"' not in c.path for c in changes), [c.path for c in changes]
    assert all("source" not in c.path for c in changes)


def test_plain_text_output_still_parses() -> None:
    """What a person sees running the command by hand. Not what the application
    receives, but the parser should not care."""
    line = (
        "2026/08/10 09:55:55 NOTICE: - Path1             File is new"
        "                                 - typed-by-hand.txt"
    )

    changes = bisync.parse_planned_changes(line)

    assert [c.path for c in changes] == ["typed-by-hand.txt"]


def test_a_change_is_recorded_against_the_side_it_lands_on() -> None:
    """`JobRunChange.side` means where a change lands, which is what
    `rclone.side_for` documents and the one way planner writes. bisync names the
    side a difference was *detected* on, and those are opposites: a file new on
    Path1 gets copied to Path2. Recording the detected side would give the
    column two meanings depending on which engine wrote the row.
    """
    changes = bisync.parse_planned_changes(DRY_RUN_OUTPUT)
    landing = {c.path: c.side.value for c in changes}

    # Detected on Path1, so it is copied to Path2, which is the destination.
    assert landing["new1.txt"] == "dest"
    assert landing["docs/mod1.txt"] == "dest"
    assert landing["willgo.txt"] == "dest"
    # Detected on Path2, so it travels the other way.
    assert landing["new2.txt"] == "source"


def test_no_message_that_quotes_the_safety_abort_leaves_the_alias_in() -> None:
    """Three places surface rclone's abort text: the dry run warning, the live
    pre-flight and the post-run failure. Fixing one of them left the alias on
    screen for the other two, which is what a reader actually saw.

    Reads the source rather than rendering, because the point is that every
    site goes through the one implementation.
    """
    import pathlib

    for path in ("app/engines/bisync.py", "app/jobs/runner.py"):
        body = pathlib.Path(path).read_text(encoding="utf-8")
        assert "{deltas.safety_abort}" not in body, (
            f"{path} interpolates the raw abort text, so the synthetic run alias "
            "reaches the screen. Use prepared.readable()."
        )


def test_the_alias_strip_handles_the_hash_suffix_rclone_adds() -> None:
    """rclone appends {hash} to a remote defined by environment variables, so
    matching the bare alias removed nothing at all."""
    from app.engines.rcloneconf import Endpoint, Prepared, Redactor

    endpoint = Endpoint(alias="hs_src", path="/x", env={}, redactable=(), uses_user_config=False)
    prepared = Prepared(endpoints={"hs_src": endpoint}, env={}, base_args=[], redactor=Redactor(()))

    suffixed = 'too many deletes on Path1 "hs_src{Eidmi}:Backups/old/source/".'
    bare = 'too many deletes on Path1 "hs_src:Backups/old/source/".'

    assert prepared.readable(suffixed) == 'too many deletes on Path1 "Backups/old/source/".'
    assert prepared.readable(bare) == 'too many deletes on Path1 "Backups/old/source/".'

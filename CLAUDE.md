# CLAUDE.md

Standing instructions for work on this repository. `SPEC.md` is the requirements document. This file is the working agreement plus the running record of decisions and gotchas. Keep it current.

## What this project is

A Dockerized file sync orchestrator with a web UI. It does not implement file transfer protocols. It orchestrates `rclone` (and optionally `lftp`) as subprocesses, adding scheduling, credential management, dry-run previews, deletion archiving, and a UI.

Read `SPEC.md` before starting any milestone.

## Current state

| Field | Value |
|---|---|
| Milestone in progress | none, M8 not started |
| Milestones complete | M0, M1, M2, M3, M4, M5, M6, M7 |
| Pinned rclone version | 1.74.4, by SHA256 digest, see `versions.env` |
| Observed lftp version | 4.9.2 (Debian trixie, verified in the built image 2026-08-05) |
| Config generation method | Env vars, `RCLONE_CONFIG_<NAME>_<KEY>`, verified in 1.74.4. Secrets obscured via `rclone obscure -` on stdin. No temp file, see below |
| Capability probe method | `rclone backend features <remote>:`, verified present in 1.74.4 with a stable JSON shape. No fallback needed |

The lftp row says "observed" rather than "pinned" on purpose. Debian trixie ships
one lftp and only security patches it, so an exact apt version pin turns every
future point release into a build failure for no safety gain. The base image gets
pinned by digest at M8, which pins the apt snapshot properly.

### M0 verification status: acceptance criteria met

Host:

- 71 unit tests pass, `ruff check`, `ruff format --check` and `mypy app` are clean
- `alembic upgrade head` produces exactly the declarative models, asserted by
  `tests/test_migrations.py`

Container, `docker compose up`, verified 2026-08-05:

- Image builds for amd64 and arm64. rclone digest check passes.
- `/api/health` returns `status: ok` with `rclone 1.74.4` (`matches_expected: true`)
  and `lftp 4.9.2`. **This is M0's acceptance criterion.**
- `/login` serves a styled login page, 200. `/` redirects unauthenticated to
  `/login?next=/`. Tailwind CSS and both vendored scripts serve from `/static`.
- Full auth flow: bootstrap login, forced password change, dashboard, and a wrong
  password rejected.
- Container HEALTHCHECK reports `healthy`.
- Startup order is correct: alembic upgrade, then key fingerprint, then admin
  creation, then uvicorn.
- PID 1 runs as uid 1000 with all four uid fields at 1000, so the setpriv drop is
  complete and root cannot be regained. Remapping to `PUID=1500` works.
- `TZ` reaches the app: startup logs `America/Denver`.

Also verified in CI on clean Linux infrastructure, which reproduces the same
health output and asserts it on every push and pull request. The workflow starts
the container and checks health, `/login`, and that PID 1 is not root, so M0's
acceptance criterion is now regression tested rather than a one-off observation.

Published: `geaves006/hivesync:0.1.0`, `:0.1` and `:latest`, linux/amd64 and
linux/arm64, from tag `v0.1.0`. The published image was pulled back and confirmed
to run and report healthy.

Repository: `stevegee006/hivesync`, private. Flip to public in GitHub settings if
wanted, bearing in mind that SPEC.md and CLAUDE.md describe the network layout and
are excluded from the image by `.dockerignore`.

Publishing is tag gated: only a `v*` tag pushes to Docker Hub. Local publishing
needs `docker login` and a manual QEMU binfmt registration, so prefer the tag.

## Rules

1. **Confirm the plan for each milestone before writing its code.** Present the file layout and approach, get agreement, then implement the whole milestone.
2. **Do not start a milestone until the previous one's acceptance criteria pass.** They are in SPEC.md section 18.
3. **Never invent rclone flags or output formats.** If unsure whether a flag exists in the pinned version, verify in the container: `rclone help flags | grep <flag>`, or run the command and inspect real output. Record surprises in the Gotchas section below.
4. **Every command that touches the network or filesystem gets a test.** Assert on resulting filesystem state, not on log strings.
5. **Secret handling lives in exactly one module.** No other module touches ciphertext or plaintext credentials. No secret ever reaches a log line, a stored command, an exception message, or an API response.
6. **Build commands as `list[str]`.** Never a shell string. Never `shell=True`.
7. **`--max-delete` is always passed.** There is no code path that runs a live sync without a delete brake.
8. **Prefer failing a job with a clear user-facing message over guessing at intent.** Ambiguity in a tool that deletes files is a bug.
9. **Never write to the user-supplied `/config/rclone/rclone.conf`.** Read only, always.
10. **Update this file at every milestone boundary** with the current architecture, any version changes, and anything learned the hard way.

## Invariants that must never regress

- No plaintext credential is ever written to `/config` or to any log.
- No live sync runs without `--max-delete`.
- A dry run modifies nothing on either endpoint.
- The deletion archive path can never be inside the synced tree without an injected exclude filter.
- `bisync` never auto-resyncs. Resync is always an explicit, confirmed user action.
- A stale or empty-looking source cannot cause a mass delete on the destination.
- Only one run per job at a time.

## Commands

```bash
make dev            # run locally with reload
make test           # unit tests
make test-integration   # fixtures, then pytest inside the image on their network
make lint           # ruff + mypy
docker compose up --build
```

## Architecture summary

Every module below exists. `engines/lftp.py` is the only one from SPEC that does
not, and it is gated on open question 1 rather than scheduled.

```
app/
  main.py            app factory, startup checks, exception handlers
  config.py          pydantic-settings, env only, never touches key material
  preferences.py     the settings an operator changes at runtime, in the DB
  db.py              engine, session factory, SQLite pragmas
  logging_conf.py    structured JSON to stdout
  binaries.py        rclone and lftp version discovery for /api/health
  security.py        argon2id, sessions, bootstrap admin
  crypto.py          the ONLY module that touches secrets
  notify.py          webhook and ntfy delivery, never raises into a run
  metrics.py         Prometheus text, rendered from the run history
  portable.py        config export and import, never any credential material
  filter_presets.py  built-in presets, re-seeded at every startup
  models/            SQLAlchemy models, full SPEC section 4 schema
  schemas/           Pydantic request/response models
  probe.py           connection test and browse orchestration, host key trust
  engines/
    process.py       subprocess primitive, timeouts, redacted capture
    rcloneconf.py    remote rendering via env vars, obscure, known_hosts, ini parse
    inspect.py       listremotes, config providers, backend features, lsd, lsf
    base.py          SyncEngine interface: plan(), execute()
    rclone.py        RcloneEngine, one way
    bisync.py        bidirectional, with its own flag semantics
    parsers.py       --use-json-log and check category parsing
    lftp.py          does not exist. Gated on open question 1, not on a milestone
  capabilities.py    probe interpretation and the two-endpoint intersection
  jobs/
    planner.py       dry run lifecycle, run creation, overlap refusal
    runner.py        run lifecycle, subprocess supervision, cancellation
    scheduler.py     APScheduler wiring, plus the nightly maintenance pass
    archive.py       backup-dir path computation and validation
    retention.py     archive, log and run-history pruning
    events.py        in-process broker for live output
    cron.py          expression validation and fire-time preview
  api/               route modules mirroring SPEC.md section 12
                     health, auth, connections, credentials, rclone, jobs,
                     presets, settings, metrics
                     deps.py provides CurrentUser, so auth resolves before
                     body validation
  web/               Jinja templates, HTMX partials
```

`api/metrics.py` is mounted at the application root, not under `/api`: `/metrics`
is where a scrape config looks. It is still authenticated, by session or by
`HIVESYNC_METRICS_TOKEN`, because job names are share names.

Startup order, which is deliberate and lives in `main.create_app`:

1. Validate the Fernet key. Without it every credential is unreadable, so starting
   would only defer the failure.
2. Compare the key fingerprint against the one stored at first boot.
3. Create the bootstrap admin if the user table is empty.

Migrations are not run by the app. `docker/entrypoint.sh` runs `alembic upgrade
head` before uvicorn, so a failed migration stops the container instead of leaving
a half-migrated database serving requests. The startup checks run in `create_app`
rather than the lifespan handler so a configuration failure prints a readable
message from `main()` instead of a traceback out of uvicorn.

## Deviations from SPEC.md, agreed at M0

Each of these is also explained in a comment at the site.

1. `Job.source_subpath` is named `source_path`, symmetric with `dest_path`.
2. `Job.filters.preset_ids[]` is a real association table, `job_filter_preset`.
   As specified it was a foreign key inside a JSON blob, so deleting a preset would
   silently corrupt every job referencing it.
3. `Connection.sentinel_file` and `Connection.host_key_fingerprint` were added.
   Both are required by features the spec mandates (sections 6.4 and 15) but has no
   column for.
4. `User.must_change_password` was added, required by section 14's forced change.
5. Foreign keys are `ON DELETE RESTRICT` for Job to Connection and Connection to
   Credential. Section 12 promised only an API-level 409, which the scheduler and
   any future code path could bypass.
6. One active run per job is a partial unique index, not an application check.
7. Section 14 says a bootstrap admin password is generated when
   `HIVESYNC_ADMIN_PASSWORD` is unset. HiveSync refuses to start instead: the only
   way to deliver a generated password is to print it, and rule 5 forbids a secret
   in a log line. The message includes a suggestion to copy.
8. A secret key fingerprint is stored in `setting` at first boot, so a swapped key
   is reported at startup rather than as an opaque decrypt failure mid-run.
9. `HIVESYNC_AUTH_MODE=trusted_header` refuses to start until M8. A half
   implemented proxy trust check is an authentication bypass.
10. Section 14 says "official release tarball". rclone publishes zip archives.

## Gotchas log

Append findings here as they are discovered. Format: date, area, finding.

- 2026-08-05, spec, SMB exposes no hash types, so `--checksum` is unavailable on any job with an SMB endpoint. Compare on mtime and size, and expose `--modify-window`.
- 2026-08-05, spec, ~~a `--backup-dir` inside the sync destination causes rclone to treat the archive as extra files on the destination and delete or re-archive them on subsequent runs.~~ **Corrected at M6 against 1.74.4: that is not what happens.** rclone refuses the run outright with `Failed to sync: destination and parameter to --backup-dir mustn't overlap`. The conclusion stands for a different reason: the archive must be a sibling, or an exclude filter must be injected, because without one the job will not run at all.
- 2026-08-05, spec, `rclone bisync` requires `--resync` on first run and a persistent `--workdir`. Losing the workdir forces another resync.
- 2026-08-05, spec, Synology DSM creates `@eaDir` directories at every level and `#recycle` at share roots. Both must be excluded or they replicate to the other endpoint.
- 2026-08-05, alembic, SQLite reports non-transactional DDL, so `context.begin_transaction()` is a no-op, and pysqlite does not emit `BEGIN` until the first DML statement. Without an explicit `connection.commit()` in `migrations/env.py`, `CREATE TABLE` statements persist through autocommit while the `INSERT` into `alembic_version` is rolled back at connection close. Symptom: a fully built schema that `alembic current` reports as being at base, and a downgrade that silently does nothing.
- 2026-08-05, fastapi, a dependency parameter annotated as anything FastAPI does not recognise as a `Request` becomes a **query parameter**, turning every route that uses it into a 422. `app/db.py:get_session` must keep its `Request` annotation.
- 2026-08-05, alembic, the `script.py.mako` template must include `${imports}` or autogenerated migrations referencing dialect types emit `sqlite.JSON()` with no import and fail at runtime.
- 2026-08-05, rclone, releases are published as `.zip`, not tarballs, and `SHA256SUMS` is published alongside them. Digests for 1.74.4 are pinned in `versions.env`.
- 2026-08-05, docker, `python:3.12-slim` already carries `setpriv` (util-linux) and `usermod`/`groupmod` (passwd), so PUID/PGID remapping needs no `gosu` download. The Dockerfile asserts this with `command -v` so a wrong assumption fails the build rather than the container start.
- 2026-08-05, windows, `docker/entrypoint.sh` must be LF. A CRLF in the shebang kills the container with an opaque error. Enforced by `.gitattributes`.
- 2026-08-05, tailwind, the standalone CLI is a single Go binary, which is how "Tailwind with no Node build step" is satisfied. Tailwind publishes no checksum file for these assets, so there is no digest to pin.
- 2026-08-05, openssh, **OpenSSH 9.8 PerSourcePenalties throttles sources that connect without authenticating, and `ssh-keyscan` does exactly that.** Measured against the fixture: 4 scans succeed, the next 8 return nothing, and sshd logs `drop connection ... penalty: connections without attempting authentication`. Host key trust therefore scans **once**, records the keys as untrusted, and approves from that record with no second scan. Never add a re-scan to the approval path. The user-facing failure message says to wait a minute.
- 2026-08-05, sftp, **SFTP hash support is a property of the server, not the protocol.** SPEC 11.1 lists md5 and sha1 for SFTP, but rclone detects them by running `md5sum` and `sha1sum` over a shell. A chroot SFTP-only server such as `atmoz/sftp` has no shell and reports no hashes at all. Do not assume SFTP means checksums are available; this is precisely why the runtime probe exists.
- 2026-08-05, sftp, a server usually offers several host keys (here ssh-rsa and ssh-ed25519) and the client negotiates one. Pin all of them. Pinning only the first silently forces whichever algorithm `ssh-keyscan` happened to list first, and breaks when the server stops offering it. A single RSA entry also exceeds `String(255)`, so the column is Text.
- 2026-08-05, python, `subprocess.run(env=...)` **replaces** the environment rather than extending it, so passing only the `RCLONE_CONFIG_*` vars wiped PATH and rclone stopped being findable. Always overlay onto `os.environ`, which also keeps `RCLONE_CONFIG_PASS` reaching rclone.
- 2026-08-05, sqlalchemy, SQLite hands back **naive** datetimes even from `DateTime(timezone=True)`, so any comparison with an aware `utcnow()` raises. Fixed with the `UtcDateTime` decorator in models/base.py. Use it for every timestamp column.
- 2026-08-05, pydantic, a response model field must not share a name with an ORM attribute of a different shape. `ConnectionRead.capabilities` is a digest while `Connection.capabilities` is the raw probe payload, and `model_validate` fed one into the other. It only failed once a connection had been probed, so it passed every test written before the first successful test.
- 2026-08-05, alembic, `env.py` must call `fileConfig(..., disable_existing_loggers=False)`. The default is True, which switches off every application logger in-process, and the symptom is log lines silently not appearing.
- 2026-08-05, fastapi, enforce auth as a **dependency**, not a call inside the handler. Inside the handler, body validation runs first, so an unauthenticated caller gets a 422 describing a payload they were never entitled to submit.
- 2026-08-05, docker, never set `container_name` in a compose file. It is global to the daemon, so a leftover container from another project blocks the whole stack from starting.
- 2026-08-05, starlette, `HTTP_422_UNPROCESSABLE_ENTITY` is deprecated in favour of `..._CONTENT`. `app/api/auth.py` uses a local literal so it works across versions.
- 2026-08-05, lftp, Debian trixie ships **4.9.2**, not 4.9.3. Confirmed in the built image. Do not assume a version, read it with `make pin-versions`.
- 2026-08-05, docker, there is deliberately no `USER` directive in the Dockerfile: the entrypoint needs root to apply PUID/PGID before dropping via setpriv. Consequence: `docker exec` lands as root even though PID 1 is uid 1000. Check the app's identity with `cat /proc/1/status`, not `docker exec id`. This is the standard PUID/PGID image tradeoff.
- 2026-08-05, docker, `procps` is not in `python:3.12-slim`, so there is no `ps` in the runtime image. Use `/proc` or `docker top`.
- 2026-08-05, windows, `make` is not installed on the development host, so the Makefile targets cannot be run there. The equivalent raw commands are in the README. Consider dropping the Makefile in favour of a script if this keeps biting.
- 2026-08-05, windows, a bind mounted `/config` reports mode 777 regardless of the chown in the entrypoint, because Docker Desktop's filesystem does not carry POSIX modes. Do not write a permission assertion that expects otherwise on Windows.
- 2026-08-05, docker, `docker buildx ls` advertising `linux/arm64` does not mean arm64 builds work. Docker Desktop lists the platform but ships no binfmt handlers, so an arm64 stage dies with `exec format error`. Fix with `docker run --privileged --rm tonistiigi/binfmt --install arm64`, or build amd64 only, or let the GitHub Actions workflow do it (`docker/setup-qemu-action` handles this).
- 2026-08-05, docker, `auths` keys present in `~/.docker/config.json` do **not** mean you are logged in. Docker Desktop leaves those keys behind when logged out, with the real tokens in an external credential store. Check with `docker info | grep -i username`, which prints nothing when unauthenticated.
- 2026-08-05, ci, **the dev toolchain must be pinned exactly.** With `ruff>=0.8,<1.0` the pipeline resolved a newer ruff than the developer venv and failed a locally clean tree on rules that did not exist when the code was written. Bumping a pinned tool is a deliberate commit that shows the new findings.
- 2026-08-05, process, **check CI after the commit that ends a milestone, not before.** M1 was reported complete on the strength of a run from earlier in the milestone; the commit that actually finished it turned the pipeline red and stayed red for two more commits.
- 2026-08-05, docker, **a stage appended after `runtime` becomes the default build target**, so a plain `docker build .` silently produced an image whose entrypoint was pytest, and the container restart-looped running tests. The test image lives in `Dockerfile.test` for that reason, and compose pins `target: runtime` as well. Never append a stage after runtime.
- 2026-08-05, ux, "these endpoints share no hash type" and "nobody has probed these endpoints" are different statements. Emitting the first when the second is true sends the reader off debugging the wrong problem. `capabilities.intersect` distinguishes them and so must anything else that reports on capabilities.
- 2026-08-05, tooling, never pipe a build or test command into `tail` when the exit code matters: the pipeline returns tail's status and a failure reports as success. Redirect to a file and check `$?`, or use `PIPESTATUS`.
- 2026-08-05, rclone, **an archiving run emits no `Deleted` line at all.** With `--backup-dir` the log carries `Moved (server-side)` then `Moved into backup dir` for the same object, and a dry run reports `"skipped":"move into backup dir"` rather than `"delete"`. Counting only `delete` reported every archived file as though nothing had happened to it: zero deleted, zero archived, and a run summary claiming it did nothing. `parsers.removals` is the count that means "left the destination"; use it anywhere a deletion total is wanted. Same lines for bisync.
- 2026-08-05, rclone, **a `remote:` prefix does not make the path relative.** Every local endpoint is addressed as `alias:/absolute/path`, so rebuilding a sibling path after `strip("/")` produced `alias:tmp/...`, which rclone resolved against its own working directory. Archived files landed under the application's directory instead of beside the destination. Preserve the leading slash independently of the prefix.
- 2026-08-05, ux, the operator types a path, never an rclone spec: they have no idea the run invents an `hs_dst:` alias. A typed archive path has to be qualified with the destination's remote before it is compared to anything, or "same connection" validation rejects the only thing a person could reasonably enter. `archive.qualify()` does this; a path that does name a remote keeps it, so naming the wrong one is still refused.
- 2026-08-05, ux, a form control that is missing does not leave the field alone, it submits the schema default. The job editor carried no conflict-resolution control, so every web edit of a bidirectional job silently reset its policy to `newer`. When adding a field to a schema, add it to the form in the same change, or the form starts overwriting it. Covered now by `tests/test_job_form.py`.
- 2026-08-05, ux, hidden form inputs still submit. Switching a job away from archiving kept sending the old archive path, which the schema then refused because an archive path is only valid with archiving on. The payload builder clears it rather than the template removing the input, so the value survives a mode toggle that ends up back on archive.
- 2026-08-05, alpine, `x-cloak` does nothing without a `[x-cloak] { display: none }` rule, which nothing defined. Templates had used the attribute since M1, so every panel Alpine was about to hide was painted first and then removed. The rule is now in `tailwind.src.css`.

- 2026-08-06, rclone, **a leading `**/` in a filter does the opposite of what it looks like.** `**/@eaDir/**` requires at least one directory in front of the name, so it does not match `@eaDir` at the top of the synced folder, which is exactly where DSM puts one. An unanchored pattern already matches at every level: `@eaDir/**` is the correct form, and a leading slash anchors to the sync root where that is deliberate (`/#recycle/**`). The built-in presets shipped with the broken form from M2 until M7 and silently failed on the most likely case. Pinned by `test_the_dsm_preset_excludes_metadata_at_the_top_of_the_tree`, which runs the real binary.
- 2026-08-06, sqlalchemy, **`dict(session.execute(...))` does not iterate rows.** `Result` has a `keys()` method, so `dict()` takes the mapping path, tries to subscript the Result and raises `'ChunkedIteratorResult' object is not subscriptable`. It reads as though it works right up until it runs. `.tuples()` does not help: it is a typing-only wrapper that returns the same object. Iterate explicitly.
- 2026-08-06, httpx, **header values are encoded as ascii, not latin-1**, and a non-ascii value raises `UnicodeEncodeError` and fails the whole request. A job name is free text and ntfy carries the title in a header, so the title is degraded to ascii and the real name is repeated in the UTF-8 body.
- 2026-08-06, testing, the SMB fixture is a **persistent volume**, shared across tests and across runs until `docker compose down -v`. A test asserting "two files are new" fails on the second run for reasons that look like a product bug. Give each run a unique destination subpath rather than trusting the fixture to be empty.
- 2026-08-06, ux, a field with no control on the form submits the schema default on every save. This has now happened three times: `conflict_resolve`, `notify_on`, and the archive fields. `tests/test_job_form.py` covers each. When adding a column, add the control in the same change.

### M1 acceptance status, verified 2026-08-05

All four criteria pass. 167 unit tests, 12 integration tests against live SFTP,
FTP and SMB fixtures, ruff and mypy clean.

1. SFTP, FTP and SMB each test green against the fixtures.
2. A remote defined only in the mounted rclone.conf is listed, tested and browsed,
   with zero Credential rows written and the user's file left byte-identical.
3. No plaintext secret in any database text column, anywhere under /config, or in
   any log record. Swept by tests/test_redaction_sweep.py.
4. A hash-less backend disables checksum comparison with a visible reason, checked
   both on fixtures and against capabilities read from the live endpoints.

Remaining for M1: nothing blocking. The connection editor, credentials page,
directory browser and compatibility page are built; the job editor that will
consume the intersection logic belongs to a later milestone.

### M7 polish, verified 2026-08-06

353 unit tests, 65 integration tests against live SFTP, FTP and SMB fixtures.

**The `lftp` engine is still not built.** SPEC gates it on open question 1, which
is unanswered, so the binary stays in the image and jobs selecting it are refused
with a message. Building an engine nobody has established a need for is how the
one deletion path nothing tests gets written.

**Apprise was declined.** SPEC 16 says "if it is cheap to add". It is a large
dependency tree wrapping HTTP calls this already makes, and every service it adds
is one nothing here can test. Webhook and ntfy are implemented directly.

**Notifications are sent after the run record is committed, never inside it.** A
slow endpoint would otherwise hold a SQLite write lock for the length of its
timeout, and a failed notification must never change what a run recorded. Proved
by `test_a_broken_endpoint_does_not_change_the_run`.

**Metrics are aggregates over `job_run`, not in-process counters.** A counter held
in memory resets on restart, and a container that restarts nightly reports a
sawtooth that means nothing. The cost is that this module owns the Prometheus text
format by hand, which is why `test_metrics.py` parses the output rather than
grepping it.

**`/metrics` is authenticated**, by session or by `HIVESYNC_METRICS_TOKEN`. The
labels carry job names, which in this application are the names people give their
shares. This partly answers open issue 7; `/api/health` still discloses binary
versions without a session, and that stays for M8.

**Retention only prunes what it can be sure of.** Off unless a number is set,
whole run directories only, only names matching the run stamp, only local paths,
and never the flat suffix layout. Everything else is reported as unprunable with
the path, so it can be cleared by hand. Deleting from an archive is the one
operation here with nothing behind it.

**An export contains no credential material, including ciphertext.** Fernet
ciphertext is only as strong as a key that lives in the compose file people commit
next to it. Credentials are exported as names, and the import re-links by name and
warns loudly about each one that is missing. Probe output (capabilities, host keys,
last-test results) is excluded too: it describes an environment, and a host key is
a trust decision that has to be made again on the machine doing the trusting.

Acceptance criteria, all five met:

1. A failed run posts exactly one webhook with the specified payload; a successful
   run under "failure only" posts none; a target that times out fails the
   notification and not the run. Against a real socket, not a mocked httpx.
2. `/metrics` parses as Prometheus text format and the counters move by exactly
   one run's counts. Dry runs do not move them.
3. A 30 day prune removes archive directories older than 30 days, keeps the
   newest, and touches nothing outside the archive base. Asserted on filesystem
   state.
4. An export imports into an empty instance reproducing every connection, job and
   preset, with zero `Credential` rows and no secret anywhere in the file.
5. The README walkthrough is executed step by step against the SMB fixture, through
   the same web forms a reader would use. `tests/test_readme_walkthrough.py`.

Criterion 5 found a real defect in step 6's advice, and the same defect in the
shipped Synology preset: see the `**/` filter gotcha above.

Also fixed here, because they were false rather than merely unpolished: the
dashboard still said jobs were not built, the jobs list still said live syncing
arrived in a later milestone, nothing linked to the connections, credentials or
compatibility pages, and `x-cloak` had never been defined so every panel Alpine
was about to hide was painted first.

### M6 deletion archiving, verified against rclone 1.74.4 and real SMB

Where a deleted file goes lives in `app/jobs/archive.py`. Two decisions were made
here rather than asked about, and both are load bearing.

**A share root archives into a child, not a sibling.** SPEC 7.1's sibling rule is
right for `remote:Share/media`, whose sibling is `remote:Share/media.hivesync-archive`.
It is unusable for `remote:Share`, whose sibling is `remote:Share.hivesync-archive`,
a *different share* that does not exist and that rclone cannot create. This is not
a Synology quirk; no SMB server has a sibling of a share root. Verified against
the fixture: rclone does not fail fast, it hangs retrying. A destination with no
parent therefore archives into a child and the exclude is injected. An absolute
path is exempt, because `/data` does have a creatable sibling.

**Retention pruning is deferred to M7.** `archive_retention_days` is stored and
ignored. Deleting from the archive is the one operation in this program with no
undo behind it, and it belongs with the other scheduled maintenance work rather
than bolted onto the run path. Nothing prunes today: say so in the UI before
claiming otherwise.

Acceptance criteria, all four met, `tests/test_archive_integration.py`:

1. A deleted file lands in the archive with its relative path preserved, under
   `<base>/<job-slug>/<run timestamp>/`.
2. The archive is never itself synced or re-archived, asserted across three
   consecutive runs for both a sibling archive and a child archive with the
   injected exclude. The destination is byte-identical between runs two and three.
3. lftp plus archiving is refused with a reason about the combination rather than
   about lftp, because it stays impossible once the engine exists.
4. The manual checklist, automated against the SMB fixture rather than a Synology:
   create, update, delete into the archive, rename, and a unicode filename, then a
   further run proving nothing is archived twice.

**Criterion 4 does not cover a file larger than 5 GB.** The spec asks for one. It
needs tens of gigabytes of disk and minutes of transfer per run, so it stays a
manual check against real hardware. Nothing in this suite is evidence about
multi-gigabyte files.

Also true, and verified rather than assumed: archived deletions still count
against `--max-delete`. With a brake of two and ten files to remove, rclone
archived two and aborted. Archiving does not smuggle deletions past the brake.

### M5 bidirectional, verified against rclone 1.74.4

**`--max-delete` is a PERCENTAGE for bisync and a COUNT for sync.** Same flag,
different units. Verified: `--max-delete 10` produces
`Safety abort: too many deletes (>10%, 3 of 10)`. Never call
`resolve_max_delete()` for a bisync command: on a 1000 file destination a 20%
brake resolves to 200, which bisync reads as 200 percent and the brake is gone,
on the one direction that can damage both copies. Pass `job.max_delete_pct`
straight through. Ironically SPEC 6.4's percentage framing is right for bisync
and wrong for sync.

**A nonzero `--modify-window` disables the newer and older conflict policies
entirely.** Not merely within the window: verified with versions ten seconds
apart and a one second window. With the flag there is no winner, both versions
are renamed to `.conflict1` and `.conflict2`, and **the file disappears from its
original name**. Without it, or with `--modify-window 0`, the newer version wins
and the loser is kept as `.conflict1`. `path1` and the other non-time policies
are unaffected. `Job.modify_window` defaults to `1s`, so passing it blindly would
silently discard the operator's chosen conflict policy on every bidirectional
job. `bisync.modify_window_applies()` drops it for time-based policies only.

**bisync has more than one safety abort**, and they are pre-flight, aborting
before anything changes:
- `too many deletes (>N%, x of y)` against the `--max-delete` percentage.
- `all files were changed on PathN`, which fires when 100% of a side changed.
  A restored-from-backup or re-encrypted tree looks like this. It also means a
  test fixture whose only file is the conflicting one is refused before any
  conflict handling happens.

Both end with `Run with --force if desired`, which is what detection matches.
**Do not match on `Safety abort:`**: under `--use-json-log` rclone moves that
prefix into a separate `object` field and the obvious marker silently never fires.

**A first run and a wiped workdir are indistinguishable.** Both produce
`Bisync aborted. Must run --resync to recover.` and exit 7. One detection path
drives one recovery prompt, and `Job.bisync_initialized` cannot be trusted alone
because the workdir can vanish underneath it. Seeing the message clears the flag.

`--workdir` defaults to `/root/.cache/rclone/bisync`, wrong for a container
running as uid 1000 and not persistent. Always set it to `/config/bisync/<job-id>`.

The colour flag is `--color NEVER`. There is no `--no-color`, and passing one
makes rclone misparse the whole command into `unknown command`.

### M4 scheduler design

**The schedule lives in the Job table, not in an APScheduler jobstore.** SPEC
section 3 specifies `SQLAlchemyJobStore`; this deviates, with evidence:

- Starting one against our database creates `apscheduler_jobs`, which is not in
  `Base.metadata`, so Alembic autogenerate immediately proposes
  `remove_table apscheduler_jobs`. The next generated migration would delete the
  schedule store on upgrade. Verified directly.
- It is a second copy of every schedule that must be kept in step on every job
  edit and delete, and it pickles a function reference that breaks when the
  function moves.

Rebuilding from `Job.schedule_cron` at startup gives the same restart survival,
because the schedule was always persisted there. It also means a restart fires no
backlog at all, which is what section 9 wants `coalesce` and `misfire_grace_time`
to achieve. Anything that edits a job calls `scheduler.reload()`.

**Overlap is prevented three times over**, because "never double-runs" is a claim
about a tool that deletes files: APScheduler's `max_instances=1`, a recorded
`skipped` run instead of queueing (SPEC 6.2, which is what `skip_reason` has been
waiting for since M0), and the database's partial unique index.

**APScheduler 3.11.3 uses zoneinfo, not pytz.** `CronTrigger.from_crontab` takes
a timezone string, rejects a malformed expression with `ValueError` and an unknown
zone with `ZoneInfoNotFoundError`. No extra dependency.

The schedule preview is concrete fire times rather than prose. They are
unambiguous for any expression, and they come from the same trigger object the
scheduler uses, so the preview cannot disagree with what happens. A note on
reading them: at exactly a boundary instant, `*/2` matches *now*, so the first
listed time can be the current minute. That is correct, not an off-by-one.

### M3 live sync design

**The delete brake is two mechanisms.** SPEC 6.4 and invariant 7 describe
`--max-delete` as the brake, but verified against rclone 1.74.4 the flag is an
**in-flight abort**: it deletes up to the threshold and then stops with exit 7.
M3's criterion says an emptied source is *refused*, which means nothing is
removed at all. So:

1. **Pre-flight veto.** A live run plans immediately before executing and refuses
   before rclone is invoked if the plan exceeds the brake. This is what makes the
   criterion true. It also re-checks the sentinel file rather than trusting the
   last connection test, since a mount healthy an hour ago is exactly the failure
   SPEC 6.4 is about.
2. **The flag, always.** Still passed on every live sync, so a tree that changed
   between planning and executing cannot run away. Invariant 7 holds literally.

Planning before every live run is deliberate. A dry run from an hour ago cannot
notice a source that failed to mount five minutes ago.

**Cancellation.** SIGTERM, ten seconds, then SIGKILL. Verified: on SIGTERM rclone
logs `Removing failed copy` and deletes the `<name>.<id>.partial` it was writing,
so a cancelled transfer leaves nothing half-written under a final name. **A
SIGKILL skips that handler and does leave a `.partial` behind**, which the next
sync would see as an extra file on the destination and count against the brake.
The grace period is functional, not polite.

A cancelled run records the work it actually completed, parsed from the stream as
it arrives. Reporting nothing would mislead the next run's brake, which reads the
resulting state.

**Live output** goes through an in-process broker (`app/jobs/events.py`), not the
database. An SSE stream stays open for the length of a sync, and holding a SQLite
session that long blocks writers.

`copy` is used instead of `sync` when `delete_mode` is none, so a copy-only job
has no code path that can remove anything.

### M2 dry run design, verified against rclone 1.74.4

**SPEC section 8's `--combined` legend is inverted.** Real output for
`rclone check SRC DST`: `+` is path1-only (will be created), `-` is path2-only
(will be deleted), `*` differs, `=` identical. The spec states the opposite for
`+` and `-`. Following it would label every about-to-be-created file "deleted" in
the review table whose entire purpose is preventing accidental deletion.

`--combined` is therefore not parsed at all. `check` has named per-category
outputs, `--missing-on-dst`, `--missing-on-src`, `--differ`, `--match`,
`--error`, which cannot be inverted by a reader or a later edit.

**`check` compares hash and size, never modification times.** So it cannot
reproduce what `sync` would do for the default mtime comparison, and the spec's
"classify with check, confirm with sync" has the roles backwards. The engine uses:

- Phase 1, `check --size-only`, for **presence only**. Presence is independent of
  hashes and mtimes, so it is reliable for every backend pairing, and
  `--size-only` means no hashing at all, which matters on a NAS.
- Phase 2, `sync --dry-run --use-json-log`, as the **authority** on what changes,
  because it is the same code path a live run takes.
- Reconciliation: new = copied and missing on dest; updated = copied and present;
  deleted = deleted; unchanged = present on source and not copied.

Other verified behaviour:

- `sync --dry-run --use-json-log` lines carry a machine-readable **`skipped`**
  field, `"copy"` or `"delete"`. Do not parse the message string. Both new and
  updated files report `skipped: "copy"`, which is why phase 1 is needed to tell
  them apart.
- **`check` exits 1 when differences exist.** That is the normal case, not a
  failure, and `CommandResult.ok` would read it as one.
- **`--max-delete` trips during a dry run**, exit 7 with
  `--max-delete threshold reached`, and truncates the plan at the threshold. The
  planning pass therefore uses a high limit and evaluates the real brake against
  the full count afterwards. Invariant 7 is untouched: live syncs pass the real value.
- **Dry run modifies nothing.** Verified byte for byte before and after.
- With no common hash, `check` does not fail; it falls back to size and reports
  "N hashes could not be checked". With `--size-only`, a 4-byte-vs-4-byte content
  change is reported **identical**. That blind spot is surfaced as a plan warning.

### Verified against rclone 1.74.4 in the built image, 2026-08-05

Spikes run before M1 design. These replace assumptions in SPEC.md sections 5.3, 5.4 and 6.4.

- **Env var remotes work.** `RCLONE_CONFIG_HSSRC_TYPE=local` makes `hssrc:` usable, appears in `listremotes`, and resolves under both `hssrc:` and `HSSRC:`. This is the config generation method.
- **Env var secrets must be obscured.** Plaintext fails: `failed to decrypt password: input too short when revealing password - is it obscured?`. Obscured works.
- **`rclone obscure -` reads plaintext from stdin.** This resolves the argv exposure concern: the plaintext never appears in a command line or on disk. Do not reimplement rclone's obscure algorithm in Python.
- **`--config ""` fully disables config file lookup** and silences the "Config file not found" NOTICE. Pass it on every invocation that uses env var remotes, so an unexpected config file can never be picked up.
- **`--max-delete` is an `int`, a count.** There is no percentage flag. The only related flag is `--max-delete-size SizeSuffix`. This confirms open issue 1 with evidence.
- **`rclone backend features` returns** `Name`, `Root`, `String`, `Precision` (int), `Hashes` (list of strings), `Features` (dict of 52 booleans), `MetadataInfo`.
- **There is no file-level `CanSetModTime` feature flag.** The only modtime entries are `DirSetModTime`, `WriteDirSetModTime`, `DirModTimeUpdatesOnWrite` and `SlowModTime`, all about directories. Per rclone convention, a backend that cannot set file modtimes reports `Precision` as `math.MaxInt64` (9223372036854775807). **That is the signal for the bidirectional gate in SPEC 5.4**, not a Features boolean. Confirm against the real SFTP, FTP and SMB fixtures during M1.
- **`Features.CaseInsensitive` exists**, which directly serves the SPEC 10.7 case sensitivity warning.
- **`rclone config providers` returns 69 providers.** Each option carries `IsPassword` and `Sensitive` booleans. `IsPassword` (36 options overall) means the value must be obscured. `Sensitive` (211 options overall) means redact from logs, and includes non-secrets like `host` and `user`. Drive both behaviours from this metadata rather than a hardcoded list.
- **`sftp` exposes `key_pem`**, so an SSH private key can be passed inline through an env var. No temp key file on disk. `key_pem` is `Sensitive` but not `IsPassword`, so it is passed raw and must be redacted, not obscured.
- **`sftp.known_hosts_file` defaults to empty, so rclone does not verify host keys by default.** SPEC section 15's "verification on by default" requires explicitly supplying a known_hosts file. Nothing is verified until we do.
- **`smb` has no `share` option**, confirming SPEC 5.1: the share is the first path element of `remote:Share/path`.
- **Env var remotes and `--config <user file>` coexist in one invocation.** Verified: both appear in `listremotes`, both resolve, and the user file is never written. A job pairing an inline endpoint with an imported one needs no special handling.
- **Consequence for SPEC 5.3: the temp file fallback is not needed and is not implemented.** Inline remotes use env vars, imported remotes pass `--config /config/rclone/rclone.conf` read only, SSH keys use `key_pem`. There is therefore no code path that writes credential material to disk at all, which is a stronger guarantee than the spec asked for. If a future backend cannot be expressed through env vars, reconsider, and record it here.

## Open spec issues carried forward

Raised at M0, resolved at the milestone named. Do not rediscover these.

1. **`max_delete_pct` has no rclone flag behind it. M3.** Section 6.4 and invariant
   7 both treat `--max-delete` as taking a percentage; it takes a **count**.
   Converting needs the destination file count before the run, which the spec never
   provides a source for. Worse, `--max-delete` is an in-flight abort, not a
   pre-flight veto: it stops the sync partway through, after some deletions have
   already happened. M3's criterion ("a job whose source is emptied is refused by
   the delete brake") describes a pre-flight refusal, a different mechanism. Likely
   resolution: both, plus a `max_delete_abs` column. Verify the pinned rclone's real
   flag set first.
2. **Does `bisync` accept `--max-delete`? M5.** If not, invariant 7 is
   unsatisfiable for bidirectional jobs and needs rewording.
3. **Do not trust the `check --combined` symbol table in section 8. M2.** Read the
   real output of the pinned rclone instead: rule 3. Related gap in the same
   section: `check` compares by hash, and SMB and FTP expose none, so phase 1 needs
   `--size-only` or `--download` on those pairings. That is most Synology jobs.
4. **The default archive path breaks at a share root. M6.** Section 7.1 derives a
   sibling, so `remote:Media` gives `remote:Media.hivesync-archive`, a different SMB
   share that does not exist and cannot be created over the SMB backend. Needs a
   defined fallback.
5. **`rclone obscure` as a subprocess puts a plaintext secret in argv. M1.**
   Readable via `/proc` by anything in the container. Section 5.3 accepts env var
   exposure explicitly but does not note this. Needs a stdin path, or no `obscure`.
6. **Dry run does two full listings of both endpoints. M2.** Section 8 runs `check`
   then `sync --dry-run`. On a large NAS tree that is slow enough that people stop
   using dry run, which is the feature keeping them safe. Consider deriving phase 1
   from phase 2's JSON. Section 8's "estimated duration" card also has no stated
   basis; it needs last-run throughput or should be dropped.
7. **Unauthenticated `/api/health` discloses binary versions. M8.** Kept for now
   because M0's acceptance criterion requires it. Split bare liveness from version
   detail. **`/metrics` was resolved at M7**: it requires a session or
   `HIVESYNC_METRICS_TOKEN`, because its labels carry job and share names.
8. **CSRF, login rate limiting, host key pinning. M8.** Until then the app is not
   safe to expose beyond a trusted network, which the README states.

## Style

- Type hints everywhere. `ruff` and `mypy` clean.
- No em dashes in any user-facing string, doc, or comment. Use a colon or comma.
- User-facing error messages say what happened, why, and what to do next.
- Commit messages: `<area>: <what changed>`, imperative mood.

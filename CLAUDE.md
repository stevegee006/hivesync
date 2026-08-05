# CLAUDE.md

Standing instructions for work on this repository. `SPEC.md` is the requirements document. This file is the working agreement plus the running record of decisions and gotchas. Keep it current.

## What this project is

A Dockerized file sync orchestrator with a web UI. It does not implement file transfer protocols. It orchestrates `rclone` (and optionally `lftp`) as subprocesses, adding scheduling, credential management, dry-run previews, deletion archiving, and a UI.

Read `SPEC.md` before starting any milestone.

## Current state

| Field | Value |
|---|---|
| Milestone in progress | M3 |
| Milestones complete | M0, M1, M2 |
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

Target layout. Modules marked `todo` do not exist yet: they are created by the
milestone that fills them, because empty stub modules are noise that lint has to be
told to ignore.

```
app/
  main.py            app factory, startup checks, exception handlers
  config.py          pydantic-settings, env only, never touches key material
  db.py              engine, session factory, SQLite pragmas
  logging_conf.py    structured JSON to stdout
  binaries.py        rclone and lftp version discovery for /api/health
  security.py        argon2id, sessions, bootstrap admin
  crypto.py          the ONLY module that touches secrets
  models/            SQLAlchemy models, full SPEC section 4 schema
  schemas/           Pydantic request/response models
  probe.py           connection test and browse orchestration, host key trust
  engines/
    process.py       subprocess primitive, timeouts, redacted capture
    rcloneconf.py    remote rendering via env vars, obscure, known_hosts, ini parse
    inspect.py       listremotes, config providers, backend features, lsd, lsf
    base.py          todo, M2. SyncEngine interface: plan(), execute()
    rclone.py        todo, M2. RcloneEngine
    parsers.py       todo, M2. --combined and --use-json-log parsing
    lftp.py          todo, M7, optional
  capabilities.py    probe interpretation and the two-endpoint intersection
  jobs/              todo, M3 and M4
    runner.py        run lifecycle, subprocess supervision, cancellation
    scheduler.py     APScheduler wiring
    archive.py       backup-dir path computation and validation
  api/               route modules mirroring SPEC.md section 12
                     health, auth, connections, credentials, rclone
                     deps.py provides CurrentUser, so auth resolves before
                     body validation
  web/               Jinja templates, HTMX partials
  notify.py          todo, M7
  metrics.py         todo, M7
```

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
- 2026-08-05, spec, a `--backup-dir` inside the sync destination causes rclone to treat the archive as extra files on the destination and delete or re-archive them on subsequent runs. Archive must be a sibling, or an exclude filter must be injected.
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
   detail. Same decision needed for `/metrics`.
8. **CSRF, login rate limiting, host key pinning. M8.** Until then the app is not
   safe to expose beyond a trusted network, which the README states.

## Style

- Type hints everywhere. `ruff` and `mypy` clean.
- No em dashes in any user-facing string, doc, or comment. Use a colon or comma.
- User-facing error messages say what happened, why, and what to do next.
- Commit messages: `<area>: <what changed>`, imperative mood.

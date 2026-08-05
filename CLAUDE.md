# CLAUDE.md

Standing instructions for work on this repository. `SPEC.md` is the requirements document. This file is the working agreement plus the running record of decisions and gotchas. Keep it current.

## What this project is

A Dockerized file sync orchestrator with a web UI. It does not implement file transfer protocols. It orchestrates `rclone` (and optionally `lftp`) as subprocesses, adding scheduling, credential management, dry-run previews, deletion archiving, and a UI.

Read `SPEC.md` before starting any milestone.

## Current state

| Field | Value |
|---|---|
| Milestone in progress | M1 not started |
| Milestones complete | M0 |
| Pinned rclone version | 1.74.4, by SHA256 digest, see `versions.env` |
| Observed lftp version | (run `make pin-versions` once Docker is available and record it here) |
| Config generation method | (env vars or temp file, decide and record at M1) |
| Capability probe method | (`rclone backend features` or fallback, record at M1) |

The lftp row says "observed" rather than "pinned" on purpose. Debian trixie ships
one lftp and only security patches it, so an exact apt version pin turns every
future point release into a build failure for no safety gain. The base image gets
pinned by digest at M8, which pins the apt snapshot properly.

### M0 verification status

Verified on the development host:

- 71 unit tests pass, `ruff check`, `ruff format --check` and `mypy app` are clean
- `alembic upgrade head` produces exactly the declarative models, asserted by
  `tests/test_migrations.py`
- The app boots, `/login` returns 200, `/` redirects unauthenticated requests, and
  `/api/health` reports database and binary state

**Not yet verified: the container.** Docker Desktop's daemon was not running, so
`docker build` and `docker compose up` have never been executed. M0's acceptance
criterion is not fully met until they are. Run `make build && make up`, then
`make pin-versions`, and fill in the lftp row above.

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
make test-integration   # spins up docker-compose.test.yml fixtures
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
  engines/           todo, M2
    base.py          SyncEngine interface: plan(), execute()
    rclone.py        RcloneEngine
    lftp.py          LftpEngine (M7, optional)
    rcloneconf.py    remote definition rendering, env vars and temp config
    parsers.py       --combined and --use-json-log parsing
  capabilities.py    todo, M1. backend feature probe and intersection logic
  jobs/              todo, M3 and M4
    runner.py        run lifecycle, subprocess supervision, cancellation
    scheduler.py     APScheduler wiring
    archive.py       backup-dir path computation and validation
  api/               route modules mirroring SPEC.md section 12
                     M0: health.py, auth.py
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
- 2026-08-05, starlette, `HTTP_422_UNPROCESSABLE_ENTITY` is deprecated in favour of `..._CONTENT`. `app/api/auth.py` uses a local literal so it works across versions.

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

# HiveSync

A self-hosted, Docker-based file sync orchestrator with a web UI. Replaces Resilio Sync for cloud-server-to-local-server directory syncing, adding multi-protocol support, scheduling, dry runs, and deletion archiving.

*Rename the project freely before scaffolding. Every occurrence of `hivesync` is a placeholder.*

---

## 1. Requirements

1. Runs as a single Docker container, with a compose file.
2. Web UI. No CLI required for normal operation.
3. Per-job source and destination, each independently configured with its own protocol and credentials.
4. Protocols: SFTP, FTP/FTPS, SMB/CIFS, local filesystem, and a generic rclone remote passthrough.
5. Reusable credential storage per connection, encrypted at rest.
6. A job is: source connection, destination connection, options, schedule.
7. Direction modes: one way A to B, one way B to A, bidirectional.
8. Dry run mode that reports exactly what would change, changing nothing.
9. Deletion archiving: instead of deleting, move the file to an archive folder. Defaults to a location derived from the affected side, overridable per job.

---

## 2. Core architecture decision

**Do not write protocol code.** Use `rclone` as the transfer engine, invoked as a subprocess. Optionally use `lftp` as a secondary engine. Every requirement above maps to a native rclone capability:

| Requirement | rclone mechanism |
|---|---|
| SFTP / FTP / FTPS / SMB / local / everything else | backend types `sftp`, `ftp`, `smb`, `local`, plus 60 or so others |
| One way sync | `rclone sync SRC DST` |
| Bidirectional | `rclone bisync PATH1 PATH2` |
| Dry run | `rclone check --combined` for classification, `rclone sync --dry-run` for the exact plan |
| Deletion archive | `--backup-dir`, or `--backup-dir1` / `--backup-dir2` for bisync |
| Machine-readable output | `--use-json-log -v` |
| Bandwidth and concurrency | `--bwlimit`, `--transfers`, `--checkers` |
| Integrity | mtime and size by default, `--checksum` only when both backends expose a common hash type |
| Capability discovery | `rclone backend features <remote>:` |

### 2.1 Engine and protocol are separate concepts

The UI must keep these visually distinct because they answer different questions.

**Engine**, chosen per job, means "how do bytes move":
- `rclone` (default, recommended, supports every feature in this spec)
- `lftp` (optional, faster on high-latency FTP links via segmented parallel transfers, one way only, no archiving)

**Protocol**, chosen per endpoint, means "what am I talking to":
- `local`, `sftp`, `ftp`, `ftps`, `smb`, `rclone_remote`

LFTP is not a protocol, it is a client. Label it accurately in the UI so nobody expects it in the protocol list.

### 2.2 Engine constraints

- `lftp` is selectable only when **both** endpoints are `sftp`, `ftp`, or `ftps`. Any `smb`, `local`, or `rclone_remote` endpoint forces the `rclone` engine, and the UI states why rather than silently switching.
- `lftp` cannot do bidirectional sync or `--backup-dir` archiving. If the user selects either, the UI explains that rclone is required and offers to switch the engine.
- New jobs default to `rclone`.

---

## 3. Tech stack

- **Backend:** Python 3.12, FastAPI, Uvicorn
- **DB:** SQLite via SQLAlchemy 2.x with Alembic migrations, at `/config/hivesync.db`
- **Scheduler:** APScheduler 3.x with `SQLAlchemyJobStore` so schedules survive restarts, cron triggers, timezone aware
- **Frontend:** Jinja2 templates, HTMX, Alpine.js, Tailwind. Server rendered, no Node build step. Live run output over Server-Sent Events.
- **Crypto:** `cryptography` Fernet, key from `HIVESYNC_SECRET_KEY`
- **Password hashing:** argon2id via `argon2-cffi`
- **External binaries:** `rclone` (pinned), `lftp`, `openssh-client`, `ca-certificates`, `tzdata`
- **Tests:** pytest, pytest-asyncio, integration tests against throwaway SFTP/FTP/SMB containers

HTMX over a SPA because this is roughly seven screens. The API in section 12 stays clean enough to add a SPA later if wanted.

---

## 4. Data model

```
Connection
  id, name,
  type (local|sftp|ftp|ftps|smb|rclone_remote),
  host, port, share (smb only), base_path,
  username, credential_id (nullable), extra_opts (JSON),
  # rclone_remote only:
  rclone_mode (inline|imported), rclone_remote_name, rclone_backend_type,
  # populated by the capability probe, section 5.4:
  capabilities (JSON), capabilities_probed_at,
  created_at, updated_at, last_test_at, last_test_ok, last_test_error

Credential
  id, name, kind (password|ssh_key|smb_ntlm|backend_secret),
  secret_ciphertext (BLOB, Fernet), key_passphrase_ciphertext (nullable),
  created_at, updated_at
  # never serialized back to the client, ever

Job
  id, name, enabled,
  source_connection_id, source_subpath,
  dest_connection_id, dest_path,
  engine (rclone|lftp),
  direction (source_to_dest|dest_to_source|bidirectional),
  delete_mode (none|delete|archive),
  archive_base (nullable, default computed per section 7.1),
  archive_layout (timestamped_dir|suffix),
  archive_retention_days (nullable),
  filters (JSON: include[], exclude[], min_size, max_age, preset_ids[]),
  compare_mode (mtime_size|checksum|size_only),
  modify_window (default 1s),
  transfers, checkers, bwlimit,
  max_delete_pct (safety brake, default 20),
  conflict_resolve (newer|older|larger|smaller|path1|path2|none),
  schedule_cron (nullable), timezone, timeout_seconds,
  notify_on (never|failure|always),
  bisync_initialized (bool),
  created_at, updated_at

JobRun
  id, job_id, trigger (manual|schedule|api), mode (dry_run|live),
  status (queued|running|success|failed|cancelled|skipped),
  started_at, finished_at, exit_code,
  files_transferred, files_deleted, files_archived, bytes_transferred,
  errors_count, log_path, summary (JSON), command_redacted (TEXT),
  skip_reason (nullable)

JobRunChange
  id, run_id, action (new|updated|deleted|archived|unchanged|conflict|error),
  side (source|dest), path, size, mtime, message

FilterPreset
  id, name, builtin (bool), rules (JSON)

Setting     # key/value: auth mode, notifications, concurrency, defaults
User        # id, username, password_hash, role, created_at, last_login_at
```

---

## 5. Connections

### 5.1 Native types

`local`, `sftp`, `ftp`, `ftps`, `smb`. The editor is a two column form where the protocol picker drives which fields appear. SMB gets a **Share** field distinct from the base path, because rclone SMB paths are `remote:Share/sub/path` and the first path element is the share.

### 5.2 The `rclone_remote` type

A generic passthrough exposing every backend rclone supports (S3, B2, WebDAV, Box, Google Drive, OneDrive, Azure, Storj, crypt, union, and the rest) without per-backend UI. It sits below a divider in the protocol picker, labelled "rclone remote (advanced)". Two sub-modes:

**`imported`** references a remote from a user-managed config file mounted read-only at `/config/rclone/rclone.conf`:
- Enumerate with `rclone listremotes --config /config/rclone/rclone.conf`, present as a dropdown
- No credentials enter the app database
- Support an encrypted config via `RCLONE_CONFIG_PASS` sourced from the environment, never from the DB
- **Never write to this file.** Open read only. If it is missing, error clearly rather than creating one.

**`inline`** defines the remote inside the app:
- Backend type field populated at runtime from `rclone config providers`, never a hardcoded list
- Remaining fields are a key/value editor. Rows marked secret go to `Credential` encrypted and are rendered through `rclone obscure` at run time.
- Offer a "paste an rclone.conf stanza" import that parses ini into those rows. This is the single highest value convenience in the feature.

Both modes resolve identically at run time to `remote:path`.

### 5.3 Runtime config generation

**Preferred:** define remotes through environment variables of the form `RCLONE_CONFIG_<REMOTE>_<KEY>`, so no plaintext secret ever touches disk. Verify this works in the pinned version with `rclone listremotes` and an `lsd` against an env-defined remote before committing to it. Env vars are readable via `/proc` by processes in the same container, which is an acceptable trade in a single tenant container and strictly better than a file on a persistent volume.

**Fallback**, for cases env vars cannot express (imported configs, SSH key files): generate a minimal `rclone.conf` into a `tempfile.TemporaryDirectory()` at mode `0600` containing only the two remotes that run needs, pass `--config <path>`, and remove it in a `finally` block. Obscure passwords before writing. SSH keys go in the same temp dir at `0600`, referenced via `key_file`.

Never call `rclone config` interactively. Never write generated config to `/config`. Never log generated config.

### 5.4 Capability probing

Do not hardcode per-protocol feature matrices. Once arbitrary rclone backends are allowed, a static table is unmaintainable.

On every "Test connection", run `rclone backend features <remote>:` and store the JSON on `Connection.capabilities` with a timestamp. It reports supported hash types, whether modification times are writable, whether empty directories are supported, and which server-side operations exist.

The job editor derives its constraints from the intersection of the two endpoints:

| Job option | Enabled when |
|---|---|
| `compare_mode = checksum` | both endpoints share at least one hash type |
| `direction = bidirectional` | both endpoints can write modification times |
| `delete_mode = archive` | the archiving side supports Move, and the archive path is on the same remote and same share or bucket |
| `--create-empty-src-dirs` | both endpoints support empty directories |
| server-side archive rename | archiving side reports Move, otherwise warn that archiving costs a full round trip |

Every disabled option shows its reason inline, for example: "Checksum comparison unavailable: SMB exposes no hash types." Re-probe on edit. Treat a probe older than 30 days as stale on the job edit screen.

If `rclone backend features` is absent or shaped differently in the pinned version, fall back to `rclone about` plus targeted probe operations, and record the actual approach in `CLAUDE.md`. Verify in the container. Do not assume.

### 5.5 Connection test and browse

- Test: `rclone lsd` against the configured base path with a short timeout, plus the capability probe, plus an archive-path writability check when relevant
- Browse: `rclone lsf` powering a directory picker. For `smb` and `rclone_remote`, the root level lists shares or buckets.

---

## 6. Runtime rules

### 6.1 Command construction
Build every command as `list[str]`, never a shell string. Store a **redacted** copy on `JobRun.command_redacted` so the UI can show what ran without leaking secrets.

### 6.2 Concurrency
One running `JobRun` per Job, enforced at the DB level. A scheduled trigger firing while a run is active records a `skipped` run with a `skip_reason` rather than queueing indefinitely. Global concurrency cap configurable, default 3.

### 6.3 Cancellation
Store the subprocess PID. Cancel sends SIGTERM, waits 10 seconds, then SIGKILL. Mark the run `cancelled`.

### 6.4 Safety brakes
- Always pass `--max-delete` derived from `max_delete_pct`, so a source that mounts empty or fails to list cannot wipe the destination. This is the single most valuable safety feature in the tool. Do not make it optional.
- For `local` connections that are network mounts, support an optional **sentinel file** check: a known filename that must exist before the run proceeds. A stale cifs or NFS mount presents as an empty directory, which is exactly this failure mode.
- Refuse to save a job whose source and destination overlap on the same remote.
- Every destructive UI control is confirmation gated and names the affected job.

---

## 7. Deletion archiving

### 7.1 Default location
The archive base defaults to a **sibling** of the sync root, not a child:

- Root `remote:Share/media` gives archive base `remote:Share/media.hivesync-archive`

If a child path is genuinely required, the implementation **must** inject a matching exclude filter so the archive tree is never itself synced or recursively archived. This is a real failure mode: a `--backup-dir` inside the sync destination makes rclone see the archive as extra files on the destination and delete or re-archive them on the next run. Do not allow that configuration to save without the auto-exclude.

Validate at save time:
- Archive base is on the same remote **and the same share or bucket** as the side being modified. A cross-share move is not server-side and may fail or silently fall back to copy plus delete.
- Archive base does not equal or nest inside the synced path, unless the exclude is injected.
- Archive base is writable, probed with a temp file during Test.

### 7.2 Layout
- `timestamped_dir` (default): `<archive_base>/<job-slug>/<UTC ISO8601 run timestamp>/<original relative path>` via `--backup-dir`
- `suffix`: `--backup-dir` plus `--suffix .<timestamp>` and `--suffix-keep-extension`

For bidirectional jobs use `--backup-dir1` and `--backup-dir2` so each side archives locally. Never archive across remotes.

`archive_retention_days` drives an optional daily prune task. Off by default.

### 7.3 Cost
Where the archiving side supports server-side Move, archiving is a rename and nothing crosses the wire. Where it does not, archiving is a download plus upload, and the UI must warn at save time.

---

## 8. Dry run

Dry run must produce a reviewable, filterable, sortable table, not a wall of log text. Two phases:

**Phase 1, classification:**
```
rclone check SRC DST --combined - --checkers 16 [filters] --config <tmp>
```
`--combined` emits one line per path with a leading symbol: `=` identical, `-` only on path1, `+` only on path2, `*` differing, `!` error. Parse into `JobRunChange` rows.

**Phase 2, exact plan:**
```
rclone sync SRC DST --dry-run --use-json-log -v [all real run flags] --config <tmp>
```
Parse the JSON lines and reconcile against phase 1. This confirms what the real flag set would do, including archive destinations and filter effects.

For bidirectional, `rclone bisync PATH1 PATH2 --dry-run`, which requires the job to be initialized first. If it is not, the UI says so and offers First Sync instead of failing.

**Presentation:**
- Summary cards: new, updated, deleted, archived, unchanged, errors, total bytes, estimated duration
- Table grouped by action with a path filter box
- A prominent warning banner whenever deletions exceed `max_delete_pct`, shown before any live run is permitted
- A "Run live with these settings" button on the dry run result page

Persist dry runs like any other run, so two can be compared later.

---

## 9. Scheduling

- Cron expression per job, with a human readable preview ("Every day at 2:30 AM America/Denver") and the next five fire times
- Timezone per job, defaulting from `TZ`
- `coalesce=True` and a configurable `misfire_grace_time`, so a container restart does not fire a backlog
- "Run now" and "Dry run now" always available
- Optional global maintenance window during which scheduled runs are suppressed

---

## 10. Bidirectional sync

This is where naive implementations lose data. Required behavior:

1. **First run must be `--resync`.** Track `bisync_initialized`. Until true, the UI shows "First Sync required" with an explanation that resync makes path2 match path1 for conflicting files, and offers it as an explicit confirmation-gated action.
2. **Persistent workdir.** Pass `--workdir /config/bisync/<job-id>` so listing state survives restarts. Losing it forces another resync.
3. **Recovery path.** If bisync exits critically and demands a resync, surface that in the UI with a one-click, confirmation-gated resync. Never auto-resync.
4. Pass `--conflict-resolve` from job config, default `newer`, with `--conflict-loser num` so nothing is silently discarded.
5. Pass `--resilient` and `--recover` for unattended scheduled runs.
6. Not available with the `lftp` engine, or when either endpoint cannot write modification times.
7. Warn when pairing a case sensitive endpoint with a case insensitive one. The symptom is a sync that never converges.

---

## 11. Backend notes

### 11.1 Capability reference

Per the rclone backend overview, docs last updated 2026-04-21. Treat this as orientation only. The runtime probe in section 5.4 is the source of truth.

| Capability | SMB | SFTP | FTP | Local |
|---|---|---|---|---|
| Support tier | 2 (stable, minor gaps) | 1 (core) | 1 (core) | 1 (core) |
| Hashes | **none** | md5, sha1 | **none** | all |
| ModTime read/write | yes (files) | yes (files and dirs) | yes | yes |
| Case insensitive | yes | depends on OS | no | depends on FS |
| Server-side Move / DirMove | yes / yes | yes / yes | yes / yes | yes / yes |
| Server-side Copy | no | yes | no | no |
| Empty dirs | yes | yes | yes | yes |
| Free space (`about`) | yes | yes | no | yes |

Older forum posts claim the SMB backend lacks modtime support. That is out of date.

Consequences:
- SMB and FTP expose no hashes, so `compare_mode = checksum` must be disabled when either side is one of them. Default to mtime and size.
- Expose `modify_window` (default `1s`) on every job. If a NAS clock drifts or timestamp granularity differs, mtime comparison causes endless re-transfers of unchanged files. That symptom is the diagnostic.
- SMB supports server-side Move, so archiving on the NAS side is a cheap rename.
- SMB does not preserve POSIX ownership or permissions. Do not imply otherwise in the UI.

### 11.2 SMB remote shape

```ini
[synology]
type = smb
host = 10.0.20.15
user = svc-hivesync
pass = <rclone obscure output>
port = 445
domain = WORKGROUP
idle_timeout = 1m0s
hide_special_share = true
```

Path form `synology:Media/photos/2026`, where `Media` is the shared folder.

### 11.3 Synology / DSM filter preset

Ship this as a built-in `FilterPreset` named "Synology / DSM", selectable with one click in the job editor. DSM scatters metadata directories through every volume, and without these excludes they get synced, archived, and re-archived forever:

```
**/@eaDir/**
**/@tmp/**
/#recycle/**
**/#snapshot/**
**/.DS_Store
**/Thumbs.db
**/desktop.ini
```

`@eaDir` holds thumbnails and index data and appears at every directory level. `#recycle` appears at the share root when the shared folder Recycle Bin is enabled.

Ship a second built-in preset, "Common junk", with `.DS_Store`, `Thumbs.db`, `desktop.ini`, `*.tmp`, `~$*`, `.Trash-*`, `lost+found`.

### 11.4 DSM prerequisites for the README

- Control Panel, File Services, SMB enabled. Advanced Settings: maximum protocol SMB3, minimum SMB2.
- A dedicated DSM local user, for example `svc-hivesync`, with Read/Write on only the target shared folder. Not an administrator account.
- NAS NTP synced, for the modtime reason above.
- Decide whether the shared folder Recycle Bin stays on. With archive mode enabled rclone moves rather than deletes, so the recycle bin normally stays empty and the archive folder becomes the single restore point.
- Keep SMB on the LAN side only. Never expose it to the internet. The cloud endpoint should be SFTP.

### 11.5 CIFS mount fallback

If the SMB backend misbehaves, mount the share on the Docker host and bind mount it in, then configure it as a `local` connection:

```
//10.0.20.15/Media  /mnt/synology-media  cifs  credentials=/root/.smbcreds,vers=3.1.1,uid=1000,gid=1000,file_mode=0664,dir_mode=0775,nofail,_netdev  0 0
```

The local backend is tier 1 and supports all hash types, so pairing a cifs mount with an SFTP source enables real end-to-end checksum verification. The risk is that a stale mount looks like an empty directory, so enable the sentinel file check from section 6.4. Mount on the host, not inside the container, to avoid granting `SYS_ADMIN`.

This needs no extra code. The `local` connection type already covers it. It needs documentation and the sentinel check.

---

## 12. HTTP API

```
POST   /api/auth/login              session cookie, argon2 verify
POST   /api/auth/logout

GET    /api/connections
POST   /api/connections
GET    /api/connections/{id}
PATCH  /api/connections/{id}
DELETE /api/connections/{id}        409 if referenced by a job
POST   /api/connections/{id}/test   lsd + capability probe
GET    /api/connections/{id}/browse?path=
GET    /api/rclone/backends         from `rclone config providers`
GET    /api/rclone/remotes          imported remotes from the mounted config
POST   /api/rclone/parse-stanza     paste-an-ini-block helper

GET    /api/credentials             names and kinds only, never secrets
POST   /api/credentials
PATCH  /api/credentials/{id}
DELETE /api/credentials/{id}

GET    /api/jobs
POST   /api/jobs
GET    /api/jobs/{id}
PATCH  /api/jobs/{id}
DELETE /api/jobs/{id}
POST   /api/jobs/{id}/run           body: {mode: "dry_run"|"live"}
POST   /api/jobs/{id}/resync        bisync --resync, explicit and confirmed
GET    /api/jobs/{id}/runs
GET    /api/filter-presets

GET    /api/runs/{id}
GET    /api/runs/{id}/changes?action=&side=&page=
GET    /api/runs/{id}/log
GET    /api/runs/{id}/stream        SSE: live log lines and progress
POST   /api/runs/{id}/cancel

GET    /api/settings
PATCH  /api/settings
POST   /api/settings/test-notification
GET    /api/health                  liveness, binary versions, db ok
GET    /metrics                     Prometheus text format
```

---

## 13. UI screens

1. **Dashboard.** Job cards with last status, last run time, next run time, quick Dry Run and Run buttons, and a live-runs strip.
2. **Connections.** List with green/red test status. Editor as described in section 5. Test button and directory browser.
3. **Credentials.** Names and kinds. Add and replace only, never reveal.
4. **Jobs.** List plus a create/edit wizard: Endpoints, Direction, Deletion handling, Filters, Performance, Schedule, Review.
5. **Job detail.** Run history, current config, and buttons for Dry run, Run, Resync, Disable.
6. **Run detail.** Summary cards, filterable change table, raw log tab, live SSE tail while running, cancel button.
7. **Settings.** Auth, notifications, concurrency, retention, engine versions.

The Review step must state in plain English what the job will do, and render the resolved endpoints in rclone syntax so there is no ambiguity:

> Every day at 2:30 AM, copy new and changed files from `prod-sftp:/var/www` to `synology:Media/www`. Files deleted on the source will be moved to `synology:Media/www.hivesync-archive`. Nothing will be written back to `prod-sftp`.

Design direction: dark first, dense tables, monospace for paths, no marketing fluff.

---

## 14. Docker and configuration

**Dockerfile:** multi-stage, final stage on `python:3.12-slim`. Install rclone from the official release tarball at a pinned version rather than the distro package, which is usually stale. Install `lftp`, `openssh-client`, `ca-certificates`, `tzdata`. Create user `hivesync`, default UID/GID 1000, with `PUID`/`PGID` remapping in the entrypoint for NAS compatibility. `HEALTHCHECK` against `/api/health`.

Pin `RCLONE_VERSION=1.74.4` (released 2026-07-08). Version 1.75.0 shipped 2026-07-31 and is fine to move to later, but for a tool that deletes files, prefer the release that has had a few more weeks of field exposure. Record whichever version is used in `CLAUDE.md` and the README, because bisync flags in particular vary between versions.

**Volumes:**
- `/config` : SQLite DB, bisync workdirs, run logs, known_hosts, optional user-supplied `rclone/rclone.conf`
- `/data` : optional bind mounts for local filesystem connections

**Environment:**
```
HIVESYNC_SECRET_KEY       required, refuse to start without it, print a generated suggestion
HIVESYNC_ADMIN_USER       bootstrap admin, default admin
HIVESYNC_ADMIN_PASSWORD   bootstrap only, force change on first login if unset
HIVESYNC_AUTH_MODE        local | trusted_header | none
HIVESYNC_TRUSTED_HEADER   e.g. X-Authentik-Username
HIVESYNC_TRUSTED_PROXIES  CIDR allowlist, required when auth mode is trusted_header
HIVESYNC_LOG_LEVEL        info
RCLONE_CONFIG_PASS        optional, for an encrypted user-supplied config
TZ                        America/Denver
PUID / PGID               1000 / 1000
```

**compose:**
```yaml
services:
  hivesync:
    image: hivesync:latest
    container_name: hivesync
    restart: unless-stopped
    ports: ["8080:8080"]
    environment:
      HIVESYNC_SECRET_KEY: ${HIVESYNC_SECRET_KEY}
      TZ: America/Denver
      PUID: 1000
      PGID: 1000
    volumes:
      - ./config:/config
      - /mnt/tank/media:/data/media
      - /mnt/synology-media:/data/synology-media
```

---

## 15. Security

- Secrets encrypted with Fernet. The key is never persisted by the app.
- Credentials are write-only through the API. GET returns metadata only. No reveal feature.
- Redact secrets from every log line, exception traceback, and stored command.
- SFTP host key verification on by default, with an explicit per-connection trust-on-first-use that pins the fingerprint. Show the fingerprint and fail the run if it changes.
- Argon2id password hashing. HttpOnly, SameSite=Lax session cookies. CSRF token on state-changing form posts.
- Rate limit login attempts.
- `trusted_header` auth mode for running behind authentik or similar. Honor the header only when the request source is inside `HIVESYNC_TRUSTED_PROXIES`.
- No outbound telemetry.

---

## 16. Observability and notifications

- Targets: webhook (JSON POST), ntfy, and Apprise if it is cheap to add. Per job: never, failure only, always.
- Payload: job name, mode, status, counts, duration, deep link to the run.
- Structured JSON app logs to stdout. Per-run logs at `/config/logs/<job-id>/<run-id>.log` with size and count caps.
- `/metrics`: `hivesync_run_total{job,status}`, `hivesync_run_duration_seconds`, `hivesync_files_transferred_total`, `hivesync_bytes_transferred_total`, `hivesync_files_deleted_total`, `hivesync_files_archived_total`, `hivesync_last_success_timestamp{job}`.

---

## 17. Testing

`docker-compose.test.yml` with `atmoz/sftp`, `delfer/alpine-ftp-server`, `dperson/samba`, and a plain volume for local. Seed a deterministic tree via script.

Every engine feature gets a test asserting on resulting filesystem state, not on log strings.

Include a redaction test that greps every log file and every DB text column for known test secrets.

DSM's Samba build has its own quirks, so add a manual acceptance checklist to run against the real Synology before M6 is signed off: create, update, delete, rename, unicode filename, a file larger than 5 GB, and a deleted file landing correctly in the archive path.

---

## 18. Milestones

Work these in order, committing at each boundary. Do not start a milestone until the previous one's criteria pass. Confirm the plan for each milestone before writing its code.

**M0: Scaffold**
FastAPI app, SQLAlchemy models, Alembic baseline, Jinja layout, Tailwind, Dockerfile, compose, `/api/health`, pytest harness, `make test`.
*Accepts when:* `docker compose up` serves a login page, and health reports the pinned rclone and lftp versions.

**M1: Connections, credentials, capabilities**
CRUD, Fernet encryption, env-var and temp-file config generation, test endpoint, directory browser, `rclone_remote` in both modes, capability probe stored on the connection.
*Accepts when:*
- SFTP, FTP, and SMB connections each test green against the fixtures
- A remote defined only in the mounted `rclone.conf` can be selected, tested, and browsed, with no credentials written to the DB
- No plaintext secret appears anywhere in `/config` or the logs
- A hash-less backend causes the job editor to disable checksum comparison with a visible reason string

**M2: Engine abstraction and dry run**
A `SyncEngine` interface with `RcloneEngine`. Implement `plan()` first. Parse output into `JobRunChange`.
*Accepts when:* a dry run against a fixture holding one new, one changed, one deleted and one identical file produces exactly those four classifications and modifies nothing on either side.

**M3: One way live sync**
`execute()`, run lifecycle, log capture, SSE streaming, cancel, `--max-delete` brake, sentinel check.
*Accepts when:* a live run applies exactly the plan from M2, and a job whose source is emptied is refused by the delete brake.

**M4: Scheduler**
APScheduler with the persistent jobstore, cron editor with preview, overlap prevention, restart survival.
*Accepts when:* a `*/2 * * * *` job runs twice in five minutes, survives a container restart, and never double-runs.

**M5: Bidirectional**
bisync, resync gating, persistent workdir, conflict resolution, recovery UX.
*Accepts when:* files created independently on both sides converge, an edit-edit conflict produces a conflict-loser copy rather than data loss, and a wiped workdir surfaces the resync prompt instead of failing silently.

**M6: Deletion archiving**
`--backup-dir` and the bisync variants, default sibling path computation, overlap validation and auto-exclude, same-share validation, optional retention pruning.
*Accepts when:* a deleted source file lands in the archive with its relative path preserved, the archive tree is never itself synced or re-archived across three consecutive runs, the lftp plus archive combination is blocked with a clear explanation, and the manual Synology checklist passes.

**M7: Polish**
Notifications, `/metrics`, retention, filter presets, config export and import with secrets excluded, README including a Resilio migration walkthrough. The `LftpEngine` lands here, not earlier, and only if the answer to open question 1 is yes.

**M8: Hardening**
Host key pinning, login rate limiting, CSRF, the redaction test, full integration suite in compose.

---

## 19. Non-goals for v1

- Real-time filesystem-watch sync. This is scheduled sync. Resilio's continuous behavior is not reproduced. Say so in the README so expectations are set.
- Peer to peer or NAT traversal.
- Multi-user RBAC beyond a single admin role.
- Mobile app.

---

## 20. Open questions

1. Are the volumes large enough that lftp's segmented transfers actually matter, or can the lftp engine be dropped entirely? If dropped, remove it from the image and simplify section 2.
2. Is the cloud server reachable inbound from the local server, or must the container live on the cloud side and push?
3. Should archived deletions be tracked in the DB as restorable entries with one-click restore, or is the archive folder on disk sufficient?
4. Single admin behind authentik, or real multi-user?

Do not block M0 or M1 on these. Question 1 gates M7. Question 3 would extend M6.

---

## 21. Working agreement

See `CLAUDE.md`, which is the authoritative standing instruction file and must be kept current as the build progresses.

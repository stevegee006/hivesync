# HiveSync

A self-hosted, Docker-based file sync orchestrator with a web UI. Replaces Resilio
Sync for cloud-server-to-local-server directory syncing, adding multi-protocol
support, scheduling, dry runs, and deletion archiving.

HiveSync implements no file transfer protocols of its own. It drives `rclone` (and
optionally `lftp`) as subprocesses, adding scheduling, credential management, dry
run previews, deletion archiving, and a UI.

## Status: M8 complete

Working today, verified against the pinned rclone and live SFTP, FTP and SMB
fixtures:

- **Connections** for local paths, SFTP, FTP, FTPS and SMB, plus remotes defined
  in your own `rclone.conf`, which is read and never written. Each connection is
  tested and capability probed, so unsupported options disable themselves with a
  reason rather than failing mid-sync.
- **Credentials** encrypted at rest. No plaintext secret is ever written to
  `/config`, a log line, a stored command, or an API response.
- **Dry runs** that list every file that would be created, updated or deleted,
  before anything moves.
- **Live syncs** with a delete brake that refuses the run before it starts if too
  much of the destination would disappear, and cancellation that leaves nothing
  half written.
- **Scheduling** by cron expression with a concrete preview of the next five fire
  times, overlap prevention, and restart survival.
- **Bidirectional sync** over rclone bisync, with an explicit first sync, conflict
  handling that never discards the losing version, and recovery when the workdir
  is lost.
- **Deletion archiving**: deleted files move to an archive directory instead of
  disappearing, keeping their relative paths, under a per run timestamp.
- **Notifications** to a webhook or ntfy, **`/metrics`** for Prometheus,
  **retention** for archives, logs and run history, **filter presets**, and
  **configuration export and import**.
- **Hardening**: CSRF tokens on every state-changing request, login rate
  limiting that survives a restart, proxy-asserted identity for authentik and
  similar, and an image built from digest-pinned bases with hash-verified
  dependencies.

Not built: the `lftp` engine. The binary is in the image and the option exists,
but jobs that select it are refused. Whether segmented transfers are worth having
is `SPEC.md` open question 1, still unanswered.

**This is not a real time sync tool.** It is scheduled sync, plus manual runs.
Resilio's continuous behaviour is not reproduced: a file saved now syncs at the
next scheduled run, not a second later. If you need continuous replication, this
is the wrong tool. See `SPEC.md` section 19.

**Exposing it to the internet is still your risk to weigh.** M8 closed the
specific gaps that made it unsafe: CSRF, login rate limiting, host key pinning,
framing, and version disclosure. It has never had a third-party security review,
there is one admin role and no audit log, and it holds credentials for every
endpoint it syncs. A reverse proxy that authenticates in front of it is still the
better arrangement, with `HIVESYNC_AUTH_MODE=trusted_header`.

## Quick start

```bash
cp .env.example .env
```

Generate the encryption key and put it in `.env` as `HIVESYNC_SECRET_KEY`:

```bash
docker run --rm python:3.12-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Set `HIVESYNC_ADMIN_PASSWORD` in `.env` to anything at least 12 characters. You
will be forced to change it at first login, after which the variable can be
removed. Then:

```bash
docker compose up --build
```

Open http://localhost:8080 and sign in.

### About `HIVESYNC_SECRET_KEY`

Every stored credential is encrypted with this key, and HiveSync never persists
it. Two consequences:

- **Losing it means losing every stored credential.** Back it up somewhere your
  container host is not the only copy.
- **Changing it is detected.** A fingerprint is recorded at first boot, and
  HiveSync refuses to start against a database created with a different key rather
  than failing later with an unreadable credential mid-sync.

## Moving from Resilio Sync

Resilio replicates continuously between peers. HiveSync runs a scheduled sync
between two endpoints it connects to directly. The differences that matter before
you start:

| Resilio | HiveSync |
|---|---|
| Continuous, seconds after a change | Scheduled, or on demand |
| Peer to peer, works through NAT | Direct connections, so one side must be reachable from the container |
| Agent installed on both machines | Nothing installed on either endpoint, just SFTP, FTP or SMB access |
| Deleted files go to `.sync/Archive` | Deleted files go to an archive directory you choose, per run |
| Selective sync, placeholder files | Not reproduced. Use filters to exclude what you do not want |

### 1. Note what Resilio was actually doing

Before removing anything, write down for each folder: which machine holds the
authoritative copy, whether changes flow one way or both ways, and what its
ignore list contains. `.sync/IgnoreList` in the synced folder is the ignore list;
its patterns translate closely to HiveSync's exclude patterns.

### 2. Decide which side HiveSync connects from

HiveSync makes outbound connections; it does not accept them. Run the container
where it can reach both endpoints. For a cloud server and a home NAS, that is
usually the cloud server reaching in through your existing SFTP access, or the
NAS reaching out to the cloud server, whichever direction already has a route.
Nothing is peer to peer, so if neither side can reach the other, HiveSync cannot
help without a VPN or tunnel between them.

### 3. Create the connections

Connections, then New connection, one per endpoint. Test each one before moving
on: the test both verifies access and probes what the endpoint supports, and the
job editor uses that to grey out options that would fail later. SFTP asks you to
approve the host key the first time, which pins it.

### 4. Create the job, and dry run it first

Jobs, then New job. Pick source and destination, set the direction, and leave
**Extra files on the destination** at *leave them alone* for now.

Run a dry run. It lists every file that would be created, updated or deleted, and
it changes nothing on either side. Read it. This is the step that catches a
wrong subpath, and a wrong subpath with deletion enabled is how people lose
data.

### 5. Turn deletion handling on, once the dry run looks right

Switch to *move them to an archive*. The archive defaults to a directory beside
the destination, and the job editor shows you the resolved path. Check the delete
brake, which is 20% by default: a run that would remove more than that share of
the destination is refused before it starts, which is what protects you when a
mount fails and the source looks empty.

### 6. Let Resilio and HiveSync overlap for a few days

Leave Resilio running. Give HiveSync a schedule, let it run, and compare. Once a
few runs have applied exactly what their dry runs predicted, stop Resilio and
remove its `.sync` directories.

Add `.sync/**` to the job's excludes while both are running, or HiveSync will
happily replicate Resilio's own metadata.

Note the pattern has no leading `**/`. An rclone pattern with no leading slash
already matches at every level, and a leading `**/` requires at least one
directory in front of the name, so `**/.sync/**` would miss the `.sync` at the
top of the synced folder, which is where it is.

## Synology DSM notes

Two things need doing on the DSM side, and one thing needs excluding.

**Enable the SMB service and create a dedicated user.** Control Panel, File
Services, SMB. Give the user access to only the shared folders you intend to
sync. HiveSync addresses SMB as `Share/path`, so the share name is the first
element of the path, set separately on the connection.

**If you use SFTP instead**, enable it under Control Panel, Terminal & SNMP, and
note that DSM's SFTP chroots to the user's home directory by default, so paths
are relative to that rather than to the volume root.

**Exclude DSM's own metadata.** Apply the built-in *Synology / DSM* filter preset
to any job touching a DSM share. DSM creates `@eaDir` directories at every level
for thumbnails and index data, and `#recycle` at share roots when the recycle bin
is enabled. Without the preset these are synced to the other endpoint, then
archived, then archived again, and a dry run against a photo library becomes
unreadable.

**Checksums are unavailable on SMB.** No SMB server exposes a hash type, so jobs
with an SMB endpoint compare on modification time and size. The job editor says so
and disables the checksum option. If a NAS clock drifts, widen the modify window
rather than switching comparison modes.

## Local development

Requires Python 3.12 or newer. The `make` targets are POSIX sh and need Git Bash or
WSL on Windows, plus `make` itself, which Git for Windows does not include. Without
it, every target is a single command you can paste directly; `make help` lists them,
or read the Makefile.

```bash
make install
```

```bash
make dev
```

`make dev` keeps its state in `./config` instead of `/config`, and runs migrations
before starting.

```bash
make test
```

```bash
make lint
```

Front end assets (Tailwind CSS, htmx, Alpine) are built inside the image, so a
host checkout has none until you run `make assets`, which builds them in Docker
using the pinned versions. Without them the pages render unstyled but fully
functional: every page works without JavaScript.

## Volumes

| Path | Contents |
|---|---|
| `/config` | SQLite database, bisync workdirs, run logs, known_hosts, and an optional user-supplied `rclone/rclone.conf` |
| `/data` | Optional bind mounts for local filesystem connections |

`/config` ownership is set from `PUID` and `PGID` at container start. `/data` is
deliberately left alone: recursively chowning a mounted NAS share would take hours
across millions of inodes. Set host side permissions to match `PUID` and `PGID`
yourself.

A user-supplied `/config/rclone/rclone.conf` is opened read only and never
written to.

## Environment

See `.env.example` for the full list. Required: `HIVESYNC_SECRET_KEY`, and
`HIVESYNC_ADMIN_PASSWORD` on first start.

`HIVESYNC_AUTH_MODE` accepts `local` (default) and `none`. `none` gives every
visitor full control of every job, including deletion, and is only appropriate
behind a proxy that authenticates for you; it logs a warning at startup and shows
a persistent banner in the UI. `trusted_header` is declared in the spec but
refuses to start today, because a half-implemented proxy trust check is an
authentication bypass. It arrives in M8.

`HIVESYNC_MAX_CONCURRENT_RUNS` (default 3) sizes the worker pool for syncs and
dry runs. `HIVESYNC_METRICS_TOKEN` lets Prometheus scrape `/metrics` with a
bearer token; without it the endpoint requires a logged-in session. It is never
open, because job names appear in the metric labels and those are your share
names.

`HIVESYNC_API_TOKEN` authenticates the JSON API for scripts. Bearer-authenticated
requests skip the CSRF check, because a browser never attaches a bearer token on
its own, so there is nothing for a cross-site page to forge. Treat it as a full
admin credential.

`HIVESYNC_AUTH_MODE=trusted_header` accepts an identity asserted by a reverse
proxy. Set `HIVESYNC_TRUSTED_HEADER` to the header your proxy injects and
`HIVESYNC_TRUSTED_PROXIES` to its address range. The header is honoured only when
the connection's own peer address is inside that range, never on the strength of
an `X-Forwarded-For` the client could have written itself, and it maps to an
account that already exists: it will not create one.

`HIVESYNC_LOGIN_MAX_ATTEMPTS` (5) and `HIVESYNC_LOGIN_LOCKOUT_SECONDS` (900)
bound login attempts, counted per username and per source address and stored in
the database so a restart does not clear a lockout.

Everything else is configured from the Settings screen and stored in the
database: notification target, retention, and log limits.

## Security

What is in place:

- Credentials encrypted at rest with a key the application never persists, and
  never returned by any endpoint or written to any log. A sweep test greps every
  log file, database column, `/config` file, export document and API response for
  known sentinel values.
- CSRF tokens on every state-changing request, enforced in middleware so a new
  route is protected without anyone remembering to protect it. Login is included:
  without a token there, someone can log you into an account they control.
- Login rate limiting per username and per source address. A wrong password, an
  unknown username and a locked account are indistinguishable in the response.
- SFTP host keys pinned per connection on first use. A changed key fails the run
  and says so.
- Argon2id password hashing, HttpOnly SameSite=Lax session cookies, and a
  restrictive content security policy that refuses framing.
- `/api/health` reports liveness only. Binary versions moved to
  `/api/health/detail`, which needs authentication.
- Base images pinned by digest and Python dependencies installed with
  `--require-hashes`, so a re-uploaded artifact fails the build rather than
  shipping.
- No outbound telemetry. The only outbound requests are the ones you configure: a
  notification target, and the endpoints your jobs sync with.

What is not:

- One admin role. There is no per-user permission model and no audit log.
- No third-party security review.
- No secrets manager integration. The encryption key is an environment variable.

## Observability

`/metrics` serves Prometheus text format with one series per job:

| Series | Meaning |
|---|---|
| `hivesync_run_total{job,status}` | Runs recorded, including zeros for statuses that have not happened yet |
| `hivesync_run_duration_seconds_sum` / `_count` | Finished runs only |
| `hivesync_files_transferred_total{job}` | Live runs only, so a dry run does not move it |
| `hivesync_files_deleted_total{job}` | Everything that left the destination, archived files included |
| `hivesync_files_archived_total{job}` | The subset that landed in an archive |
| `hivesync_bytes_transferred_total{job}` | |
| `hivesync_last_success_timestamp{job}` | Unix time, zero meaning never |
| `hivesync_job_enabled{job}` | Distinguishes "stalled" from "someone turned it off" |

These are aggregates over the run history, not in-process counters, so a
container restart does not reset them.

Notifications go to a webhook (JSON POST) or ntfy, configured once under
Settings. Each job chooses whether it notifies never, on failure, or after every
run. A notification is sent after the run's outcome is committed and can never
change it: a webhook that is down produces a log line, not a failed sync.

Per-run logs are written to `/config/logs/<job-id>/<run-id>.log` and pruned by
the nightly maintenance pass, along with archived deletions past their retention
and run history beyond the per-job cap. Retention is off unless you set a number:
pruning an archive is the one operation here with nothing behind it.

## Pinned versions

| Tool | Version | How |
|---|---|---|
| Base images | pinned by digest | `python:3.12-slim` and `debian:trixie-slim`, by manifest digest |
| Python packages | exact, with hashes | `requirements.lock`, installed with `--require-hashes` |
| rclone | 1.74.4 | Official release zip, SHA256 verified against a pinned digest |
| lftp | 4.9.2 | Debian trixie, at the pinned base image digest, see below |
| Tailwind CLI | 4.3.3 | Standalone Go binary, no Node in the build |
| htmx | 2.0.10 | Vendored, not a CDN |
| Alpine.js | 3.15.12 | Vendored, not a CDN |

Versions live in `versions.env`. rclone is pinned by digest because a tool that
deletes files should not run an unverified binary; if that check fails, re-read the
published `SHA256SUMS` and update `versions.env` rather than bypassing it.

lftp is not pinned to an exact apt version, but the base image digest pins the
apt snapshot it comes from, so the same build produces the same lftp. Debian
trixie ships one lftp and only security patches it, so an exact apt pin would turn
every future point release into a build failure for no safety gain.
`make pin-versions` reports what the built image actually contains.

`requirements.txt` is the hand-edited list; `requirements.lock` is generated from
it with a hash for every distribution. After changing a dependency, run:

```bash
make lock-deps
```

Without that the build fails rather than silently installing something
unverified, which is the intended behaviour.

Base image digests are refreshed deliberately, not automatically:

```bash
docker buildx imagetools inspect python:3.12-slim --format '{{.Manifest.Digest}}'
```

bisync flags in particular vary between rclone versions, so record the version in
use when reporting a problem.

## Publishing to Docker Hub

Two prerequisites, both one-time.

Log in. Never put a registry token in this repo or in `.env`:

```bash
docker login
```

Register QEMU emulation, otherwise the arm64 half of the build fails with
`exec format error`. Docker Desktop ships the platform list but not the binfmt
handlers:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

Then build and push multi-arch, amd64 and arm64:

```bash
make push
```

This publishes `geaves006/hivesync:0.1.0` and `:latest`. Override the namespace
with `make push DOCKERHUB_NAMESPACE=other`.

A first push creates the Docker Hub repository as **public** by default. Create it
as private in Docker Hub beforehand if that is not what you want.

If arm64 is not needed, `--platform linux/amd64` alone skips the emulation
requirement entirely. The GitHub Actions workflow needs neither prerequisite: it
runs `docker/setup-qemu-action` and authenticates from repository secrets.

### Publishing from CI, the easier path

`.github/workflows/docker-publish.yml` needs neither prerequisite above: it
registers QEMU itself and authenticates from repository secrets. Set those two
secrets once, using a Docker Hub **access token**, not your password:

```bash
gh secret set DOCKERHUB_USERNAME
```

```bash
gh secret set DOCKERHUB_TOKEN
```

Create the token at https://app.docker.com/settings/personal-access-tokens with
Read and Write scope.

Then publishing is a tag:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow runs on every push to `main` and every pull request as well, but
**only a `v*` tag publishes**. Other runs build, test, and verify the image
without pushing, so a broken image cannot reach the registry. The verification
step starts the container and asserts that health returns `ok` with a matching
rclone version, that `/login` serves, and that PID 1 is not root, which is M0's
acceptance criterion checked on every commit.

## Testing

```bash
make test
```

Integration tests run against throwaway SFTP, FTP and SMB containers:

```bash
make test-integration
```

The fixtures and the test runner are both declared in `docker-compose.test.yml`,
so the suite runs from compose rather than from a hand-assembled `docker run`:

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
```

The integration suite uses all three fixtures: connection tests, capability
probes, dry runs, live syncs, bidirectional runs, deletion archiving and the
README walkthrough all run against real servers rather than against fakes.

## License

Not yet chosen.

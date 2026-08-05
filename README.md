# HiveSync

A self-hosted, Docker-based file sync orchestrator with a web UI. Replaces Resilio
Sync for cloud-server-to-local-server directory syncing, adding multi-protocol
support, scheduling, dry runs, and deletion archiving.

HiveSync implements no file transfer protocols of its own. It drives `rclone` (and
optionally `lftp`) as subprocesses, adding scheduling, credential management, dry
run previews, deletion archiving, and a UI.

## Status: M0, scaffold

This is the scaffold milestone. What works today:

- The container builds, starts, and serves a login page
- Local username and password authentication, argon2id, with a forced change of
  the bootstrap password at first login
- The full database schema and an Alembic baseline
- `/api/health`, reporting the pinned rclone and lftp versions

What does not exist yet: connections, credentials, jobs, dry runs, syncing,
scheduling, archiving, and notifications. Those arrive in M1 through M7. See
`SPEC.md` section 18 for the milestone plan and `CLAUDE.md` for the current state.

**This is not a real time sync tool.** It is scheduled sync. Resilio's continuous
behaviour is not reproduced. See `SPEC.md` section 19.

**Do not expose this to the internet yet.** CSRF protection, login rate limiting
and SFTP host key pinning land in M8. Until then it belongs on a trusted network,
optionally behind an authenticating reverse proxy.

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

## Local development

Requires Python 3.12 or newer. On Windows, run `make` targets from Git Bash or
WSL: the recipes are POSIX sh.

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

## Pinned versions

| Tool | Version | How |
|---|---|---|
| rclone | 1.74.4 | Official release zip, SHA256 verified against a pinned digest |
| lftp | Debian trixie | Not version pinned, see below |
| Tailwind CLI | 4.3.3 | Standalone Go binary, no Node in the build |
| htmx | 2.0.10 | Vendored, not a CDN |
| Alpine.js | 3.15.12 | Vendored, not a CDN |

Versions live in `versions.env`. rclone is pinned by digest because a tool that
deletes files should not run an unverified binary; if that check fails, re-read the
published `SHA256SUMS` and update `versions.env` rather than bypassing it.

lftp is deliberately not pinned to an exact apt version. Debian trixie ships one
lftp and only security patches it, so an exact pin turns every future point release
into a build failure for no safety gain. `make pin-versions` reports what the built
image actually contains.

bisync flags in particular vary between rclone versions, so record the version in
use when reporting a problem.

## Publishing to Docker Hub

Log in yourself first. Never put a registry token in this repo or in `.env`:

```bash
docker login
```

Then build and push multi-arch, amd64 and arm64:

```bash
make push
```

This publishes `geaves006/hivesync:0.1.0` and `:latest`. Override the namespace
with `make push DOCKERHUB_NAMESPACE=other`.

A first push creates the Docker Hub repository as **public** by default. Create it
as private in Docker Hub beforehand if that is not what you want.

For automated publishing, `.github/workflows/docker-publish.yml` does the same on a
tag push. It needs two repository secrets, `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN`, where the token is a Docker Hub access token rather than a
password.

## Testing

```bash
make test
```

Integration tests run against throwaway SFTP, FTP and SMB containers:

```bash
make test-integration
```

The fixtures are declared in `docker-compose.test.yml`. Nothing uses them at M0.

## License

Not yet chosen.

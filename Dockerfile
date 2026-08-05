# syntax=docker/dockerfile:1
#
# HiveSync image. Four stages: rclone, front end assets, python dependencies,
# runtime. Versions come from versions.env by way of the Makefile.
#
# Build with `make build`. A bare `docker build` also works, but then the
# defaults below apply instead of versions.env.

# ---------------------------------------------------------------------------
# rclone, from the official release rather than the distro package, which lags.
#
# Note for anyone updating this: rclone publishes .zip archives, not tarballs.
# The digest check is not optional. This binary deletes files for a living, and
# a corrupted or substituted download is not something to discover at run time.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS rclone

ARG RCLONE_VERSION=1.74.4
ARG RCLONE_SHA256_AMD64=fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d
ARG RCLONE_SHA256_ARM64=97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/rclone
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) expected="${RCLONE_SHA256_AMD64}" ;; \
      arm64) expected="${RCLONE_SHA256_ARM64}" ;; \
      *) echo "No pinned rclone digest for TARGETARCH=${TARGETARCH}." >&2; exit 1 ;; \
    esac; \
    archive="rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip"; \
    curl -fsSL --retry 3 --retry-delay 2 \
      -o "${archive}" "https://downloads.rclone.org/v${RCLONE_VERSION}/${archive}"; \
    echo "${expected}  ${archive}" > expected.sha256; \
    # If this fails, do not bypass it. Re-read the published SHA256SUMS, confirm
    # upstream did not republish the release, then update versions.env.
    sha256sum -c expected.sha256; \
    unzip -q "${archive}"; \
    install -m 0755 "rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" \
      /usr/local/bin/rclone; \
    rclone version | head -n 1

# ---------------------------------------------------------------------------
# Front end assets. Tailwind's standalone CLI is a single Go binary, which is how
# SPEC section 3 gets Tailwind with no Node build step. htmx and Alpine are
# vendored rather than loaded from a CDN, because a LAN sync tool should not need
# outbound internet access to render a page.
#
# Tailwind publishes no checksum file for these assets, so there is no digest to
# pin here. Lower stakes than rclone: this stage only produces CSS and JS.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS assets

ARG TAILWIND_VERSION=4.3.3
ARG HTMX_VERSION=2.0.10
ARG ALPINE_VERSION=3.15.12
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY app/web/templates ./app/web/templates
COPY app/web/static/css/tailwind.src.css ./app/web/static/css/tailwind.src.css

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) tw="tailwindcss-linux-x64" ;; \
      arm64) tw="tailwindcss-linux-arm64" ;; \
      *) echo "No Tailwind binary for TARGETARCH=${TARGETARCH}." >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 --retry-delay 2 -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/${tw}"; \
    chmod 0755 /usr/local/bin/tailwindcss; \
    mkdir -p /out/css /out/vendor; \
    tailwindcss \
      --input  app/web/static/css/tailwind.src.css \
      --output /out/css/app.css \
      --minify; \
    curl -fsSL --retry 3 -o /out/vendor/htmx.min.js \
      "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js"; \
    curl -fsSL --retry 3 -o /out/vendor/alpine.min.js \
      "https://unpkg.com/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js"; \
    test -s /out/css/app.css; \
    test -s /out/vendor/htmx.min.js; \
    test -s /out/vendor/alpine.min.js

# Export-only stage, so `make assets` can pull the built files onto the host
# without exporting an entire Debian filesystem.
FROM scratch AS assets-export
COPY --from=assets /out/ /

# ---------------------------------------------------------------------------
# Python dependencies in their own venv, so the runtime stage carries no build
# tooling. cryptography, argon2-cffi and pydantic-core all ship manylinux wheels
# for amd64 and arm64, so no compiler is needed here either.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS pydeps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG RCLONE_VERSION=1.74.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HIVESYNC_CONFIG_DIR=/config \
    HIVESYNC_EXPECTED_RCLONE_VERSION="${RCLONE_VERSION}"

# lftp is deliberately not version pinned. Debian trixie ships one lftp and only
# security patches it, so an exact apt pin turns every future point release into
# a build failure for no safety gain. The version is printed here and recorded by
# `make pin-versions`.
#
# openssh-client is here for ssh-keyscan, which the host key pinning in SPEC
# section 15 needs. rclone's sftp backend is pure Go and does not use it.
#
# The last line fails the build if an assumption about the base image is wrong,
# rather than letting the container die at start: setpriv comes from util-linux
# and usermod from passwd, both expected to be present already.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        lftp \
        openssh-client \
        ca-certificates \
        tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    lftp --version | head -n 1; \
    command -v setpriv usermod groupmod ssh-keyscan

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=pydeps /opt/venv /opt/venv

RUN set -eux; \
    groupadd --gid 1000 hivesync; \
    useradd --uid 1000 --gid 1000 --no-create-home --home-dir /config \
            --shell /usr/sbin/nologin hivesync; \
    mkdir -p /config /data /app; \
    chown hivesync:hivesync /config /data

WORKDIR /app
COPY --chown=hivesync:hivesync alembic.ini ./alembic.ini
COPY --chown=hivesync:hivesync migrations ./migrations
COPY --chown=hivesync:hivesync app ./app
# After the app copy, so a stale host-built stylesheet cannot shadow this one.
COPY --from=assets --chown=hivesync:hivesync /out/css/app.css ./app/web/static/css/app.css
COPY --from=assets --chown=hivesync:hivesync /out/vendor/ ./app/web/static/vendor/
COPY --chown=hivesync:hivesync docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

VOLUME ["/config"]
EXPOSE 8080

# python is already present, so no curl in the runtime image just for this.
# urlopen raises on the 503 that /api/health returns when the database is
# unreachable, so an unhealthy state is reported rather than masked.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", \
       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]

# Starts as root only to apply PUID/PGID, then drops to the hivesync user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---------------------------------------------------------------------------
# Test image. Adds the dev dependencies so the integration suite can run inside
# the container, against the pinned rclone rather than whatever is on a
# developer's PATH. Not part of the published image.
# ---------------------------------------------------------------------------
FROM runtime AS test

USER root
COPY pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir pytest pytest-asyncio
WORKDIR /src
ENTRYPOINT []
CMD ["pytest", "-m", "integration"]

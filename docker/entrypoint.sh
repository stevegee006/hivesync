#!/bin/sh
# HiveSync container entrypoint.
#
# Runs as root only long enough to apply PUID/PGID, then re-executes itself as
# the hivesync user and never returns to root.
#
# This file must have LF line endings. A CRLF in the shebang makes the container
# exit immediately with an opaque error. See .gitattributes.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" -eq 0 ]; then
    if [ "${PGID}" != "$(id -g hivesync)" ]; then
        groupmod -o -g "${PGID}" hivesync
    fi
    if [ "${PUID}" != "$(id -u hivesync)" ]; then
        usermod -o -u "${PUID}" hivesync
    fi

    # Only the directories HiveSync owns, and never recursively.
    #
    # /data is not touched on purpose. It is where local filesystem connections
    # get bind mounted, and recursively chowning a NAS share is a multi hour
    # operation across millions of inodes. Host side permissions stay the host's
    # responsibility, which the README says.
    mkdir -p /config /config/logs /config/bisync
    chown hivesync:hivesync /config /config/logs /config/bisync

    exec setpriv --reuid "${PUID}" --regid "${PGID}" --clear-groups "$0" "$@"
fi

cd /app

# Migrate before serving. A failure here stops the container rather than leaving
# a half migrated database answering requests.
alembic upgrade head

exec python -m app.main "$@"

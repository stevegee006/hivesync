"""rclone inspection commands: listing, probing, and enumerating backends.

Every output format here was read off rclone 1.74.4 rather than taken from the
spec. The shapes are recorded in CLAUDE.md under the verification section.

`rclone backend features` returns:
    Name, Root, String, Precision (int), Hashes (list[str]),
    Features (dict[str, bool], 52 keys), MetadataInfo

There is no file level "can set modification time" flag. rclone signals that
through Precision: a backend that cannot set modtimes reports the maximum int64.
That is what MODTIME_UNSUPPORTED_PRECISION below is for, and it is what gates
bidirectional sync.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.crypto import Redactor
from app.engines import process
from app.engines.rcloneconf import RCLONE, Prepared

logger = logging.getLogger(__name__)

# rclone reports this Precision when a backend cannot set modification times.
MODTIME_UNSUPPORTED_PRECISION = 9223372036854775807

LIST_TIMEOUT_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 45

# Applied to every network listing so a dead host fails fast instead of hanging a
# request for rclone's default retry budget.
_FAIL_FAST_ARGS = [
    "--contimeout",
    "10s",
    "--timeout",
    "20s",
    "--retries",
    "1",
    "--low-level-retries",
    "2",
]


class InspectError(Exception):
    """An rclone inspection command failed. The message is user facing."""


@dataclass(frozen=True)
class DirEntry:
    name: str
    is_dir: bool


def _parse_json(result: process.CommandResult, what: str) -> Any:
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        # Never echo raw stdout into the message: it can be large, and on some
        # backends it can quote back a value.
        raise InspectError(
            f"rclone returned output for {what} that could not be read as JSON. "
            "This usually means the installed rclone is not the expected version."
        ) from exc


def list_backends() -> list[dict[str, Any]]:
    """Every backend rclone supports, from `rclone config providers`.

    Never a hardcoded list: SPEC 5.2 requires the backend picker be populated at
    runtime. 69 providers in 1.74.4.
    """
    result = process.run(
        [RCLONE, "--config", "", "config", "providers"],
        timeout_seconds=LIST_TIMEOUT_SECONDS,
        log_label="config providers",
    )
    if not result.ok:
        raise InspectError(f"Could not list rclone backends. {result.failure_summary()}")
    providers = _parse_json(result, "the backend list")
    if not isinstance(providers, list):
        raise InspectError("The rclone backend list was not in the expected form.")
    return providers


def password_option_names(providers: list[dict[str, Any]], backend: str) -> list[str]:
    """Options rclone marks IsPassword for a backend, which must be obscured.

    Driven by rclone's own metadata rather than a table in this repo, which would
    rot the first time a backend gains an option.
    """
    for provider in providers:
        if provider.get("Name") == backend:
            return [
                str(option["Name"])
                for option in provider.get("Options", [])
                if option.get("IsPassword")
            ]
    return []


def sensitive_option_names(providers: list[dict[str, Any]], backend: str) -> list[str]:
    """Options rclone marks Sensitive, which must be redacted from logs.

    Broader than IsPassword: it includes host and user, which are not secrets but
    do not belong in a shared log either.
    """
    for provider in providers:
        if provider.get("Name") == backend:
            return [
                str(option["Name"])
                for option in provider.get("Options", [])
                if option.get("Sensitive")
            ]
    return []


def list_imported_remotes(config_path: Path) -> list[str]:
    """Remotes defined in the user supplied config. Opened read only, never written.

    SPEC 5.2: if the file is missing, say so clearly rather than creating one.
    """
    if not config_path.is_file():
        raise InspectError(
            f"No rclone config file is mounted at {config_path}. Mount your "
            "rclone.conf there, read only, to use imported remotes."
        )
    result = process.run(
        [RCLONE, "--config", str(config_path), "listremotes"],
        timeout_seconds=LIST_TIMEOUT_SECONDS,
        log_label="listremotes",
    )
    if not result.ok:
        summary = result.failure_summary()
        if "password" in summary.lower() or "encrypted" in summary.lower():
            raise InspectError(
                "The mounted rclone config appears to be encrypted. Set "
                "RCLONE_CONFIG_PASS in the environment so it can be read."
            )
        raise InspectError(f"Could not read the mounted rclone config. {summary}")
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


def probe_features(prepared: Prepared, alias: str) -> dict[str, Any]:
    """Run the capability probe for one endpoint. SPEC 5.4."""
    endpoint = prepared.endpoints[alias]
    result = process.run(
        prepared.argv("backend", "features", f"{endpoint.alias}:", *_FAIL_FAST_ARGS),
        env=prepared.env,
        redactor=prepared.redactor,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        log_label="backend features",
    )
    if not result.ok:
        raise InspectError(f"The capability probe failed. {result.failure_summary()}")
    payload = _parse_json(result, "the capability probe")
    if not isinstance(payload, dict) or "Features" not in payload:
        raise InspectError("The capability probe returned an unexpected structure.")
    return payload


def list_dirs(prepared: Prepared, alias: str, subpath: str | None = None) -> list[DirEntry]:
    """Directory listing for the browser, via `rclone lsf`. SPEC 5.5.

    `--dirs-only` is not used: the picker shows files as context, and a directory
    is identified by the trailing slash that lsf appends.
    """
    endpoint = prepared.endpoints[alias]
    # RemoteConfigError propagates deliberately. A rejected path is the caller
    # asking for something not allowed, which is a client error, not a failure of
    # the remote. The API layer maps the two to different status codes.
    target = endpoint.spec(subpath)

    result = process.run(
        prepared.argv("lsf", "--format", "p", "--dir-slash", target, *_FAIL_FAST_ARGS),
        env=prepared.env,
        redactor=prepared.redactor,
        timeout_seconds=LIST_TIMEOUT_SECONDS,
        log_label="lsf",
    )
    if not result.ok:
        raise InspectError(f"Could not list that directory. {result.failure_summary()}")

    entries: list[DirEntry] = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        if name.endswith("/"):
            entries.append(DirEntry(name=name.rstrip("/"), is_dir=True))
        else:
            entries.append(DirEntry(name=name, is_dir=False))
    entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
    return entries


def check_reachable(prepared: Prepared, alias: str) -> process.CommandResult:
    """`rclone lsd` against the base path, the core of a connection test.

    Returns the raw result so the caller can store the diagnostics on failure
    rather than turning them into an exception.
    """
    endpoint = prepared.endpoints[alias]
    return process.run(
        prepared.argv("lsd", endpoint.spec(), *_FAIL_FAST_ARGS),
        env=prepared.env,
        redactor=prepared.redactor,
        timeout_seconds=LIST_TIMEOUT_SECONDS,
        log_label="lsd",
    )


def scan_host_key(host: str, port: int) -> tuple[str, str] | None:
    """Fetch a host's SSH key with ssh-keyscan, for trust on first use.

    Returns (key_type_and_value, sha256_fingerprint) or None if unreachable.
    The value returned is a public key. It is not secret, and it is what gets
    pinned on the connection and written into a known_hosts file at run time.
    """
    result = process.run(
        ["ssh-keyscan", "-p", str(port), "-T", "10", host],
        timeout_seconds=20,
        log_label="ssh-keyscan",
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        # Format: host keytype base64key
        if len(parts) >= 3 and not line.startswith("#"):
            key = f"{parts[1]} {parts[2]}"
            return key, _fingerprint(parts[1], parts[2])
    return None


def _fingerprint(key_type: str, key_b64: str) -> str:
    """SHA256 fingerprint in the form OpenSSH displays."""
    import base64
    import hashlib

    try:
        raw = base64.b64decode(key_b64)
    except ValueError:
        return ""
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"{key_type} SHA256:{digest}"


def redactor_for(values: list[str]) -> Redactor:
    return Redactor(values)

"""External binary discovery for /api/health.

Version strings are parsed from the text output of `rclone version` and
`lftp --version`. Neither is assumed to have a JSON mode: CLAUDE.md rule 3
forbids inventing flags, and no JSON output flag was verified for either tool in
the pinned versions. If one is confirmed later, switch to it and record the
finding in the gotchas log.

Every command here is a fixed list[str]. There is no shell, and no user input
reaches an argument.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

_PROBE_TIMEOUT_SECONDS = 10

# `rclone version` first line: "rclone v1.74.4"
_RCLONE_VERSION_RE = re.compile(r"^rclone\s+v?(?P<version>[0-9][^\s]*)", re.MULTILINE)
# `lftp --version` first line: "LFTP | Version 4.9.2 | Copyright (c) ..."
_LFTP_VERSION_RE = re.compile(r"Version\s+(?P<version>[0-9][^\s|]*)")
# Last resort for either tool if upstream reformats its banner.
_ANY_VERSION_RE = re.compile(r"(?P<version>\d+\.\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class BinaryInfo:
    """Discovered state of one external binary. Never raises, always reports."""

    name: str
    ok: bool
    version: str | None = None
    path: str | None = None
    error: str | None = None


def _probe(name: str, args: list[str], patterns: list[re.Pattern[str]]) -> BinaryInfo:
    path = shutil.which(name)
    if path is None:
        return BinaryInfo(
            name=name,
            ok=False,
            error=(
                f"{name} was not found on PATH. In the container this means the "
                "image is built wrong. Running outside the container, it means the "
                "binary is not installed locally."
            ),
        )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [path, *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return BinaryInfo(
            name=name,
            ok=False,
            path=path,
            error=f"{name} did not respond within {_PROBE_TIMEOUT_SECONDS} seconds.",
        )
    except OSError as exc:
        return BinaryInfo(name=name, ok=False, path=path, error=f"{name} could not run: {exc}")

    # Some tools print their banner to stderr, so consider both streams.
    output = f"{completed.stdout}\n{completed.stderr}"
    for pattern in patterns:
        match = pattern.search(output)
        if match:
            return BinaryInfo(name=name, ok=True, version=match.group("version"), path=path)

    return BinaryInfo(
        name=name,
        ok=False,
        path=path,
        error=f"{name} ran but its version output was not recognised.",
    )


def rclone_info() -> BinaryInfo:
    return _probe("rclone", ["version"], [_RCLONE_VERSION_RE, _ANY_VERSION_RE])


def lftp_info() -> BinaryInfo:
    return _probe("lftp", ["--version"], [_LFTP_VERSION_RE, _ANY_VERSION_RE])


@dataclass(frozen=True)
class BinaryReport:
    rclone: BinaryInfo
    lftp: BinaryInfo
    expected_rclone_version: str | None

    @property
    def rclone_matches_expected(self) -> bool | None:
        """Whether the installed rclone is the one the image intended to install.

        None when the image did not declare an expectation. A False here means the
        binary is not the reviewed, pinned build, which matters for a tool whose
        bisync flags vary between versions.
        """
        if self.expected_rclone_version is None or self.rclone.version is None:
            return None
        return self.rclone.version == self.expected_rclone_version

    @property
    def all_ok(self) -> bool:
        return self.rclone.ok and self.lftp.ok and self.rclone_matches_expected is not False


def collect(expected_rclone_version: str | None) -> BinaryReport:
    """Probe both binaries. Called once at startup and cached on app state."""
    return BinaryReport(
        rclone=rclone_info(),
        lftp=lftp_info(),
        expected_rclone_version=expected_rclone_version,
    )

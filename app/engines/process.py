"""Subprocess invocation for external engines.

Every command is a `list[str]`. There is no shell anywhere, and no user supplied
value is ever interpolated into a string that a shell would parse.

Secrets reach rclone through the environment, never through argv, so a captured
command is safe to store. The redactor is applied anyway, because output can echo
a value back and because defence in depth costs nothing here.

This module is the shared subprocess primitive. The streaming, cancellable run
supervision that SPEC section 6.3 needs belongs to the runner at M3; what is here
is the bounded, capture-everything call that connection tests and probes use.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.crypto import Redactor

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30

# Bound captured output so a backend that dumps a million lines cannot exhaust
# memory or fill a log line.
_MAX_CAPTURE_CHARS = 512 * 1024


class ProcessTimeout(Exception):
    """The command exceeded its wall clock budget and was killed."""


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command. Never carries an unredacted secret."""

    argv_redacted: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Populated only when the caller asks, since most callers do not need it.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def command_line(self) -> str:
        return " ".join(self.argv_redacted)

    def failure_summary(self) -> str:
        """A user facing description of what went wrong.

        rclone puts its diagnostics on stderr, and the last line is usually the
        actionable one, so prefer it over the first.
        """
        if self.timed_out:
            return "The command did not finish within its time limit."
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        if lines:
            return lines[-1]
        if self.stdout.strip():
            return self.stdout.strip().splitlines()[-1]
        return f"The command failed with exit code {self.exit_code} and no output."


def _truncate(text: str) -> str:
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text
    return text[:_MAX_CAPTURE_CHARS] + "\n[output truncated]"


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    redactor: Redactor | None = None,
    stdin_text: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    log_label: str | None = None,
) -> CommandResult:
    """Run a command to completion with a hard timeout. Never raises on failure.

    `stdin_text` exists so a plaintext secret can be handed to `rclone obscure -`
    without it appearing in argv or on disk. It is never logged.

    A non-zero exit is a result, not an exception: a failed connection test is
    ordinary and the caller needs the diagnostics, not a traceback.
    """
    redactor = redactor or Redactor([])
    argv_list = list(argv)
    argv_redacted = redactor.redact_argv(argv_list)

    # Overlay onto the current environment rather than replacing it. Passing a
    # bare dict to subprocess wipes PATH, so rclone stops being findable, and it
    # would also drop RCLONE_CONFIG_PASS, which SPEC section 5.2 requires be
    # sourced from the environment for an encrypted user config. Our own
    # RCLONE_CONFIG_* entries win over anything inherited.
    child_env = {**os.environ, **env} if env is not None else None

    if log_label:
        logger.info(
            "Running external command",
            extra={"label": log_label, "command": " ".join(argv_redacted)},
        )

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv list, shell is never used
            argv_list,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired as expired:
        # TimeoutExpired carries whatever was captured before the kill.
        stdout = expired.stdout or ""
        stderr = expired.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        logger.warning(
            "External command timed out",
            extra={"label": log_label, "command": " ".join(argv_redacted)},
        )
        return CommandResult(
            argv_redacted=argv_redacted,
            exit_code=124,
            stdout=redactor.redact(_truncate(stdout)),
            stderr=redactor.redact(_truncate(stderr)),
            timed_out=True,
        )
    except FileNotFoundError:
        return CommandResult(
            argv_redacted=argv_redacted,
            exit_code=127,
            stdout="",
            stderr=f"{argv_list[0]} was not found on PATH.",
        )
    except OSError as exc:
        return CommandResult(
            argv_redacted=argv_redacted,
            exit_code=126,
            stdout="",
            stderr=f"{argv_list[0]} could not be started: {exc}",
        )

    return CommandResult(
        argv_redacted=argv_redacted,
        exit_code=completed.returncode,
        stdout=redactor.redact(_truncate(completed.stdout or "")),
        stderr=redactor.redact(_truncate(completed.stderr or "")),
    )

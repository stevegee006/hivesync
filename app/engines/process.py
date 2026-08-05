"""Subprocess invocation for external engines.

Every command is a `list[str]`. There is no shell anywhere, and no user supplied
value is ever interpolated into a string that a shell would parse.

Secrets reach rclone through the environment, never through argv, so a captured
command is safe to store. The redactor is applied anyway, because output can echo
a value back and because defence in depth costs nothing here.

Two shapes live here. `run()` is the bounded, capture-everything call that
connection tests and probes use. `stream()` is the long-lived, cancellable call a
live sync needs: it yields output as it arrives and exposes the process so it can
be signalled.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
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


@dataclass
class StreamingProcess:
    """A running command whose output is consumed as it arrives.

    Exists so a live sync can be watched and cancelled. SPEC section 6.3 asks for
    SIGTERM, a ten second grace, then SIGKILL.

    Verified against rclone 1.74.4: on SIGTERM it removes the partial file it was
    writing and exits cleanly, so a cancelled transfer leaves no half-written file
    under its final name. A SIGKILL skips that handler, so the grace period is not
    cosmetic: killing early can leave a `<name>.<id>.partial` behind, which the
    next sync sees as an extra file on the destination.
    """

    argv_redacted: list[str]
    _popen: subprocess.Popen[str]
    _redactor: Redactor

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def command_line(self) -> str:
        return " ".join(self.argv_redacted)

    def lines(self) -> Iterator[str]:
        """Yield redacted output lines as the process produces them."""
        if self._popen.stdout is None:  # pragma: no cover - always piped by stream()
            return
        for raw in self._popen.stdout:
            yield self._redactor.redact(raw.rstrip("\n"))

    def wait(self, timeout: float | None = None) -> int:
        return self._popen.wait(timeout=timeout)

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def terminate(self, *, grace_seconds: float = 10.0) -> None:
        """SIGTERM, then SIGKILL if it does not stop. SPEC section 6.3.

        The grace period matters: rclone cleans up its partial file on SIGTERM
        and cannot on SIGKILL.
        """
        if self._popen.poll() is not None:
            return
        logger.info("Terminating command", extra={"pid": self.pid})
        self._popen.terminate()
        try:
            self._popen.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "Command ignored SIGTERM, killing. A partial file may be left behind.",
                extra={"pid": self.pid},
            )
        self._popen.kill()
        self._popen.wait(timeout=5)


def stream(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    redactor: Redactor | None = None,
    log_label: str | None = None,
) -> StreamingProcess:
    """Start a command and return it for line by line consumption.

    stderr is merged into stdout so the caller sees one ordered stream. rclone
    writes its JSON log to stderr, which is where the interesting output is.
    """
    redactor = redactor or Redactor([])
    argv_list = list(argv)
    argv_redacted = redactor.redact_argv(argv_list)

    # Same reasoning as run(): overlay, never replace, or PATH disappears.
    child_env = {**os.environ, **env} if env is not None else None

    if log_label:
        logger.info(
            "Starting external command",
            extra={"label": log_label, "command": " ".join(argv_redacted)},
        )

    popen = subprocess.Popen(  # noqa: S603 - fixed argv list, shell is never used
        argv_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=child_env,
    )
    return StreamingProcess(argv_redacted=argv_redacted, _popen=popen, _redactor=redactor)

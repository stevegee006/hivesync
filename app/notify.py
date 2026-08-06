"""Outbound notifications. SPEC section 16.

Two targets: a JSON webhook and ntfy. **Apprise is deliberately not here.** It is
a large dependency tree wrapping HTTP calls we already make, and every service it
adds is one nothing in this repository can test. If a specific service is wanted
later, adding it as a target is a small change; adding Apprise is a large one
that only appears to cover more ground.

Three rules, all of which exist because a notification is the least important
thing happening when it fires:

1. **Never raise into the caller.** A failed notification is logged and returned
   as a failure result. A sync that worked must not be recorded as failed
   because a webhook was down.
2. **Never run inside the run's transaction.** Sending happens after the run
   record is committed, so a slow endpoint cannot hold a SQLite write lock. This
   is the same reason live output goes through a broker rather than the database.
3. **Bounded time.** The timeout is a preference with a ceiling, so a hung
   endpoint cannot pin a worker thread indefinitely.

A webhook URL often carries its own token in the path, so it is treated as
sensitive: never logged, never exported, never returned by the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import Job, JobRun, NotifyOn, RunStatus
from app.preferences import Preferences

logger = logging.getLogger(__name__)

# Statuses that count as "something went wrong" for a job set to failure only.
FAILURE_STATUSES = frozenset({RunStatus.failed, RunStatus.cancelled})


@dataclass(frozen=True)
class Delivery:
    """What happened when we tried to send. Never raised, always returned."""

    attempted: bool
    ok: bool
    detail: str

    @property
    def skipped(self) -> bool:
        return not self.attempted


def should_notify(job: Job, status: RunStatus) -> bool:
    """SPEC 16: per job, never / failure only / always.

    A skipped run notifies nobody under "failure only": nothing failed, a run was
    declined because another was in progress. It does notify under "always",
    because someone asking for every run wants to know a run did not happen.
    """
    if job.notify_on == NotifyOn.never:
        return False
    if job.notify_on == NotifyOn.always:
        return True
    return status in FAILURE_STATUSES


def build_payload(job: Job, run: JobRun, *, base_url: str = "") -> dict[str, Any]:
    """The payload SPEC 16 specifies: name, mode, status, counts, duration, link.

    Deliberately flat and stable: this is consumed by whatever someone points it
    at, so the shape is part of the contract rather than an internal detail.
    """
    duration: float | None = None
    if run.started_at and run.finished_at:
        duration = round((run.finished_at - run.started_at).total_seconds(), 3)

    payload: dict[str, Any] = {
        "job": job.name,
        "job_id": job.id,
        "run_id": run.id,
        "mode": run.mode.value,
        "trigger": run.trigger.value,
        "status": run.status.value,
        "duration_seconds": duration,
        "files_transferred": run.files_transferred,
        "files_deleted": run.files_deleted,
        "files_archived": run.files_archived,
        "bytes_transferred": run.bytes_transferred,
        "errors": run.errors_count,
    }
    if run.skip_reason:
        payload["skip_reason"] = run.skip_reason
    # The error text, when there is one, is the whole reason someone opens this.
    if isinstance(run.summary, dict) and run.summary.get("error"):
        payload["error"] = str(run.summary["error"])
    if base_url:
        payload["url"] = f"{base_url.rstrip('/')}/runs/{run.id}"
    return payload


def summarise(payload: dict[str, Any]) -> tuple[str, str]:
    """A title and body for targets that take text rather than JSON."""
    title = f"HiveSync: {payload['job']} {payload['status']}"
    lines = [
        # Repeated in the body because the title is degraded to ascii for the
        # header, and a name outside ascii is unreadable by the time it lands.
        f"{payload['job']}: {payload['status']}",
        f"{payload['files_transferred']} transferred, "
        f"{payload['files_deleted']} removed, "
        f"{payload['files_archived']} archived",
    ]
    if payload.get("duration_seconds") is not None:
        lines.append(f"took {payload['duration_seconds']}s")
    if payload.get("error"):
        lines.append(str(payload["error"]))
    if payload.get("skip_reason"):
        lines.append(str(payload["skip_reason"]))
    if payload.get("url"):
        lines.append(str(payload["url"]))
    return title, "\n".join(lines)


def send(preferences: Preferences, payload: dict[str, Any]) -> Delivery:
    """Deliver one payload to the configured target.

    Returns rather than raises, always. See rule 1 in the module docstring.
    """
    target = preferences.notify_target
    if target == "none":
        return Delivery(attempted=False, ok=False, detail="No notification target is configured.")

    try:
        if target == "webhook":
            return _send_webhook(preferences, payload)
        return _send_ntfy(preferences, payload)
    except httpx.TimeoutException:
        detail = (
            f"The notification target did not respond within "
            f"{preferences.notify_timeout_seconds} seconds."
        )
    except httpx.HTTPError as exc:
        # str(exc) can contain the URL, which can contain a token.
        detail = f"The notification could not be delivered: {type(exc).__name__}."
    except Exception:
        detail = "The notification could not be delivered because of an unexpected error."
        logger.exception("Notification failed unexpectedly", extra={"target": target})

    logger.warning("Notification failed", extra={"target": target, "detail": detail})
    return Delivery(attempted=True, ok=False, detail=detail)


def _send_webhook(preferences: Preferences, payload: dict[str, Any]) -> Delivery:
    url = preferences.notify_webhook_url.strip()
    if not url:
        return Delivery(
            attempted=False,
            ok=False,
            detail="The webhook target is selected but no URL is set.",
        )
    response = httpx.post(url, json=payload, timeout=preferences.notify_timeout_seconds)
    return _from_response(response, "webhook")


def _send_ntfy(preferences: Preferences, payload: dict[str, Any]) -> Delivery:
    topic = preferences.notify_ntfy_topic.strip().strip("/")
    server = preferences.notify_ntfy_server.strip().rstrip("/")
    if not topic or not server:
        return Delivery(
            attempted=False,
            ok=False,
            detail="The ntfy target is selected but the server or topic is not set.",
        )
    title, body = summarise(payload)
    # ntfy takes the message as the body and everything else as headers. httpx
    # encodes header values as **ascii**, not latin-1, and raises on anything
    # else, so a job named "Sauvegarde éphémère" would fail the whole request
    # rather than the header. The body is UTF-8 and carries the real name.
    response = httpx.post(
        f"{server}/{topic}",
        content=body.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Tags": (
                "white_check_mark" if payload["status"] == RunStatus.success.value else "warning"
            ),
            "Priority": "high" if payload["status"] == RunStatus.failed.value else "default",
        },
        timeout=preferences.notify_timeout_seconds,
    )
    return _from_response(response, "ntfy")


def _from_response(response: httpx.Response, target: str) -> Delivery:
    if response.is_success:
        logger.info("Notification sent", extra={"target": target, "status": response.status_code})
        return Delivery(attempted=True, ok=True, detail=f"Delivered, HTTP {response.status_code}.")
    detail = (
        f"The target returned HTTP {response.status_code}. Check the URL and any token it needs."
    )
    logger.warning(
        "Notification rejected", extra={"target": target, "status": response.status_code}
    )
    return Delivery(attempted=True, ok=False, detail=detail)


def sample_payload() -> dict[str, Any]:
    """The payload behind the Settings screen's test button.

    Shaped exactly like a real one so that what someone tests is what they get.
    """
    return {
        "job": "Test notification",
        "job_id": 0,
        "run_id": 0,
        "mode": "live",
        "trigger": "manual",
        "status": "success",
        "duration_seconds": 1.5,
        "files_transferred": 3,
        "files_deleted": 0,
        "files_archived": 1,
        "bytes_transferred": 4096,
        "errors": 0,
    }

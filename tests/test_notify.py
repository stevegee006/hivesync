"""Notifications. SPEC section 16.

The acceptance criterion for M7: a failed run posts exactly one webhook with the
specified payload, a successful run under "failure only" posts none, and a target
that times out fails the notification rather than the run.

Delivery is exercised against a real local HTTP server rather than a mocked
httpx. What is being tested is that a payload actually leaves the process in the
shape someone's receiver will parse, and a mock asserts only that we called our
own code the way we expected to.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from app import notify
from app.models import Job, JobRun, NotifyOn, RunMode, RunStatus, RunTrigger
from app.preferences import Preferences


class _Receiver(BaseHTTPRequestHandler):
    """Records what arrives. Behaviour is set per test on the server object."""

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        server: Any = self.server
        server.received.append(
            {
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
        )
        if server.delay:
            threading.Event().wait(server.delay)
        self.send_response(server.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr logging."""


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    server.received = []  # type: ignore[attr-defined]
    server.status = 200  # type: ignore[attr-defined]
    server.delay = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: HTTPServer, path: str = "/hook") -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}{path}"


def _job(**overrides: object) -> Job:
    job = Job(name="Nightly Media", notify_on=NotifyOn.failure, filters={})
    job.id = 7
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _run(**overrides: object) -> JobRun:
    started = datetime(2026, 8, 5, 2, 30, tzinfo=UTC)
    run = JobRun(
        job_id=7,
        trigger=RunTrigger.schedule,
        mode=RunMode.live,
        status=RunStatus.failed,
        started_at=started,
        finished_at=started + timedelta(seconds=12, milliseconds=500),
        files_transferred=4,
        files_deleted=2,
        files_archived=2,
        bytes_transferred=2048,
        errors_count=1,
        summary={"error": "The source is not mounted."},
    )
    run.id = 42
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


# --------------------------------------------------------------------------
# Who gets told
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "status", "expected"),
    [
        (NotifyOn.never, RunStatus.failed, False),
        (NotifyOn.never, RunStatus.success, False),
        (NotifyOn.failure, RunStatus.failed, True),
        (NotifyOn.failure, RunStatus.cancelled, True),
        (NotifyOn.failure, RunStatus.success, False),
        (NotifyOn.failure, RunStatus.skipped, False),
        (NotifyOn.always, RunStatus.success, True),
        (NotifyOn.always, RunStatus.skipped, True),
    ],
)
def test_notify_on_decides_who_hears(setting: NotifyOn, status: RunStatus, expected: bool) -> None:
    """A skipped run is not a failure: nothing broke, a run was declined because
    another was in progress. Someone asking for every run still wants it."""
    assert notify.should_notify(_job(notify_on=setting), status) is expected


# --------------------------------------------------------------------------
# The payload SPEC 16 specifies
# --------------------------------------------------------------------------


def test_payload_carries_name_mode_status_counts_duration_and_link() -> None:
    payload = notify.build_payload(_job(), _run(), base_url="http://nas.local:8080/")
    assert payload["job"] == "Nightly Media"
    assert payload["mode"] == "live"
    assert payload["status"] == "failed"
    assert payload["files_transferred"] == 4
    assert payload["files_deleted"] == 2
    assert payload["files_archived"] == 2
    assert payload["bytes_transferred"] == 2048
    assert payload["duration_seconds"] == 12.5
    # A deep link to the run, with exactly one slash.
    assert payload["url"] == "http://nas.local:8080/runs/42"
    # The reason, which is the whole point of opening the notification.
    assert payload["error"] == "The source is not mounted."


def test_payload_omits_the_link_when_no_address_is_configured() -> None:
    assert "url" not in notify.build_payload(_job(), _run())


def test_a_skipped_run_says_why() -> None:
    run = _run(status=RunStatus.skipped, skip_reason="A previous run was still in progress.")
    payload = notify.build_payload(_job(), run)
    assert payload["skip_reason"] == "A previous run was still in progress."


# --------------------------------------------------------------------------
# Delivery, against a real socket
# --------------------------------------------------------------------------


def test_a_failed_run_posts_exactly_one_webhook(receiver) -> None:
    preferences = Preferences(notify_target="webhook", notify_webhook_url=_url(receiver))
    payload = notify.build_payload(_job(), _run())

    delivery = notify.send(preferences, payload)

    assert delivery.ok is True
    assert len(receiver.received) == 1, "a notification must not be sent twice"
    body = json.loads(receiver.received[0]["body"])
    assert body["job"] == "Nightly Media"
    assert body["status"] == "failed"
    assert body["files_archived"] == 2


def test_a_rejected_webhook_is_reported_rather_than_raised(receiver) -> None:
    """Rule 1: a sync that worked must not be recorded as failed because a
    webhook was down."""
    receiver.status = 500
    preferences = Preferences(notify_target="webhook", notify_webhook_url=_url(receiver))

    delivery = notify.send(preferences, notify.sample_payload())

    assert delivery.attempted is True
    assert delivery.ok is False
    assert "500" in delivery.detail


def test_a_timeout_fails_the_notification_and_says_so(receiver) -> None:
    receiver.delay = 2
    preferences = Preferences(
        notify_target="webhook",
        notify_webhook_url=_url(receiver),
        notify_timeout_seconds=1,
    )

    delivery = notify.send(preferences, notify.sample_payload())

    assert delivery.ok is False
    assert "did not respond within 1 seconds" in delivery.detail


def test_an_unreachable_target_does_not_leak_the_url() -> None:
    """The URL can carry a token, so it must not reach a message someone pastes
    into a support thread."""
    secret = "http://127.0.0.1:9/hook?token=super-secret-value"
    preferences = Preferences(
        notify_target="webhook", notify_webhook_url=secret, notify_timeout_seconds=1
    )

    delivery = notify.send(preferences, notify.sample_payload())

    assert delivery.ok is False
    assert "super-secret-value" not in delivery.detail
    assert "token" not in delivery.detail


def test_ntfy_sends_a_title_and_a_readable_body(receiver) -> None:
    preferences = Preferences(
        notify_target="ntfy",
        notify_ntfy_server=_url(receiver, ""),
        notify_ntfy_topic="hivesync-alerts",
    )

    delivery = notify.send(preferences, notify.build_payload(_job(), _run()))

    assert delivery.ok is True
    request = receiver.received[0]
    assert request["path"] == "/hivesync-alerts"
    assert "Nightly Media" in request["headers"]["Title"]
    body = request["body"].decode("utf-8")
    assert "4 transferred" in body
    assert "The source is not mounted." in body


def test_a_unicode_job_name_does_not_break_the_ntfy_header(receiver) -> None:
    """Headers are latin-1. A job name is free text, so it has to survive being
    called something with an accent in it."""
    preferences = Preferences(
        notify_target="ntfy",
        notify_ntfy_server=_url(receiver, ""),
        notify_ntfy_topic="t",
    )
    payload = notify.build_payload(_job(name="Sauvegarde éphémère 文件"), _run())

    delivery = notify.send(preferences, payload)

    assert delivery.ok is True
    # The body carries the real name; only the header is degraded.
    assert "Sauvegarde éphémère 文件" in receiver.received[0]["body"].decode("utf-8")


def test_no_target_configured_is_a_skip_not_a_failure() -> None:
    delivery = notify.send(Preferences(), notify.sample_payload())
    assert delivery.skipped is True
    assert delivery.ok is False


def test_a_selected_target_with_nothing_filled_in_says_which_field() -> None:
    delivery = notify.send(Preferences(notify_target="webhook"), notify.sample_payload())
    assert delivery.attempted is False
    assert "no URL is set" in delivery.detail

    delivery = notify.send(Preferences(notify_target="ntfy"), notify.sample_payload())
    assert delivery.attempted is False
    assert "topic" in delivery.detail

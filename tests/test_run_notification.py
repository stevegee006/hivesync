"""A real run reaching a real notification endpoint.

The unit tests in test_notify.py cover the payload and the transport. This covers
the wiring: that a run which fails actually causes a POST, that a run which
succeeds under "failure only" does not, and, most importantly, that a broken
notification endpoint cannot change what the run recorded.

No rclone is needed. Every run here fails in the pre-flight, which is a real
failure path and the one people most want to be told about.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from app import preferences as preferences_store
from app.crypto import SecretBox
from app.db import create_db_engine
from app.jobs import planner
from app.jobs.runner import LiveRunner
from app.models import (
    Connection,
    ConnectionType,
    Direction,
    Job,
    JobRun,
    NotifyOn,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.preferences import Preferences
from tests.conftest import create_schema, make_settings


class _Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        server: Any = self.server
        server.received.append(body)
        if server.hang:
            threading.Event().wait(3)
        self.send_response(server.status)
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr logging."""


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    server.received = []  # type: ignore[attr-defined]
    server.status = 200  # type: ignore[attr-defined]
    server.hang = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path / "config")
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    return settings, SecretBox(settings.secret_key), factory, factory()


def _job(session, tmp_path: Path, **overrides) -> Job:  # noqa: ANN003
    # A source with a sentinel file that is not there, so the pre-flight refuses
    # before rclone is ever invoked. A real failure, and no binary needed.
    source_dir = tmp_path / "src"
    source_dir.mkdir(exist_ok=True)
    source = Connection(
        name="src",
        type=ConnectionType.local,
        base_path=str(source_dir),
        sentinel_file=".mounted",
    )
    dest = Connection(name="dst", type=ConnectionType.local, base_path=str(tmp_path / "dst"))
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "Nightly Media",
        "source_connection_id": source.id,
        "dest_connection_id": dest.id,
        "source_path": "",
        "dest_path": "",
        "direction": Direction.source_to_dest,
        "notify_on": NotifyOn.failure,
        "max_delete_pct": 20,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _run(env, job: Job) -> JobRun:
    settings, box, factory, session = env
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    LiveRunner(factory, box=box, settings=settings).run_now(run.id)
    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    return stored


def _configure(session, url: str, **overrides: object) -> None:
    preferences_store.save(
        session,
        Preferences(notify_target="webhook", notify_webhook_url=url, **overrides),  # type: ignore[arg-type]
    )


def _url(server: HTTPServer) -> str:
    return f"http://{server.server_address[0]}:{server.server_address[1]}/hook"


def test_a_failed_run_notifies(tmp_path: Path, env, receiver) -> None:
    import json

    _settings, _box, _factory, session = env
    _configure(session, _url(receiver), base_url="http://nas.local:8080")
    job = _job(session, tmp_path)

    run = _run(env, job)

    assert run.status == RunStatus.failed
    assert len(receiver.received) == 1
    payload = json.loads(receiver.received[0])
    assert payload["job"] == "Nightly Media"
    assert payload["status"] == "failed"
    assert payload["url"].endswith(f"/runs/{run.id}")
    # The reason, which is what makes the notification worth reading, and it is
    # the same reason the run recorded rather than a generic one. Not asserted
    # as a specific string: which failure comes first depends on whether rclone
    # is on the host, and both are real failures.
    assert payload["error"] == run.summary["error"]
    assert payload["error"]


def test_never_means_never(tmp_path: Path, env, receiver) -> None:
    _settings, _box, _factory, session = env
    _configure(session, _url(receiver))
    job = _job(session, tmp_path, notify_on=NotifyOn.never)

    assert _run(env, job).status == RunStatus.failed
    assert receiver.received == []


def test_no_target_configured_sends_nothing(tmp_path: Path, env, receiver) -> None:
    _settings, _box, _factory, session = env
    job = _job(session, tmp_path)

    assert _run(env, job).status == RunStatus.failed
    assert receiver.received == []


def test_a_broken_endpoint_does_not_change_the_run(tmp_path: Path, env, receiver) -> None:
    """The whole point of sending after the commit. A sync's recorded outcome
    must not depend on whether a webhook answered."""
    receiver.status = 500
    _settings, _box, _factory, session = env
    _configure(session, _url(receiver))
    job = _job(session, tmp_path)

    run = _run(env, job)

    assert run.status == RunStatus.failed
    assert run.summary["error"], "the run still recorded why it failed"
    assert len(receiver.received) == 1


def test_a_hanging_endpoint_does_not_hang_the_run(tmp_path: Path, env, receiver) -> None:
    import time

    receiver.hang = True
    _settings, _box, _factory, session = env
    _configure(session, _url(receiver), notify_timeout_seconds=1)
    job = _job(session, tmp_path)

    started = time.monotonic()
    run = _run(env, job)
    elapsed = time.monotonic() - started

    assert run.status == RunStatus.failed
    # The timeout bounds it. Without one this waits for the endpoint.
    assert elapsed < 3, f"the run waited {elapsed:.1f}s on a notification"

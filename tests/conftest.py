"""Shared fixtures.

Each test gets its own SQLite file under tmp_path, so nothing is shared between
tests and nothing touches a real /config.

Tables are created before create_app, because create_app runs the startup checks
and those expect the schema to exist. In the container that ordering is provided
by the entrypoint running `alembic upgrade head` first.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.binaries import BinaryInfo, BinaryReport
from app.config import Settings
from app.db import create_db_engine
from app.main import create_app
from app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]

# A real Fernet key, generated once per test session.
TEST_SECRET_KEY = Fernet.generate_key().decode("ascii")
TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "bootstrap-password-1"
NEW_PASSWORD = "a-longer-replacement-password"


def make_settings(config_dir: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "secret_key": TEST_SECRET_KEY,
        "config_dir": config_dir,
        "admin_user": TEST_ADMIN_USERNAME,
        "admin_password": TEST_ADMIN_PASSWORD,
        "auth_mode": "local",
        "log_level": "warning",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def create_schema(settings: Settings) -> None:
    settings.ensure_directories()
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app_settings_path(tmp_path: Path) -> Path:
    """The config directory the app fixture is using, for on-disk assertions.

    tmp_path is function scoped, so this is the same directory the app writes to
    within a single test.
    """
    return tmp_path


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    create_schema(settings)
    return create_app(settings)


_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def refresh_csrf(client: TestClient) -> str:
    """Pick up the session's CSRF token and send it on every later request.

    A browser gets the token by rendering a page, so a test client does the same
    rather than reaching into the session. Called again after login, because the
    token is rotated there: one minted before authentication must not stay valid
    afterwards.
    """
    for path in ("/", "/login"):
        match = _TOKEN.search(client.get(path).text)
        if match:
            client.headers["X-CSRF-Token"] = match.group(1)
            return match.group(1)
    raise AssertionError("no CSRF token was rendered on any page")


@pytest.fixture
def fake_binaries() -> BinaryReport:
    """A healthy binary report.

    The host running unit tests has no rclone, and should not need it:
    binary discovery is exercised directly in test_binaries.py, and everything
    else injects this.
    """
    return BinaryReport(
        rclone=BinaryInfo(name="rclone", ok=True, version="1.74.4", path="/usr/local/bin/rclone"),
        expected_rclone_version="1.74.4",
    )


@pytest.fixture
def client(app: FastAPI, fake_binaries: BinaryReport) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        # Set after lifespan, which probes the real binaries and would otherwise
        # overwrite this.
        app.state.binaries = fake_binaries
        refresh_csrf(test_client)
        yield test_client


@pytest.fixture
def authed_client(client: TestClient) -> TestClient:
    """A client signed in as the admin, past the forced password change."""
    response = client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Login rotates the token, so the stale one would be refused from here on.
    refresh_csrf(client)
    response = client.post(
        "/api/auth/change-password",
        data={"current_password": TEST_ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client

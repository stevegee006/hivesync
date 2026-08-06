"""Login rate limiting and proxy-asserted identity. SPEC section 15.

Acceptance criteria:

- Repeated failed logins lock out, survive a restart, and a correct password
  during the lockout is still refused. The message is identical for a wrong
  password, an unknown user and a locked account.
- `trusted_header` honours the header from an allowlisted peer, ignores it from
  anywhere else, and never creates a user.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from app import ratelimit, security
from app.config import Settings
from app.db import create_db_engine
from app.main import create_app
from app.models import AttemptScope, LoginAttempt, User, utcnow
from tests.conftest import (
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    create_schema,
    make_settings,
    refresh_csrf,
)

WRONG = "definitely-not-the-password"


def _session(settings: Settings) -> Session:
    return sessionmaker(bind=create_db_engine(settings))()


def _attempt(client: TestClient, password: str, username: str = TEST_ADMIN_USERNAME) -> Response:
    return client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_repeated_failures_lock_the_account(client: TestClient, settings: Settings) -> None:
    for _ in range(settings.login_max_attempts):
        assert _attempt(client, WRONG).headers["location"] == "/login?error=invalid"

    # The correct password is now refused too. That is the point: an attacker who
    # reaches the right password on attempt six must not get in.
    locked = _attempt(client, TEST_ADMIN_PASSWORD)
    assert locked.headers["location"] == "/login?error=invalid"


def test_a_locked_account_is_indistinguishable_from_a_wrong_password(
    client: TestClient, settings: Settings
) -> None:
    """A limiter that announces the lockout confirms the account exists, which is
    exactly what the equal-time password comparison avoids elsewhere."""
    wrong = _attempt(client, WRONG)
    for _ in range(settings.login_max_attempts):
        _attempt(client, WRONG)
    locked = _attempt(client, TEST_ADMIN_PASSWORD)
    unknown = _attempt(client, WRONG, username="nobody-by-that-name")

    assert wrong.status_code == locked.status_code == unknown.status_code
    assert wrong.headers["location"] == locked.headers["location"] == unknown.headers["location"]


def test_the_json_api_gives_the_same_answer(client: TestClient, settings: Settings) -> None:
    for _ in range(settings.login_max_attempts):
        client.post("/api/auth/login", json={"username": TEST_ADMIN_USERNAME, "password": WRONG})

    locked = client.post(
        "/api/auth/login", json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD}
    )
    assert locked.status_code == 401
    assert "not valid" in locked.json()["detail"]
    assert "locked" not in locked.text.lower()
    assert "attempts" not in locked.text.lower()


def test_the_lockout_survives_a_restart(tmp_path: Path) -> None:
    """In memory it would not, and restarting a container is not hard to cause."""
    settings = make_settings(tmp_path)
    create_schema(settings)

    with TestClient(create_app(settings)) as client:
        refresh_csrf(client)
        for _ in range(settings.login_max_attempts):
            _attempt(client, WRONG)

    # A completely new application over the same database.
    with TestClient(create_app(settings)) as restarted:
        refresh_csrf(restarted)
        locked = _attempt(restarted, TEST_ADMIN_PASSWORD)
        assert locked.headers["location"] == "/login?error=invalid"


def test_a_successful_login_clears_the_counter(client: TestClient, settings: Settings) -> None:
    """Otherwise a few typos this morning lock someone out this afternoon."""
    for _ in range(settings.login_max_attempts - 1):
        _attempt(client, WRONG)

    assert _attempt(client, TEST_ADMIN_PASSWORD).status_code == 303
    refresh_csrf(client)

    stored = _session(settings)
    assert stored.query(LoginAttempt).count() == 0


def test_the_lock_lifts_when_the_window_passes(client: TestClient, settings: Settings) -> None:
    """The window slides from the oldest failure in it, not from the newest, so
    someone still retrying does not extend their own lockout forever."""
    for _ in range(settings.login_max_attempts):
        _attempt(client, WRONG)
    assert _attempt(client, TEST_ADMIN_PASSWORD).headers["location"] == "/login?error=invalid"

    # Age every recorded failure past the window.
    stored = _session(settings)
    old = utcnow() - timedelta(seconds=settings.login_lockout_seconds + 60)
    for row in stored.query(LoginAttempt).all():
        row.attempted_at = old
    stored.commit()

    # Signed in. The bootstrap admin is sent to change its password, which is
    # not the login wall.
    assert _attempt(client, TEST_ADMIN_PASSWORD).headers["location"] == "/account/password"


def test_both_scopes_are_counted(client: TestClient, settings: Settings) -> None:
    """Per username so one address cannot walk a password list across accounts,
    and per address so a botnet cannot walk one account."""
    for index in range(settings.login_max_attempts):
        _attempt(client, WRONG, username=f"someone-{index}")

    stored = _session(settings)
    by_scope: dict[AttemptScope, int] = dict.fromkeys(AttemptScope, 0)
    for row in stored.query(LoginAttempt).all():
        by_scope[row.scope] += 1
    assert by_scope[AttemptScope.username] == settings.login_max_attempts
    assert by_scope[AttemptScope.address] == settings.login_max_attempts

    # The address has now hit its limit even though no single username did.
    assert _attempt(client, TEST_ADMIN_PASSWORD).headers["location"] == "/login?error=invalid"


def test_usernames_are_counted_case_insensitively(settings: Settings, tmp_path: Path) -> None:
    create_schema(settings)
    session = _session(settings)
    ratelimit.record_failure(session, username="Admin", address="10.0.0.1")
    ratelimit.record_failure(session, username="ADMIN", address="10.0.0.1")

    verdict = ratelimit.check(
        session,
        make_settings(tmp_path, login_max_attempts=2),
        username="admin",
        address="10.0.0.2",
    )
    assert verdict.allowed is False
    assert verdict.scope == AttemptScope.username


# --------------------------------------------------------------------------
# trusted_header
# --------------------------------------------------------------------------


def _trusted_app(tmp_path: Path, **overrides: object) -> tuple[Settings, FastAPI]:
    fields: dict[str, object] = {
        "auth_mode": "trusted_header",
        "trusted_header": "X-Authentik-Username",
        "trusted_proxies": "10.0.0.0/8, 192.168.1.5/32",
    }
    fields.update(overrides)
    settings = make_settings(tmp_path, **fields)
    create_schema(settings)
    return settings, create_app(settings)


def test_an_allowlisted_proxy_can_assert_an_existing_user(tmp_path: Path) -> None:
    _settings, app = _trusted_app(tmp_path)
    with TestClient(app, client=("10.1.2.3", 12345)) as client:
        response = client.get(
            "/",
            headers={"X-Authentik-Username": TEST_ADMIN_USERNAME},
            follow_redirects=False,
        )
        # Authenticated: the bootstrap admin is sent to change its password
        # rather than to the login wall.
        assert response.status_code == 303
        assert response.headers["location"] == "/account/password"


def test_an_address_outside_the_allowlist_is_ignored(tmp_path: Path) -> None:
    """Without this the header is just a username field anyone can fill in."""
    _settings, app = _trusted_app(tmp_path)
    with TestClient(app, client=("203.0.113.9", 12345)) as client:
        response = client.get(
            "/",
            headers={"X-Authentik-Username": TEST_ADMIN_USERNAME},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


def test_a_forwarded_for_header_cannot_fake_the_peer(tmp_path: Path) -> None:
    """X-Forwarded-For is set by the client on a direct connection, so trusting it
    would let anyone claim to have arrived through the proxy."""
    _settings, app = _trusted_app(tmp_path)
    with TestClient(app, client=("203.0.113.9", 12345)) as client:
        response = client.get(
            "/",
            headers={
                "X-Authentik-Username": TEST_ADMIN_USERNAME,
                "X-Forwarded-For": "10.0.0.1",
                "X-Real-IP": "10.0.0.1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


def test_an_unknown_username_is_never_created(tmp_path: Path) -> None:
    """A header that provisions an account is a registration form with no
    password on it."""
    settings, app = _trusted_app(tmp_path)
    before = _session(settings).query(User).count()

    with TestClient(app, client=("10.1.2.3", 12345)) as client:
        response = client.get(
            "/", headers={"X-Authentik-Username": "someone-new"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    assert _session(settings).query(User).count() == before


def test_no_header_at_all_is_not_a_login(tmp_path: Path) -> None:
    _settings, app = _trusted_app(tmp_path)
    with TestClient(app, client=("10.1.2.3", 12345)) as client:
        assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    ("peer", "allowed"),
    [
        ("10.0.0.1", True),
        ("10.255.255.254", True),
        ("192.168.1.5", True),
        ("192.168.1.6", False),
        ("172.16.0.1", False),
        ("not-an-address", False),
    ],
)
def test_the_allowlist_is_matched_by_network(tmp_path: Path, peer: str, allowed: bool) -> None:
    settings, _app = _trusted_app(tmp_path)

    class _Request:
        def __init__(self, host: str) -> None:
            self.client = type("Peer", (), {"host": host})()

    assert security.peer_is_trusted_proxy(_Request(peer), settings) is allowed


def test_an_unparseable_allowlist_entry_does_not_open_the_door(tmp_path: Path) -> None:
    settings, _app = _trusted_app(tmp_path, trusted_proxies="not-a-cidr, 10.0.0.0/8")

    class _Request:
        def __init__(self, host: str) -> None:
            self.client = type("Peer", (), {"host": host})()

    assert security.peer_is_trusted_proxy(_Request("10.0.0.1"), settings) is True
    assert security.peer_is_trusted_proxy(_Request("203.0.113.1"), settings) is False


# --------------------------------------------------------------------------
# The API token
# --------------------------------------------------------------------------


def test_the_api_token_authenticates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_token="a-long-script-token")
    create_schema(settings)
    with TestClient(create_app(settings)) as client:
        ok = client.get("/api/jobs", headers={"Authorization": "Bearer a-long-script-token"})
        assert ok.status_code == 200

        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_no_api_token_configured_means_no_bypass(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    create_schema(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/jobs", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 401

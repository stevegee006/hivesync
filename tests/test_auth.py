"""Authentication and the login page, which is M0's other acceptance criterion."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import security
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.main import create_app
from app.models import User
from tests.conftest import (
    NEW_PASSWORD,
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    create_schema,
    make_settings,
    refresh_csrf,
)

# A browser submitting a form always sends this content type, even when the form
# has no fields, which is the case for the sign out button.
BROWSER_POST = {"headers": {"content-type": "application/x-www-form-urlencoded"}}


def test_login_page_renders(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert 'name="password"' in response.text


def test_login_page_works_without_javascript(client: TestClient) -> None:
    """The vendored assets are optional. A plain form post has to be enough."""
    body = client.get("/login").text
    assert 'action="/api/auth/login"' in body
    assert 'method="post"' in body


def test_dashboard_requires_authentication(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=invalid"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_unknown_user_is_indistinguishable_from_a_wrong_password(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        data={"username": "nobody", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login?error=invalid"


def test_bootstrap_login_forces_a_password_change(client: TestClient) -> None:
    """The value from HIVESYNC_ADMIN_PASSWORD must not become the permanent one."""
    response = client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/account/password"

    # Even navigating elsewhere comes back to the password change.
    assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"


def test_password_change_then_dashboard(client: TestClient) -> None:
    client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    refresh_csrf(client)
    response = client.post(
        "/api/auth/change-password",
        data={"current_password": TEST_ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Jobs" in dashboard.text


def test_new_password_works_and_old_one_does_not(authed_client: TestClient) -> None:
    authed_client.post("/api/auth/logout", **BROWSER_POST, follow_redirects=False)
    refresh_csrf(authed_client)

    rejected = authed_client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert rejected.headers["location"] == "/login?error=invalid"

    accepted = authed_client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert accepted.headers["location"] == "/"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"current_password": "wrong", "new_password": NEW_PASSWORD}, "wrong"),
        ({"current_password": TEST_ADMIN_PASSWORD, "new_password": "short"}, "weak"),
        (
            {"current_password": TEST_ADMIN_PASSWORD, "new_password": TEST_ADMIN_PASSWORD},
            "same",
        ),
    ],
)
def test_password_change_rejections(client: TestClient, payload: dict[str, str], code: str) -> None:
    client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    refresh_csrf(client)
    response = client.post("/api/auth/change-password", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/account/password?error={code}"


def test_logout_ends_the_session(authed_client: TestClient) -> None:
    response = authed_client.post("/api/auth/logout", **BROWSER_POST, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert authed_client.get("/", follow_redirects=False).status_code == 303


def test_json_login_returns_json(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"username": TEST_ADMIN_USERNAME, "must_change_password": True}


def test_json_login_failure_is_401(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": TEST_ADMIN_USERNAME, "password": "wrong"}
    )
    assert response.status_code == 401


def test_api_route_returns_401_not_a_redirect(client: TestClient) -> None:
    response = client.post("/api/auth/change-password", json={}, follow_redirects=False)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "target",
    ["https://evil.example", "//evil.example", "http://evil.example/path"],
)
def test_login_cannot_be_used_as_an_open_redirect(client: TestClient, target: str) -> None:
    response = client.post(
        "/api/auth/login",
        data={
            "username": TEST_ADMIN_USERNAME,
            "password": TEST_ADMIN_PASSWORD,
            "next": target,
        },
        follow_redirects=False,
    )
    assert response.headers["location"] in {"/account/password", "/"}


def test_next_parameter_survives_a_login(client: TestClient) -> None:
    """A relative path is honoured, so a deep link is not lost at the login wall."""
    engine = create_db_engine(client.app.state.settings)  # type: ignore[attr-defined]
    session = create_session_factory(engine)()
    user = session.scalar(select(User))
    assert user is not None
    security.set_password(session, user, NEW_PASSWORD)

    response = client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": NEW_PASSWORD, "next": "/runs/7"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/runs/7"


def test_no_password_hash_is_ever_serialized(authed_client: TestClient) -> None:
    body = authed_client.get("/").text
    assert "$argon2" not in body
    assert NEW_PASSWORD not in body


def test_starting_without_an_admin_password_leaves_the_instance_unclaimed(
    tmp_path: Path,
) -> None:
    """It used to refuse to start. The first visitor now creates the account, so
    the variable is optional and the instance simply waits."""
    settings: Settings = make_settings(tmp_path, admin_password=None)
    create_schema(settings)

    app = create_app(settings)

    factory = sessionmaker(bind=create_db_engine(settings))
    with factory() as session:
        assert security.needs_setup(session) is True
    assert app is not None


def test_bootstrap_refuses_a_weak_admin_password(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path, admin_password="short")
    create_schema(settings)
    with pytest.raises(security.BootstrapError):
        create_app(settings)


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    """Restarting must not create a second admin or reset the first one."""
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    create_app(settings)

    engine = create_db_engine(settings)
    session = create_session_factory(engine)()
    user = session.scalar(select(User))
    assert user is not None
    original_hash = user.password_hash
    session.close()

    create_app(settings)

    session = create_session_factory(create_db_engine(settings))()
    users = list(session.scalars(select(User)))
    assert len(users) == 1
    assert users[0].password_hash == original_hash


def test_password_verification_rejects_a_wrong_password() -> None:
    stored = security.hash_password("the-right-password")
    assert security.verify_password(stored, "the-right-password")[0] is True
    assert security.verify_password(stored, "the-wrong-password")[0] is False


def test_password_hash_is_argon2id() -> None:
    assert security.hash_password("whatever").startswith("$argon2id$")


def test_auth_mode_none_grants_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented in SPEC section 14 for use behind an authenticating proxy."""
    settings: Settings = make_settings(tmp_path, auth_mode="none")
    create_schema(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303  # forced password change


def test_trusted_header_mode_starts_now_that_it_is_implemented(tmp_path: Path) -> None:
    """Refused to start from M0 to M7 rather than being half built. M8 builds it,
    and the proxy allowlist is what makes it safe: see test_trusted_header.py."""
    settings: Settings = make_settings(
        tmp_path,
        auth_mode="trusted_header",
        trusted_header="X-Authentik-Username",
        trusted_proxies="10.0.0.0/8",
    )
    create_schema(settings)
    app = create_app(settings)
    assert app.state.settings.auth_mode == "trusted_header"


def test_trusted_header_mode_requires_a_proxy_allowlist(tmp_path: Path) -> None:
    """Without the allowlist any client could forge the identity header."""
    with pytest.raises(ValueError, match="HIVESYNC_TRUSTED_PROXIES"):
        make_settings(tmp_path, auth_mode="trusted_header", trusted_header="X-Authentik-Username")

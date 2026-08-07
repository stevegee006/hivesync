"""The first-run setup wizard.

Replaces the old arrangement, where `HIVESYNC_ADMIN_PASSWORD` was mandatory and
the container refused to start without it. The variable still works and is the
right choice for an instance that will be exposed before anyone has claimed it;
these tests cover the case where it is absent and a person creates the account
from the browser instead.

The security property being tested is narrow and worth stating: the wizard is
open to anyone who can reach it, but **only while no account exists**. The
moment one does, every route into it closes permanently.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import security
from app.config import Settings
from app.db import create_db_engine
from app.main import create_app
from app.models import User
from tests.conftest import create_schema, make_settings


def _unclaimed(tmp_path: Path) -> tuple[Settings, TestClient]:
    """An instance with a schema and no accounts."""
    settings = make_settings(tmp_path, admin_password=None)
    create_schema(settings)
    app = create_app(settings)
    return settings, TestClient(app)


def _users(settings: Settings) -> list[User]:
    with sessionmaker(bind=create_db_engine(settings))() as session:
        return list(session.scalars(select(User)))


def _token(client: TestClient) -> str:
    page = client.get("/setup").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_an_unclaimed_instance_offers_the_wizard(tmp_path: Path) -> None:
    _settings, client = _unclaimed(tmp_path)
    with client:
        response = client.get("/setup")

    assert response.status_code == 200
    assert "Create your account" in response.text


def test_every_page_leads_to_the_wizard_while_unclaimed(tmp_path: Path) -> None:
    """Including the login page, which would otherwise be a dead end: no account
    exists and none can be created from there."""
    _settings, client = _unclaimed(tmp_path)
    with client:
        for path in ("/", "/jobs", "/connections", "/settings", "/login"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/setup", path


def test_creating_the_account_signs_that_person_in(tmp_path: Path) -> None:
    settings, client = _unclaimed(tmp_path)
    with client:
        response = client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "steve",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        # Straight in, no second sign-in step.
        assert client.get("/").status_code == 200

    users = _users(settings)
    assert [u.username for u in users] == ["steve"]
    assert users[0].role.value == "admin"


def test_the_created_account_is_not_forced_to_change_its_password(tmp_path: Path) -> None:
    """A bootstrap password out of a compose file has to be changed because it
    has been sitting in a file. One chosen in the browser a second ago has not."""
    settings, client = _unclaimed(tmp_path)
    with client:
        client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "steve",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )

    assert _users(settings)[0].must_change_password is False


def test_the_password_is_stored_hashed(tmp_path: Path) -> None:
    settings, client = _unclaimed(tmp_path)
    secret = "a-long-enough-password"
    with client:
        client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "steve",
                "password": secret,
                "confirm": secret,
            },
        )

    stored = _users(settings)[0].password_hash
    assert secret not in stored
    assert stored.startswith("$argon2")


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_the_wizard_closes_once_an_account_exists(tmp_path: Path) -> None:
    """The whole security property. Once claimed, there is no way back in."""
    _settings, client = _unclaimed(tmp_path)
    with client:
        client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "first",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )
        client.cookies.clear()

        page = client.get("/setup", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login"


def test_posting_the_wizard_again_creates_nothing(tmp_path: Path) -> None:
    """With a valid session and a fresh token, so this reaches the claimed check
    rather than being stopped by CSRF: that is a different test, below."""
    settings, client = _unclaimed(tmp_path)
    with client:
        client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "first",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )

        # Signed in now, so the token is valid and the refusal is the wizard's.
        page = client.get("/settings").text
        marker = 'name="csrf_token" value="'
        start = page.index(marker) + len(marker)
        token = page[start : page.index('"', start)]

        response = client.post(
            "/setup",
            data={
                "csrf_token": token,
                "username": "second",
                "password": "another-long-password",
                "confirm": "another-long-password",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert [u.username for u in _users(settings)] == ["first"]


def test_a_stranger_with_a_stale_form_is_refused(tmp_path: Path) -> None:
    """Someone who loaded the wizard before it was claimed, kept the tab open,
    and submitted afterwards. They have no session, so CSRF stops them first."""
    settings, client = _unclaimed(tmp_path)
    with client:
        stale = _token(client)
        client.post(
            "/setup",
            data={
                "csrf_token": stale,
                "username": "first",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )
        client.cookies.clear()
        response = client.post(
            "/setup",
            data={
                "csrf_token": stale,
                "username": "attacker",
                "password": "another-long-password",
                "confirm": "another-long-password",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert [u.username for u in _users(settings)] == ["first"]


def test_a_short_password_is_refused(tmp_path: Path) -> None:
    settings, client = _unclaimed(tmp_path)
    with client:
        response = client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "steve",
                "password": "short",
                "confirm": "short",
            },
        )

    assert response.status_code == 400
    assert _users(settings) == []


def test_mismatched_passwords_are_refused(tmp_path: Path) -> None:
    settings, client = _unclaimed(tmp_path)
    with client:
        response = client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "steve",
                "password": "a-long-enough-password",
                "confirm": "a-different-password",
            },
        )

    assert response.status_code == 400
    assert "do not match" in response.text
    assert _users(settings) == []


def test_an_empty_username_is_refused(tmp_path: Path) -> None:
    settings, client = _unclaimed(tmp_path)
    with client:
        response = client.post(
            "/setup",
            data={
                "csrf_token": _token(client),
                "username": "   ",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )

    assert response.status_code == 400
    assert _users(settings) == []


def test_the_wizard_is_not_exempt_from_csrf(tmp_path: Path) -> None:
    """Without a token here, a page on another site could create the account on
    a freshly started instance and the operator would never know."""
    settings, client = _unclaimed(tmp_path)
    with client:
        client.get("/setup")
        response = client.post(
            "/setup",
            data={
                "username": "attacker",
                "password": "a-long-enough-password",
                "confirm": "a-long-enough-password",
            },
        )

    assert response.status_code == 403
    assert _users(settings) == []


# --------------------------------------------------------------------------
# The pre-provisioned path still works
# --------------------------------------------------------------------------


def test_an_admin_password_still_pre_provisions_and_skips_the_wizard(tmp_path: Path) -> None:
    """The way to have no unclaimed window at all, which is what anyone exposing
    the instance publicly should use."""
    settings = make_settings(tmp_path, admin_password="a-long-enough-password")
    create_schema(settings)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/setup", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    users = _users(settings)
    assert len(users) == 1
    # Out of a compose file, so it does have to be changed.
    assert users[0].must_change_password is True


def test_create_first_admin_refuses_a_claimed_instance(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, admin_password="a-long-enough-password")
    create_schema(settings)
    create_app(settings)

    with sessionmaker(bind=create_db_engine(settings))() as session:
        try:
            security.create_first_admin(session, "second", "another-long-password")
        except security.BootstrapError as exc:
            assert "already has an account" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a second admin was created")

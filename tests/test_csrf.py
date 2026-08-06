"""CSRF protection. SPEC section 15.

Acceptance criterion: a form POST without a valid token is refused, with a valid
one succeeds, and a token from another session is refused, across web forms,
HTMX partials and the JSON API.

The last test in this file is the one that matters most over time. It walks every
page, finds every POST form, and fails if any of them lacks a token field. A
protection applied by hand to twenty-three forms is a protection that is missing
from the twenty-fourth.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import csrf
from app.config import Settings
from tests.conftest import (
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    create_schema,
    make_settings,
    refresh_csrf,
)

FORM_TAG = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
TOKEN_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


@contextmanager
def no_token(client: TestClient):
    """Drop the token header for the duration of the block.

    httpx *merges* per-request headers with the client's, so passing a filtered
    dict does not remove anything. The header has to come off the client.
    """
    saved = client.headers.pop("X-CSRF-Token", None)
    try:
        yield client
    finally:
        if saved is not None:
            client.headers["X-CSRF-Token"] = saved


# --------------------------------------------------------------------------
# The criterion
# --------------------------------------------------------------------------


def test_a_post_without_a_token_is_refused(authed_client: TestClient) -> None:
    with no_token(authed_client):
        response = authed_client.post("/jobs", data={"name": "x"})
    assert response.status_code == 403
    assert "security token" in response.text


def test_a_post_with_the_token_succeeds(authed_client: TestClient, settings: Settings) -> None:
    """The same request, with the field a browser would have submitted."""
    token = refresh_csrf(authed_client)
    with no_token(authed_client):
        response = authed_client.post(
            "/filter-presets",
            data={"name": "With a token", "exclude": "*.tmp", "include": "", "csrf_token": token},
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_a_token_from_another_session_is_refused(authed_client: TestClient, tmp_path) -> None:
    """The token is bound to the session that issued it, which is what makes this
    a synchroniser token rather than a value anyone can mint."""
    from app.main import create_app

    # A second application rather than a second client on the same one: entering
    # the lifespan twice would start the scheduler twice.
    other_settings = make_settings(tmp_path / "other-instance")
    create_schema(other_settings)
    with TestClient(create_app(other_settings)) as other:
        stolen = refresh_csrf(other)

    with no_token(authed_client):
        response = authed_client.post(
            "/filter-presets",
            data={"name": "Nope", "exclude": "", "include": "", "csrf_token": stolen},
        )
    assert response.status_code == 403


def test_the_api_refuses_a_json_post_without_a_token(authed_client: TestClient) -> None:
    with no_token(authed_client):
        response = authed_client.post(
            "/api/filter-presets", json={"name": "x", "rules": {"exclude": []}}
        )
    assert response.status_code == 403
    # JSON in, JSON out: a caller parsing the response must not get HTML.
    assert response.json()["detail"]


def test_the_api_accepts_the_token_in_a_header(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/filter-presets", json={"name": "Header token", "rules": {"exclude": ["*.tmp"]}}
    )
    assert response.status_code == 201


def test_an_htmx_partial_post_carries_the_token(authed_client: TestClient) -> None:
    """htmx sends no form body for a bare hx-post, so the token has to travel as
    a header. base.html sets hx-headers for the whole page."""
    # The page hands htmx the token for every request it makes.
    assert "hx-headers=" in authed_client.get("/").text

    with no_token(authed_client):
        response = authed_client.post("/connections/999/test-partial")
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Login, which is not exempt
# --------------------------------------------------------------------------


def test_login_requires_a_token(client: TestClient) -> None:
    """Login CSRF logs a victim into an attacker's account and then collects
    whatever they do in it."""
    with no_token(client):
        response = client.post(
            "/api/auth/login",
            data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        )
    assert response.status_code == 403


def test_the_login_page_mints_a_token_for_an_anonymous_visitor(client: TestClient) -> None:
    body = client.get("/login").text
    assert TOKEN_INPUT.search(body), "the login form must carry a token before there is a user"


def test_the_token_is_rotated_on_login(client: TestClient) -> None:
    """A token fixed by an attacker before sign in must not survive it."""
    before = refresh_csrf(client)
    client.post(
        "/api/auth/login",
        data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    after = refresh_csrf(client)
    assert before != after

    # And the old one no longer works.
    with no_token(client):
        response = client.post(
            "/api/auth/change-password",
            data={
                "current_password": TEST_ADMIN_PASSWORD,
                "new_password": "another-long-password",
                "csrf_token": before,
            },
        )
    assert response.status_code == 403


def test_the_token_is_rotated_on_logout(authed_client: TestClient) -> None:
    before = refresh_csrf(authed_client)
    authed_client.post("/api/auth/logout", follow_redirects=False)
    assert refresh_csrf(authed_client) != before


# --------------------------------------------------------------------------
# Requests that are exempt, and why
# --------------------------------------------------------------------------


def test_safe_methods_are_not_blocked(authed_client: TestClient) -> None:
    with no_token(authed_client):
        assert authed_client.get("/jobs").status_code == 200


def test_the_api_token_skips_the_check(tmp_path) -> None:
    """A bearer token is never attached by a browser, so there is nothing to
    forge and requiring a form token would only make the API unusable."""
    from app.main import create_app

    settings = make_settings(tmp_path, api_token="script-token-value")
    create_schema(settings)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/filter-presets",
            json={"name": "From a script", "rules": {"exclude": ["*.tmp"]}},
            headers={"Authorization": "Bearer script-token-value"},
        )
        assert response.status_code == 201

        # A wrong token is not a free pass: it fails CSRF like anyone else.
        refused = client.post(
            "/api/filter-presets",
            json={"name": "Nope", "rules": {}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert refused.status_code == 403


def test_an_oversized_form_is_refused_before_it_is_buffered(authed_client: TestClient) -> None:
    """The body is read before authentication, so it has to be bounded."""
    payload = "x" * (csrf.MAX_BUFFERED_BODY + 1024)
    with no_token(authed_client):
        response = authed_client.post(
            "/settings", data={"notify_target": "none", "padding": payload}
        )
    assert response.status_code == 413


# --------------------------------------------------------------------------
# The guard that keeps this true
# --------------------------------------------------------------------------

PAGES = (
    "/",
    "/login",
    "/account/password",
    "/connections",
    "/connections/new",
    "/credentials",
    "/jobs",
    "/jobs/new",
    "/filter-presets",
    "/settings",
    "/compatibility",
)


@pytest.mark.parametrize("path", PAGES)
def test_every_post_form_on_every_page_carries_a_token(
    authed_client: TestClient, path: str
) -> None:
    """Twenty-three forms were tokenised by hand. This is what catches the
    twenty-fourth, and the one someone adds next year."""
    body = authed_client.get(path).text
    for tag in FORM_TAG.finditer(body):
        if 'method="post"' not in tag.group(0).lower():
            continue
        following = body[tag.end() : tag.end() + 400]
        assert 'name="csrf_token"' in following, (
            f"a POST form on {path} has no CSRF token: {tag.group(0)[:120]}"
        )


def test_the_multipart_import_form_still_receives_its_file(authed_client: TestClient) -> None:
    """The middleware buffers and replays a multipart body to find the token. If
    the replay were wrong the route would see an empty upload, which would look
    like a bad file rather than a bug here."""
    import json

    token = refresh_csrf(authed_client)
    document = {
        "format_version": 1,
        "connections": [{"name": "replayed", "type": "local", "base_path": "/srv/replayed"}],
        "jobs": [],
        "filter_presets": [],
        "preferences": {},
    }
    with no_token(authed_client):
        response = authed_client.post(
            "/settings/import",
            data={"csrf_token": token},
            files={"document": ("config.json", json.dumps(document).encode(), "application/json")},
        )
    assert response.status_code == 200
    assert "Imported 1 connections" in response.text

"""Response headers, and the session cookie flags. SPEC section 15.

Framing is the one that matters here rather than being hygiene: this UI has a Run
button that deletes files, and a framed page clicked through is a real click, so
no CSRF token protects against it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.headers import CONTENT_SECURITY_POLICY
from app.web import TEMPLATE_DIR


def test_every_response_refuses_framing(client: TestClient) -> None:
    response = client.get("/login")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_content_type_sniffing_is_disabled(client: TestClient) -> None:
    """An uploaded configuration file must not be guessed into HTML."""
    assert client.get("/login").headers["x-content-type-options"] == "nosniff"


def test_paths_do_not_leak_through_the_referer(client: TestClient) -> None:
    assert client.get("/login").headers["referrer-policy"] == "same-origin"


def test_the_policy_allows_only_this_origin(client: TestClient) -> None:
    """Everything is vendored rather than loaded from a CDN, so there is no
    external origin that needs allowing."""
    policy = client.get("/login").headers["content-security-policy"]
    assert policy == CONTENT_SECURITY_POLICY
    assert "http://" not in policy
    assert "https://" not in policy
    assert "object-src 'none'" in policy
    assert "form-action 'self'" in policy


def test_the_policy_permits_what_the_vendored_scripts_actually_need() -> None:
    """The regression this exists for.

    M8 shipped a policy without 'unsafe-eval'. Alpine's standard build compiles
    every expression with `new Function`, so Alpine died in any compliant
    browser, every `<template x-if>` rendered nothing, and the connection form
    lost its Host, Port, Username and Share fields. Nothing failed: the server
    still returned 200 with a form that could not be filled in.

    Asserting the relationship rather than the string is the point. Swap Alpine
    for its CSP build, which needs no eval, and this stops requiring the
    exemption.

    Checked against the templates rather than the vendored script, because the
    script is built into the image and absent from a checkout, so a test that
    read it would skip in exactly the situation it exists to catch.
    """
    using_alpine = sorted(
        path.relative_to(TEMPLATE_DIR).as_posix()
        for path in TEMPLATE_DIR.rglob("*.html")
        if any(
            directive in path.read_text(encoding="utf-8")
            for directive in ('x-if="', 'x-show="', 'x-data="', 'x-text="')
        )
    )
    if not using_alpine:
        pytest.skip("no template evaluates an Alpine expression any more")

    assert "'unsafe-eval'" in CONTENT_SECURITY_POLICY, (
        "These templates hide fields behind Alpine expressions, and Alpine's "
        "standard build compiles them with `new Function`. Without "
        "'unsafe-eval' the browser renders nothing inside a <template x-if>, "
        "so fields such as the connection form's Host and Username disappear "
        "with no error anywhere: " + ", ".join(using_alpine[:5])
    )


def test_headers_are_present_on_api_responses_too(authed_client: TestClient) -> None:
    assert authed_client.get("/api/jobs").headers["x-frame-options"] == "DENY"


def test_headers_are_present_on_a_refusal(client: TestClient) -> None:
    """A 403 or a 401 is still a page a browser renders."""
    response = client.get("/api/health/detail")
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_session_cookie_is_http_only_and_same_site(client: TestClient) -> None:
    """HttpOnly keeps the session out of any script that gets injected, and
    SameSite=Lax is the second line under the CSRF token."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "bootstrap-password-1"},
        follow_redirects=False,
    )
    cookie = response.headers.get("set-cookie", "")
    assert "hivesync_session" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()

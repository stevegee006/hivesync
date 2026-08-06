"""CSRF protection for cookie-authenticated requests. SPEC section 15.

Enforced in middleware rather than per route, deliberately. A route added later
is protected without anyone remembering to protect it, and opting out becomes a
visible edit rather than an omission nobody notices. Unsafe requests here delete
files, so "someone forgot the decorator" is the wrong failure mode to accept.

The token lives in the signed session cookie and is submitted as a form field or
a header. That is a synchroniser token rather than double-submit: the cookie is
signed with the application key, so a value planted by a subdomain or by an
attacker's page cannot match one this server issued.

**Exempt, and why:**

- Safe methods. Anything under GET that changes state is a bug in that route.
- Requests carrying `HIVESYNC_API_TOKEN` as a bearer token. A browser never
  attaches one on its own, so there is nothing for a cross-site page to forge.
  Requiring a token there would protect nothing and make the API script-hostile.
- Nothing else. Login in particular is **not** exempt: without a token there, an
  attacker can log a victim into an attacker-controlled account and collect
  whatever they do next. The login page mints a token before there is a user.

**The body is only read when it has to be.** A header token is checked first, so
JSON and HTMX requests never have their body touched. When a form body must be
parsed to find the token, the raw bytes are replayed to the route underneath:
Starlette's form cache belongs to one Request instance, and the route builds its
own, so without a replay the route would read from an already-consumed stream.
The replay is bounded, because this runs before authentication and an unbounded
read is a denial of service anyone can trigger.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "X-CSRF-Token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_FORM_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data"})

# A configuration import is a few kilobytes. Nothing legitimate posts a form
# larger than this, and buffering happens before authentication.
MAX_BUFFERED_BODY = 8 * 1024 * 1024


def token_for(request: Request) -> str:
    """The session's token, minting one on first use.

    Called while rendering any page carrying a form, so an anonymous visitor on
    the login page has a token before there is a user to attach it to.
    """
    token = request.session.get(SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def rotate(request: Request) -> str:
    """Issue a fresh token, discarding the old one.

    Called on login and logout. A token minted before authentication must not
    remain valid after it, or a value fixed by an attacker survives the
    privilege change that made it worth having.
    """
    token = secrets.token_urlsafe(32)
    request.session[SESSION_KEY] = token
    return token


def api_token_authenticated(request: Request) -> bool:
    """Whether this request presents the configured API bearer token."""
    settings = getattr(request.app.state, "settings", None)
    expected = (getattr(settings, "api_token", None) or "").strip()
    if not expected:
        return False
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip(), expected)


class CsrfMiddleware:
    """Refuses unsafe requests that carry no valid token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if request.method in SAFE_METHODS or api_token_authenticated(request):
            await self.app(scope, receive, send)
            return

        expected = request.session.get(SESSION_KEY)
        header_token = request.headers.get(HEADER_NAME)

        if header_token:
            if self._matches(expected, header_token):
                await self.app(scope, receive, send)
                return
            await self._refuse(request, scope, receive, send)
            return

        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _FORM_TYPES:
            # No header, no form to look in. Nothing to accept.
            await self._refuse(request, scope, receive, send)
            return

        body, too_large = await _buffer(receive)
        if too_large:
            response = PlainTextResponse("That upload is too large.", status_code=413)
            await response(scope, receive, send)
            return

        replay = _replayer(body)
        presented = await _form_token(Request(scope, receive=replay))
        if not self._matches(expected, presented):
            await self._refuse(request, scope, replay, send)
            return

        await self.app(scope, replay, send)

    @staticmethod
    def _matches(expected: object, presented: str | None) -> bool:
        if not isinstance(expected, str) or not expected or not presented:
            return False
        return hmac.compare_digest(presented, expected)

    async def _refuse(self, request: Request, scope: Scope, receive: Receive, send: Send) -> None:
        logger.warning(
            "Rejected a request with no valid CSRF token",
            extra={"path": request.url.path, "method": request.method},
        )
        response = _refusal(request)
        await response(scope, receive, send)


async def _buffer(receive: Receive) -> tuple[bytes, bool]:
    """Read the whole body, refusing anything implausible for a form."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message: Message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > MAX_BUFFERED_BODY:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


def _replayer(body: bytes) -> Receive:
    """A receive callable that hands the same body to every reader.

    Idempotent on purpose: the middleware parses the form to find the token and
    the route parses it again, each through its own Request instance.
    """

    async def replay() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


async def _form_token(request: Request) -> str | None:
    try:
        form = await request.form()
    except Exception:
        return None
    value = form.get(FORM_FIELD)
    return value if isinstance(value, str) else None


def _refusal(request: Request) -> Response:
    message = (
        "This request was refused because its security token was missing or out "
        "of date. That usually means the page was open for a long time, or you "
        "signed in again somewhere else. Reload the page and try again."
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=403)
    return PlainTextResponse(message, status_code=403)

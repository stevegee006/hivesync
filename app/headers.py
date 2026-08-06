"""Response headers that limit what a browser will do with a page.

Not in SPEC section 15, added at M8 because the omission is a real path to the
same outcome the rest of the milestone is preventing. This UI has a "Run" button
that deletes files; a page framed inside someone else's site and clicked through
is a deletion with no CSRF token needed, because the click is genuine.

- `frame-ancestors 'none'` refuses framing. `X-Frame-Options` repeats it for
  anything that predates CSP.
- `X-Content-Type-Options: nosniff` stops a browser guessing that an uploaded
  configuration file is HTML and running it.
- `Referrer-Policy: same-origin` keeps paths, which contain job and run ids, out
  of the Referer header sent to anywhere else.
- A restrictive CSP. The application vendors htmx and Alpine rather than using a
  CDN, so `self` covers everything; there is no external origin to allow.
  `unsafe-inline` is required for style attributes and the small inline
  handlers in the templates, so this is a mitigation rather than a guarantee.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        # 'unsafe-eval' is required, not decorative. Alpine's standard build
        # compiles every x-if and x-show expression with `new Function`, so a
        # policy without it silently kills Alpine: the browser renders nothing
        # inside a <template x-if>, and the connection form loses its Host,
        # Port, Username and Share fields entirely. That shipped in M8 and was
        # caught by hand, which is why test_security_headers.py now asserts the
        # relationship instead of the string.
        #
        # Removing it means moving to Alpine's CSP build, whose expressions are
        # limited to plain property access, and rewriting every conditional in
        # the templates. Worth doing; not worth pretending is done.
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    )
)

HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in message.get("headers", [])}
                # Never override a header a route set deliberately.
                message["headers"] = list(message.get("headers", [])) + [
                    (name, value) for name, value in HEADERS if name not in existing
                ]
            await send(message)

        await self.app(scope, receive, with_headers)

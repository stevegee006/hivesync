"""Authentication: argon2id password hashing, sessions, and proxy trust.

Three modes, per SPEC section 14:

- `local`, a username and password against the user table.
- `none`, every request acts as the single admin. For deployments where a proxy
  authenticates already. Warned about at startup and banner-flagged in the UI.
- `trusted_header`, an identity asserted by a reverse proxy. Implemented at M8,
  having refused to start since M0, because a half-checked proxy trust is an
  authentication bypass rather than a missing feature.

**Two rules make `trusted_header` safe, and both matter.** The header is honoured
only when the *socket peer* is inside `HIVESYNC_TRUSTED_PROXIES`, never when a
request merely claims to have come through one. And the header maps to an
existing user: it never creates one. A header that provisions an admin account
is not authentication, it is a registration form with no password.

CSRF lives in app/csrf.py, and API bearer tokens are checked here so that one
module answers "who is this request".
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import logging
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.models import User, UserRole, utcnow

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()

SESSION_USER_KEY = "user_id"

# Compared against when no user matches, so a missing username and a wrong
# password cost roughly the same time and cannot be told apart.
_DUMMY_HASH = _hasher.hash("hivesync-timing-equaliser")

MIN_PASSWORD_LENGTH = 12


class NotAuthenticated(Exception):
    """Raised by the auth dependency. Handled in main.py, which redirects HTML
    requests to the login page and returns 401 for API requests."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, str | None]:
    """Verify a password. Returns (ok, replacement_hash_if_parameters_are_stale)."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    except Argon2Error:
        return False, None
    if _hasher.check_needs_rehash(password_hash):
        return True, _hasher.hash(password)
    return True, None


def verify_dummy() -> None:
    """Burn a comparable amount of time when the username does not exist."""
    with contextlib.suppress(Argon2Error):
        _hasher.verify(_DUMMY_HASH, "wrong")


def validate_password_strength(password: str) -> str | None:
    """Return a user-facing reason the password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    return None


def authenticate(session: Session, username: str, password: str) -> User | None:
    """Check a username and password. Returns the user, or None."""
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        verify_dummy()
        return None
    ok, replacement = verify_password(user.password_hash, password)
    if not ok:
        return None
    if replacement is not None:
        user.password_hash = replacement
    user.last_login_at = utcnow()
    session.commit()
    return user


def set_password(session: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    user.must_change_password = False
    session.commit()


def start_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id


def end_session(request: Request) -> None:
    request.session.clear()


def _bootstrap_admin_user(session: Session) -> User | None:
    return session.scalar(select(User).order_by(User.id).limit(1))


def peer_is_trusted_proxy(request: Request, settings: Settings) -> bool:
    """Whether the socket peer is inside the configured proxy allowlist.

    The peer address, not X-Forwarded-For. On a direct connection the client sets
    that header itself, so trusting it would let anyone claim to be the proxy.
    """
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in settings.trusted_proxy_list:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.error(
                "Ignoring an unparseable entry in HIVESYNC_TRUSTED_PROXIES",
                extra={"entry": entry},
            )
    return False


def _trusted_header_user(request: Request, session: Session, settings: Settings) -> User | None:
    """Resolve the user a trusted proxy asserts, or None."""
    if not peer_is_trusted_proxy(request, settings):
        logger.warning(
            "Ignoring an identity header from an address outside the proxy allowlist",
            extra={"peer": request.client.host if request.client else "unknown"},
        )
        return None
    username = (request.headers.get(settings.trusted_header or "") or "").strip()
    if not username:
        return None
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        # Never auto-created. See the module docstring.
        logger.warning(
            "A trusted proxy asserted an identity with no matching account",
            extra={"username": username},
        )
    return user


def api_token_user(request: Request, session: Session) -> User | None:
    """The acting user for a request carrying HIVESYNC_API_TOKEN.

    Full privilege, the same as the admin, and exempt from CSRF because a browser
    never attaches a bearer token on its own. See app/csrf.py.
    """
    settings: Settings = request.app.state.settings
    expected = (settings.api_token or "").strip()
    if not expected:
        return None
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    if not hmac.compare_digest(presented.strip(), expected):
        return None
    return _bootstrap_admin_user(session)


def current_user(request: Request, session: Session) -> User | None:
    """Resolve the acting user, or None. Never raises."""
    settings: Settings = request.app.state.settings

    if settings.auth_mode == "none":
        # Every request acts as the single admin. Warned about at startup and
        # banner-flagged in the UI.
        return _bootstrap_admin_user(session)

    by_token = api_token_user(request, session)
    if by_token is not None:
        return by_token

    if settings.auth_mode == "trusted_header":
        return _trusted_header_user(request, session, settings)

    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = session.get(User, user_id)
    if user is None:
        # Account deleted while a cookie was still live.
        request.session.clear()
    return user


def require_user(request: Request, session: Session) -> User:
    user = current_user(request, session)
    if user is None:
        raise NotAuthenticated
    return user


class BootstrapError(Exception):
    """The initial admin account cannot be created from the current environment."""


def bootstrap_admin(session: Session, settings: Settings) -> bool:
    """Create the initial admin if the user table is empty. Returns True if created.

    SPEC section 14 describes generating a password when HIVESYNC_ADMIN_PASSWORD
    is unset. This refuses to start instead, because the only way to hand a
    generated password to the operator is to print it, and CLAUDE.md rule 5 says
    no secret reaches a log line. Failing with an actionable message is also what
    rule 8 asks for. The account is still flagged must_change_password so the
    bootstrap value cannot become the permanent one.
    """
    existing = session.scalar(select(func.count()).select_from(User))
    if existing:
        return False

    password = settings.admin_password
    if not password:
        raise BootstrapError(
            "No admin account exists yet and HIVESYNC_ADMIN_PASSWORD is not set, "
            "so there is no way to sign in. Set HIVESYNC_ADMIN_PASSWORD to a value "
            f"of at least {MIN_PASSWORD_LENGTH} characters and start again. You "
            "will be prompted to change it at first login, and the variable can be "
            "removed afterwards. A suggestion: "
            f"{secrets.token_urlsafe(18)}"
        )

    weakness = validate_password_strength(password)
    if weakness:
        raise BootstrapError(
            f"HIVESYNC_ADMIN_PASSWORD is not acceptable. {weakness} "
            "Set a longer value and start again."
        )

    session.add(
        User(
            username=settings.admin_user,
            password_hash=hash_password(password),
            role=UserRole.admin,
            # The bootstrap value is for first login only, never permanent.
            must_change_password=True,
        )
    )
    session.commit()
    logger.info("Created bootstrap admin account", extra={"username": settings.admin_user})
    return True

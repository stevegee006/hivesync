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


def needs_setup(session: Session) -> bool:
    """True when no account exists yet, so the instance is unclaimed.

    While this holds, every page redirects to the setup wizard and the wizard is
    reachable without signing in, because there is nothing to sign in to.
    """
    return not session.scalar(select(func.count()).select_from(User))


def create_first_admin(session: Session, username: str, password: str) -> User:
    """Claim an unclaimed instance. Raises BootstrapError if it is already taken.

    The emptiness check is repeated **after** the insert rather than only
    before. Two people loading the wizard at the same moment would both see an
    empty table, and the first to submit should be the only one who gets an
    account: the second must be told the instance is taken rather than quietly
    given a second admin. SQLite serialises writers, so the loser's own commit
    is what reveals the race.
    """
    if not needs_setup(session):
        raise BootstrapError(
            "This instance already has an account, so it cannot be set up again. "
            "Sign in, or reset the password from the command line."
        )

    weakness = validate_password_strength(password)
    if weakness:
        raise BootstrapError(weakness)

    name = username.strip()
    if not name:
        raise BootstrapError("A username is required.")

    user = User(
        username=name,
        password_hash=hash_password(password),
        role=UserRole.admin,
        # Chosen by a person just now, so there is nothing to force a change of.
        must_change_password=False,
    )
    session.add(user)
    session.commit()

    total = session.scalar(select(func.count()).select_from(User))
    if total != 1:
        session.delete(user)
        session.commit()
        raise BootstrapError(
            "Another account was created at the same moment, so this one was "
            "discarded. Sign in with the account that was created first."
        )

    logger.info("Admin account created from the setup wizard", extra={"username": name})
    return user


def bootstrap_admin(session: Session, settings: Settings) -> bool:
    """Pre-provision the initial admin from the environment. True if created.

    Optional. Without `HIVESYNC_ADMIN_PASSWORD` the instance starts unclaimed
    and the first visitor completes the setup wizard, which is the ordinary
    path. This exists for two cases the wizard does not serve: an automated
    deployment that wants an account to exist before anyone can reach the port,
    and anyone exposing the instance publicly, where the window between starting
    the container and finishing the wizard is a window in which a stranger could
    claim it.

    SPEC section 14 describes generating a password when the variable is unset.
    Nothing is generated: the only way to hand a generated password to the
    operator is to print it, and CLAUDE.md rule 5 says no secret reaches a log
    line. The account is flagged must_change_password so a value that has been
    sitting in a compose file cannot become the permanent one.
    """
    existing = session.scalar(select(func.count()).select_from(User))
    if existing:
        return False

    password = settings.admin_password
    if not password:
        logger.warning(
            "No account exists yet. The first visitor to this instance will be "
            "asked to create one, and anyone who can reach it can be that visitor. "
            "Complete the setup before exposing it, or set HIVESYNC_ADMIN_PASSWORD "
            "to create the account up front."
        )
        return False

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

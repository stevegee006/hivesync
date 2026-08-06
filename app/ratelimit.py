"""Login rate limiting. SPEC section 15.

Counted per username and per source address, whichever trips first. Limiting only
by username lets one attacker walk a password list across every account at full
speed; limiting only by address lets a botnet walk one account. Both, and the
stricter answer wins.

State lives in the database, so a restart does not clear a lockout.

**The caller must not tell them apart.** A wrong password, an unknown username and
a locked account produce the same response. A limiter that says "locked" is a
username oracle: it confirms the account exists, which is the thing the identical
failure message elsewhere is protecting. The lockout is still logged, where the
operator can see it and the attacker cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.models import AttemptScope, LoginAttempt, utcnow

logger = logging.getLogger(__name__)

# Failures older than this are irrelevant to any window we enforce, so they are
# swept on write rather than accumulating forever.
RETENTION = timedelta(days=7)


@dataclass(frozen=True)
class Verdict:
    """Whether an attempt may proceed, and how long until it may."""

    allowed: bool
    retry_after_seconds: int = 0
    scope: AttemptScope | None = None


def client_address(request: Request) -> str:
    """The address to count against.

    The socket peer, never a self-reported header. `X-Forwarded-For` is set by
    the client on a direct connection, so counting it would let anyone reset
    their own limit by changing a header. Behind a proxy every request shares one
    address and the per-username limit carries the weight, which is the safe way
    round: it over-counts rather than under-counts.
    """
    return request.client.host if request.client else "unknown"


def _recent(session: Session, scope: AttemptScope, value: str, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.scope == scope,
                LoginAttempt.value == value,
                LoginAttempt.attempted_at >= since,
            )
        )
        or 0
    )


def _oldest_in_window(
    session: Session, scope: AttemptScope, value: str, since: datetime
) -> datetime | None:
    return session.scalar(
        select(func.min(LoginAttempt.attempted_at)).where(
            LoginAttempt.scope == scope,
            LoginAttempt.value == value,
            LoginAttempt.attempted_at >= since,
        )
    )


def check(session: Session, settings: Settings, *, username: str, address: str) -> Verdict:
    """Whether this login attempt may proceed. Records nothing."""
    window = timedelta(seconds=settings.login_lockout_seconds)
    since = utcnow() - window
    limit = settings.login_max_attempts

    scopes = ((AttemptScope.username, username.lower()), (AttemptScope.address, address))
    for scope, value in scopes:
        if not value:
            continue
        if _recent(session, scope, value, since) < limit:
            continue
        oldest = _oldest_in_window(session, scope, value, since)
        if oldest is None:
            continue
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=since.tzinfo)
        # The window slides: the lock lifts when the oldest failure in it ages
        # out, not a fixed period after the last one. A fixed period after the
        # last attempt means an attacker who keeps trying extends their own
        # lockout indefinitely, which sounds fine until it is a locked-out
        # operator retrying every minute.
        remaining = int((oldest + window - utcnow()).total_seconds())
        return Verdict(allowed=False, retry_after_seconds=max(remaining, 1), scope=scope)

    return Verdict(allowed=True)


def record_failure(session: Session, *, username: str, address: str) -> None:
    """Count one failed attempt against both scopes."""
    now = utcnow()
    if username:
        session.add(
            LoginAttempt(scope=AttemptScope.username, value=username.lower(), attempted_at=now)
        )
    if address:
        session.add(LoginAttempt(scope=AttemptScope.address, value=address, attempted_at=now))
    session.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < now - RETENTION))
    session.commit()


def clear(session: Session, *, username: str, address: str) -> None:
    """Forget the failures for a successful sign in.

    Both scopes, so one person fixing their typo does not leave the address they
    share with everyone else near its limit.
    """
    session.execute(
        delete(LoginAttempt).where(
            (
                (LoginAttempt.scope == AttemptScope.username)
                & (LoginAttempt.value == username.lower())
            )
            | ((LoginAttempt.scope == AttemptScope.address) & (LoginAttempt.value == address))
        )
    )
    session.commit()

"""Failed login attempts, for rate limiting. SPEC section 15.

In the database rather than in memory, for one reason: a restart must not clear a
lockout. An in-process counter turns "lock this account for fifteen minutes" into
"lock it until someone restarts the container", and restarting a container is
something anyone who can reach the health endpoint can provoke by other means.

Rows are keyed by a scope and a value: the username someone tried, and the
address they tried it from. Both are counted, because limiting only by username
lets one attacker walk a password list across every account, and limiting only by
address lets a botnet walk one account.

The username is stored as attempted, not as resolved. It is not a credential and
it is already in the log line for the same event; storing it is what makes the
limit per-account rather than global.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UtcDateTime, str_enum, utcnow


class AttemptScope(enum.StrEnum):
    username = "username"
    address = "address"


class LoginAttempt(Base):
    __tablename__ = "login_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[AttemptScope] = mapped_column(str_enum(AttemptScope, length=16), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        # The lookup is always "recent failures for this scope and value".
        Index("ix_login_attempt_scope_value_time", "scope", "value", "attempted_at"),
    )

    def __repr__(self) -> str:
        return f"LoginAttempt(scope={self.scope!r}, value={self.value!r})"

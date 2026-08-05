"""Local user accounts.

must_change_password is not in SPEC section 4's model, but SPEC section 14 says
the bootstrap admin password forces a change on first login when it is unset.
That flag needs a column, so it is in the baseline.

Only the argon2id hash is stored. There is no password column and no reveal path.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UtcDateTime, str_enum, utcnow


class UserRole(enum.StrEnum):
    admin = "admin"


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole, length=16), nullable=False, default=UserRole.admin
    )
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, username={self.username!r}, role={self.role!r})"

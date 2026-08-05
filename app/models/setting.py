"""Key/value application settings.

Holds the secret key fingerprint written at first boot, plus the auth,
notification, concurrency and default values from SPEC section 13 screen 7.
Values are plain text. Nothing secret is stored here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UtcDateTime, utcnow

# Well known keys.
SECRET_KEY_FINGERPRINT = "secret_key_fingerprint"


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"Setting(key={self.key!r})"

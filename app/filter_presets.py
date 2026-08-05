"""Built-in filter presets, seeded at startup.

SPEC section 11.3. These are not a nicety: DSM scatters metadata directories
through every volume, and without these excludes they get synced, archived, and
re-archived forever. A dry run against a Synology share is unreadable without
them, which is why they are seeded at M2 alongside the planner rather than left
for the polish milestone.

Seeded rules are refreshed on every startup so a corrected pattern reaches
existing installations. User-created presets are never touched.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FilterPreset

logger = logging.getLogger(__name__)

BUILTIN_PRESETS: dict[str, list[str]] = {
    # @eaDir holds thumbnails and index data and appears at every directory
    # level. #recycle appears at the share root when the shared folder Recycle
    # Bin is enabled.
    "Synology / DSM": [
        "**/@eaDir/**",
        "**/@tmp/**",
        "/#recycle/**",
        "**/#snapshot/**",
        "**/.DS_Store",
        "**/Thumbs.db",
        "**/desktop.ini",
    ],
    "Common junk": [
        "**/.DS_Store",
        "**/Thumbs.db",
        "**/desktop.ini",
        "**/*.tmp",
        "**/~$*",
        "**/.Trash-*/**",
        "**/lost+found/**",
    ],
}


def seed_builtin_presets(session: Session) -> None:
    existing = {
        preset.name: preset
        for preset in session.scalars(select(FilterPreset).where(FilterPreset.builtin.is_(True)))
    }
    created = 0
    for name, rules in BUILTIN_PRESETS.items():
        payload = {"exclude": rules}
        preset = existing.get(name)
        if preset is None:
            session.add(FilterPreset(name=name, builtin=True, rules=payload))
            created += 1
        elif preset.rules != payload:
            # Refresh, so a corrected pattern reaches an existing installation.
            preset.rules = payload
    session.commit()
    if created:
        logger.info("Seeded built-in filter presets", extra={"count": created})

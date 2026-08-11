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

# A pattern with no leading slash already matches at every level, **including
# the root**. A leading `**/` does the opposite of what it looks like: it
# requires at least one directory before the name, so `**/@eaDir/**` silently
# misses the `@eaDir` at the top of the synced folder. Verified against rclone
# 1.74.4, which is how this was found: the share root is the most likely place
# for one, so the preset was missing the case it existed for. A leading slash
# anchors to the sync root and is used only where that is deliberate.
BUILTIN_PRESETS: dict[str, list[str]] = {
    # @eaDir holds thumbnails and index data and appears at every directory
    # level. #recycle appears at the share root when the shared folder Recycle
    # Bin is enabled, so it is the one pattern here that is anchored.
    "Synology / DSM": [
        "@eaDir/**",
        "@tmp/**",
        "/#recycle/**",
        "#snapshot/**",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    ],
    # The other half of the answer to "do not copy a file that is still being
    # written". The quiet period catches a file whose mtime is still moving;
    # this catches the temporary names download clients use before the final
    # rename, which is the sturdier signal because the rename is atomic.
    #
    # Patterns are unanchored so they match at every level, per the note above.
    # Extensions are the ones the common clients actually write:
    #   .part        aria2, wget, Firefox, qBittorrent (with the option on)
    #   .!qB         qBittorrent's default incomplete suffix
    #   .!ut, .bc!   uTorrent and BitComet
    #   .crdownload  Chrome and Chromium
    #   .partial     Edge and some SABnzbd configurations
    #   .filepart    JDownloader
    # plus the incomplete directories SABnzbd and NZBGet write into.
    "Downloads in progress": [
        "*.part",
        "*.partial",
        "*.!qB",
        "*.!ut",
        "*.bc!",
        "*.crdownload",
        "*.filepart",
        "*.tmp",
        "*.temp",
        "incomplete/**",
        "_UNPACK_*/**",
        "_FAILED_*/**",
    ],
    "Common junk": [
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "*.tmp",
        "~$*",
        ".Trash-*/**",
        "lost+found/**",
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

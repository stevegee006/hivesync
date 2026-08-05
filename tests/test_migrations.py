"""The Alembic baseline must agree with the models.

This is the test that keeps a hand edited migration from drifting away from the
declarative models. Without it, `alembic upgrade head` in the container can
produce a schema subtly different from the one the tests exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.config import get_settings
from app.db import create_db_engine
from app.models import Base
from tests.conftest import REPO_ROOT, make_settings


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    # migrations/env.py resolves the URL through get_settings, so point that at a
    # throwaway directory rather than the real /config.
    monkeypatch.setenv("HIVESYNC_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    yield config
    get_settings.cache_clear()


def test_upgrade_head_matches_the_models(alembic_config: Config, tmp_path: Path) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_db_engine(make_settings(tmp_path))
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        differences = compare_metadata(context, Base.metadata)

    assert differences == [], (
        "The Alembic baseline has drifted from the models. Regenerate with "
        "`make migration NAME=...` or reconcile by hand."
    )


def test_downgrade_to_base_is_clean(alembic_config: Config, tmp_path: Path) -> None:
    """A baseline that cannot be reversed makes a failed upgrade unrecoverable."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    engine = create_db_engine(make_settings(tmp_path))
    with engine.connect() as connection:
        remaining = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).scalars()
        # Only Alembic's own bookkeeping table should survive.
        assert set(remaining) == {"alembic_version"}


def test_every_model_table_is_created(alembic_config: Config, tmp_path: Path) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_db_engine(make_settings(tmp_path))
    with engine.connect() as connection:
        present = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )

    missing = set(Base.metadata.tables) - present
    assert not missing, f"Tables declared on the models but absent from migrations: {missing}"

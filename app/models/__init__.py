"""Model package. Importing this module registers every table on Base.metadata,
which is what Alembic's autogenerate and the test suite's schema comparison rely
on. A new model file must be imported here or it will silently vanish from
migrations.
"""

from app.models.base import Base, str_enum, utcnow
from app.models.connection import Connection, ConnectionType, RcloneMode
from app.models.credential import Credential, CredentialKind
from app.models.filter_preset import FilterPreset, job_filter_preset
from app.models.job import (
    ArchiveLayout,
    ChangeAction,
    ChangeSide,
    CompareMode,
    ConflictResolve,
    DeleteMode,
    Direction,
    Engine,
    Job,
    JobRun,
    JobRunChange,
    NotifyOn,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.models.setting import SECRET_KEY_FINGERPRINT, Setting
from app.models.user import User, UserRole

__all__ = [
    "SECRET_KEY_FINGERPRINT",
    "ArchiveLayout",
    "Base",
    "ChangeAction",
    "ChangeSide",
    "CompareMode",
    "ConflictResolve",
    "Connection",
    "ConnectionType",
    "Credential",
    "CredentialKind",
    "DeleteMode",
    "Direction",
    "Engine",
    "FilterPreset",
    "Job",
    "JobRun",
    "JobRunChange",
    "NotifyOn",
    "RcloneMode",
    "RunMode",
    "RunStatus",
    "RunTrigger",
    "Setting",
    "User",
    "UserRole",
    "job_filter_preset",
    "str_enum",
    "utcnow",
]

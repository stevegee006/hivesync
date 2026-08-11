"""Settings, notifications, retention preview, and configuration transfer.

SPEC section 12: GET and PATCH /api/settings, POST /api/settings/test-notification.
Export and import are M7 additions to the same screen.

The webhook URL is write-only through this API. It can carry a token in its path,
and a settings screen that renders one back is a settings screen that leaks it to
anyone who can read the page. Sending an empty string leaves the stored value
alone; clearing it is an explicit action.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app import notify, portable
from app import preferences as preferences_store
from app.api.deps import AppSettings, CurrentUser, DbSession
from app.jobs import retention
from app.preferences import Clock, NotifyTarget, Preferences

router = APIRouter(tags=["settings"])


class SettingsRead(BaseModel):
    """Everything the screen shows. Never the webhook URL itself."""

    notify_target: NotifyTarget
    notify_webhook_configured: bool
    notify_ntfy_server: str
    notify_ntfy_topic: str
    notify_timeout_seconds: int
    base_url: str
    archive_retention_days: int | None
    run_history_keep: int
    log_retention_days: int
    log_max_total_mb: int
    display_timezone: str
    clock: Clock
    # 0 means the environment's value is in force.
    max_concurrent_runs_override: int
    # The zone in force once the empty "follow the environment" case is
    # resolved, so a caller does not have to know the rule to display it.
    effective_timezone: str
    # Read only, from the environment. Changing these needs a restart, and
    # showing them here is how an operator finds that out.
    auth_mode: str
    max_concurrent_runs: int


class SettingsUpdate(BaseModel):
    notify_target: NotifyTarget | None = None
    # Empty string means "leave it alone". Use clear_webhook_url to remove it.
    notify_webhook_url: str | None = None
    clear_webhook_url: bool = False
    notify_ntfy_server: str | None = None
    notify_ntfy_topic: str | None = None
    notify_timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    base_url: str | None = None
    archive_retention_days: int | None = Field(default=None, ge=1)
    clear_archive_retention: bool = False
    run_history_keep: int | None = Field(default=None, ge=10, le=10000)
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)
    log_max_total_mb: int | None = Field(default=None, ge=16, le=102400)
    # An empty string is a real value here: it means "follow the TZ environment
    # variable" rather than "leave it alone", so unlike the webhook URL it is
    # not skipped when blank.
    display_timezone: str | None = None
    clock: Clock | None = None
    max_concurrent_runs: int | None = Field(default=None, ge=0, le=32)


def to_read(
    preferences: Preferences,
    *,
    auth_mode: str,
    max_concurrent_runs: int,
    environment_timezone: str = "UTC",
) -> SettingsRead:
    return SettingsRead(
        notify_target=preferences.notify_target,
        notify_webhook_configured=bool(preferences.notify_webhook_url),
        notify_ntfy_server=preferences.notify_ntfy_server,
        notify_ntfy_topic=preferences.notify_ntfy_topic,
        notify_timeout_seconds=preferences.notify_timeout_seconds,
        base_url=preferences.base_url,
        archive_retention_days=preferences.archive_retention_days,
        run_history_keep=preferences.run_history_keep,
        log_retention_days=preferences.log_retention_days,
        log_max_total_mb=preferences.log_max_total_mb,
        display_timezone=preferences.display_timezone,
        clock=preferences.clock,
        max_concurrent_runs_override=preferences.max_concurrent_runs,
        effective_timezone=preferences.display_timezone or environment_timezone,
        auth_mode=auth_mode,
        max_concurrent_runs=preferences.max_concurrent_runs or max_concurrent_runs,
    )


def apply_update(current: Preferences, update: SettingsUpdate) -> Preferences:
    """Fold a patch into the current preferences.

    Kept as a function rather than inlined in the handler so the web form and the
    API cannot drift on what an empty field means.
    """
    data = current.model_dump()
    for name, value in update.model_dump(exclude_none=True).items():
        if name in ("clear_webhook_url", "clear_archive_retention"):
            continue
        if name == "notify_webhook_url" and not value:
            continue
        data[name] = value
    if update.clear_webhook_url:
        data["notify_webhook_url"] = ""
    if update.clear_archive_retention:
        data["archive_retention_days"] = None
    return Preferences.model_validate(data)


@router.get("/settings", response_model=SettingsRead)
def read_settings(_user: CurrentUser, session: DbSession, settings: AppSettings) -> SettingsRead:
    return to_read(
        preferences_store.load(session),
        auth_mode=settings.auth_mode,
        max_concurrent_runs=settings.max_concurrent_runs,
        environment_timezone=settings.timezone,
    )


@router.patch("/settings", response_model=SettingsRead)
def update_settings(
    payload: SettingsUpdate,
    _user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SettingsRead:
    updated = preferences_store.save(
        session, apply_update(preferences_store.load(session), payload)
    )
    return to_read(
        updated,
        auth_mode=settings.auth_mode,
        max_concurrent_runs=settings.max_concurrent_runs,
        environment_timezone=settings.timezone,
    )


class NotificationTest(BaseModel):
    attempted: bool
    ok: bool
    detail: str


@router.post("/settings/test-notification", response_model=NotificationTest)
def test_notification(_user: CurrentUser, session: DbSession) -> NotificationTest:
    """Send a sample payload to the configured target.

    Shaped exactly like a real notification, so what is tested is what arrives.
    Never raises: a failed delivery is a result to display, not a 500.
    """
    preferences = preferences_store.load(session)
    delivery = notify.send(preferences, notify.sample_payload())
    return NotificationTest(attempted=delivery.attempted, ok=delivery.ok, detail=delivery.detail)


class RetentionPreview(BaseModel):
    """What the next maintenance pass would delete. Deletes nothing."""

    job: str
    retention_days: int
    directories: list[str]
    bytes_freed: int
    skipped_reason: str | None


@router.get("/settings/retention-preview", response_model=list[RetentionPreview])
def retention_preview(_user: CurrentUser, session: DbSession) -> list[RetentionPreview]:
    report = retention.plan(session, preferences_store.load(session))
    return [
        RetentionPreview(
            job=plan.job_name,
            retention_days=plan.retention_days,
            directories=[str(path) for path in plan.directories],
            bytes_freed=plan.bytes_freed,
            skipped_reason=plan.skipped_reason,
        )
        for plan in report.archives
    ]


class MaintenanceResult(BaseModel):
    directories_removed: int
    bytes_freed: int
    logs_removed: int
    runs_removed: int
    errors: list[str]


@router.post("/settings/run-maintenance", response_model=MaintenanceResult)
def run_maintenance(
    _user: CurrentUser, session: DbSession, settings: AppSettings
) -> MaintenanceResult:
    """Run the maintenance pass now, instead of waiting for the nightly one."""
    report = retention.run(session, settings, preferences_store.load(session))
    return MaintenanceResult(
        directories_removed=report.directories_removed,
        bytes_freed=report.bytes_freed,
        logs_removed=report.logs_removed,
        runs_removed=report.runs_removed,
        errors=report.errors,
    )


@router.get("/settings/export")
def export_configuration(_user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """The whole configuration, with no credential material of any kind."""
    return portable.export(session)


class ImportResult(BaseModel):
    ok: bool
    connections_created: int
    connections_skipped: int
    jobs_created: int
    jobs_skipped: int
    presets_created: int
    preferences_applied: bool
    warnings: list[str]
    errors: list[str]


@router.post("/settings/import", response_model=ImportResult)
def import_configuration(
    _user: CurrentUser,
    session: DbSession,
    document: dict[str, Any],
) -> ImportResult:
    report = portable.import_document(session, document)
    if not report.ok and not (report.connections_created or report.jobs_created):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=" ".join(report.errors),
        )
    return ImportResult(
        ok=report.ok,
        connections_created=report.connections_created,
        connections_skipped=report.connections_skipped,
        jobs_created=report.jobs_created,
        jobs_skipped=report.jobs_skipped,
        presets_created=report.presets_created,
        preferences_applied=report.preferences_applied,
        warnings=report.warnings,
        errors=report.errors,
    )

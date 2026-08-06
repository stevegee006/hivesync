"""Configuration export and import. SPEC section 18, M7.

**No credential material leaves this process, in any form, including ciphertext.**
An export is a plain JSON file that people put in a git repository or paste into
a support thread. Fernet ciphertext is only as strong as the key, and the key
lives in an environment variable in the same compose file people commit next to
it. So credentials are exported as names only, and an import re-links by name.

The consequence is deliberate and visible: a job imported into a fresh instance
references a credential that does not exist yet, and its connection reports as
unconfigured until someone re-enters the secret. That is the honest outcome. The
alternative is an import that appears to work and fails at two in the morning.

Also excluded, for a different reason: everything a probe produces. Capabilities,
host keys, last-test results and run history describe an environment, not a
configuration, and importing them would assert facts about endpoints this
instance has never contacted. Host keys in particular are a trust decision and
must be made again on the machine doing the trusting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import preferences as preferences_store
from app.models import Connection, Credential, FilterPreset, Job
from app.preferences import Preferences

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

CONNECTION_FIELDS = (
    "name",
    "type",
    "host",
    "port",
    "share",
    "base_path",
    "username",
    "extra_opts",
    "rclone_mode",
    "rclone_remote_name",
    "rclone_backend_type",
    "sentinel_file",
)

JOB_FIELDS = (
    "name",
    "enabled",
    "source_path",
    "dest_path",
    "engine",
    "direction",
    "delete_mode",
    "archive_base",
    "archive_layout",
    "archive_retention_days",
    "filters",
    "compare_mode",
    "modify_window",
    "transfers",
    "checkers",
    "bwlimit",
    "max_delete_pct",
    "conflict_resolve",
    "check_access",
    "schedule_cron",
    "timezone",
    "timeout_seconds",
    "notify_on",
)

# Preferences that describe this deployment rather than the configuration, or
# that can carry a token. See the module docstring.
PREFERENCE_EXCLUDES = frozenset({"notify_webhook_url", "notify_ntfy_topic", "base_url"})


@dataclass
class ImportReport:
    """What an import did, in terms someone can check against their old install."""

    connections_created: int = 0
    connections_skipped: int = 0
    jobs_created: int = 0
    jobs_skipped: int = 0
    presets_created: int = 0
    preferences_applied: bool = False
    # Things the operator must now do by hand. Never silently swallowed.
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _value(instance: object, name: str) -> Any:
    value = getattr(instance, name)
    # StrEnum serialises as its value, so the file reads as plain strings.
    return value.value if hasattr(value, "value") else value


def export(session: Session) -> dict[str, Any]:
    """Build the export document. Contains no secret, by construction."""
    connections = []
    for connection in session.scalars(select(Connection).order_by(Connection.name)):
        entry = {name: _value(connection, name) for name in CONNECTION_FIELDS}
        # By name, never by id: ids differ between installations. Absent when the
        # connection needs no credential at all.
        entry["credential_name"] = connection.credential.name if connection.credential else None
        connections.append(entry)

    jobs = []
    for job in session.scalars(select(Job).order_by(Job.name)):
        entry = {name: _value(job, name) for name in JOB_FIELDS}
        entry["source_connection"] = job.source_connection.name if job.source_connection else None
        entry["dest_connection"] = job.dest_connection.name if job.dest_connection else None
        entry["filter_presets"] = [preset.name for preset in job.filter_presets]
        jobs.append(entry)

    presets = [
        {"name": preset.name, "rules": preset.rules}
        for preset in session.scalars(
            select(FilterPreset).where(FilterPreset.builtin.is_(False)).order_by(FilterPreset.name)
        )
    ]

    preferences = {
        key: value
        for key, value in preferences_store.load(session).model_dump().items()
        if key not in PREFERENCE_EXCLUDES
    }

    # Names only. Enough for an import to re-link and for a person to know what
    # they have to re-enter.
    credentials = [
        {"name": credential.name, "kind": credential.kind.value}
        for credential in session.scalars(select(Credential).order_by(Credential.name))
    ]

    return {
        "format_version": FORMAT_VERSION,
        "note": (
            "Contains no credentials, not even encrypted ones. Re-enter each "
            "secret listed under credentials_required after importing."
        ),
        "connections": connections,
        "jobs": jobs,
        "filter_presets": presets,
        "preferences": preferences,
        "credentials_required": credentials,
    }


def import_document(session: Session, document: dict[str, Any]) -> ImportReport:
    """Apply an export to this instance.

    Additive and idempotent: anything whose name already exists is skipped rather
    than overwritten. An import must never be able to silently rewrite a working
    job, and "skipped" is a report line, not a failure.
    """
    report = ImportReport()

    version = document.get("format_version")
    if version != FORMAT_VERSION:
        report.errors.append(
            f"This file says it is format version {version!r}, and this version of "
            f"HiveSync reads version {FORMAT_VERSION}. Nothing was imported."
        )
        return report

    credentials = {
        credential.name: credential for credential in session.scalars(select(Credential))
    }
    _import_presets(session, document, report)
    presets = {preset.name: preset for preset in session.scalars(select(FilterPreset))}
    _import_connections(session, document, credentials, report)
    session.flush()

    connections = {
        connection.name: connection for connection in session.scalars(select(Connection))
    }
    _import_jobs(session, document, connections, presets, report)
    _import_preferences(session, document, report)

    session.commit()
    logger.info(
        "Configuration imported",
        extra={
            "connections": report.connections_created,
            "jobs": report.jobs_created,
            "presets": report.presets_created,
            "warnings": len(report.warnings),
        },
    )
    return report


def _import_presets(session: Session, document: dict[str, Any], report: ImportReport) -> None:
    existing = {preset.name for preset in session.scalars(select(FilterPreset))}
    for entry in document.get("filter_presets") or []:
        name = str(entry.get("name") or "").strip()
        if not name or name in existing:
            continue
        session.add(FilterPreset(name=name, builtin=False, rules=entry.get("rules") or {}))
        existing.add(name)
        report.presets_created += 1


def _import_connections(
    session: Session,
    document: dict[str, Any],
    credentials: dict[str, Credential],
    report: ImportReport,
) -> None:
    existing = {connection.name for connection in session.scalars(select(Connection))}
    for entry in document.get("connections") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            report.errors.append("A connection in this file has no name and was not imported.")
            continue
        if name in existing:
            report.connections_skipped += 1
            continue

        fields = {key: entry.get(key) for key in CONNECTION_FIELDS if key in entry}
        fields["name"] = name
        fields.setdefault("base_path", "")
        fields.setdefault("extra_opts", {})

        credential_name = entry.get("credential_name")
        if credential_name:
            credential = credentials.get(str(credential_name))
            if credential is None:
                # Not an error: the export never carried the secret, so this is
                # the expected state on a fresh instance. It has to be said out
                # loud, because the connection cannot authenticate until it is
                # fixed.
                report.warnings.append(
                    f"Connection '{name}' needs the credential '{credential_name}', "
                    "which does not exist here. Create it and attach it to the "
                    "connection: exports never contain secrets."
                )
            else:
                fields["credential_id"] = credential.id

        session.add(Connection(**fields))
        existing.add(name)
        report.connections_created += 1


def _import_jobs(
    session: Session,
    document: dict[str, Any],
    connections: dict[str, Connection],
    presets: dict[str, FilterPreset],
    report: ImportReport,
) -> None:
    existing = {job.name for job in session.scalars(select(Job))}
    for entry in document.get("jobs") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            report.errors.append("A job in this file has no name and was not imported.")
            continue
        if name in existing:
            report.jobs_skipped += 1
            continue

        source = connections.get(str(entry.get("source_connection") or ""))
        dest = connections.get(str(entry.get("dest_connection") or ""))
        if source is None or dest is None:
            # A job with a dangling endpoint would be a row that cannot run and
            # cannot be fixed from the UI, so it is refused rather than created.
            missing = entry.get("source_connection") if source is None else None
            missing = missing or entry.get("dest_connection")
            report.errors.append(
                f"Job '{name}' refers to the connection '{missing}', which is not "
                "in this file and does not exist here. The job was not imported."
            )
            continue

        fields = {key: entry.get(key) for key in JOB_FIELDS if key in entry}
        fields["name"] = name
        fields["source_connection_id"] = source.id
        fields["dest_connection_id"] = dest.id
        fields.setdefault("filters", {})

        job = Job(**fields)
        for preset_name in entry.get("filter_presets") or []:
            preset = presets.get(str(preset_name))
            if preset is None:
                report.warnings.append(
                    f"Job '{name}' uses the filter preset '{preset_name}', which is "
                    "not in this file. The job was imported without it, so it will "
                    "sync files that preset would have excluded."
                )
                continue
            job.filter_presets.append(preset)

        session.add(job)
        existing.add(name)
        report.jobs_created += 1


def _import_preferences(session: Session, document: dict[str, Any], report: ImportReport) -> None:
    incoming = document.get("preferences")
    if not isinstance(incoming, dict) or not incoming:
        return
    current = preferences_store.load(session).model_dump()
    # The excluded keys are deployment specific and were never exported, so the
    # local values are kept rather than reset to defaults.
    current.update(
        {key: value for key, value in incoming.items() if key not in PREFERENCE_EXCLUDES}
    )
    try:
        preferences_store.save(session, Preferences.model_validate(current))
    except ValueError as exc:
        report.warnings.append(f"The preferences in this file were not applied: {exc}")
        return
    report.preferences_applied = True

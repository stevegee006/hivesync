"""Server rendered pages. Jinja2 templates, no build step.

These routes are a thin layer over the API modules: they parse form input, call
the same functions the JSON endpoints call, and render. Business logic belongs in
app.probe and app.capabilities, not here.

Every page works without JavaScript. HTMX enhances the test and browse panels,
Alpine hides irrelevant form fields, and both degrade to a plain form post.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__, capabilities, probe, security
from app.api.auth import safe_redirect_target
from app.api.connections import capability_summary, to_read
from app.api.jobs import _apply as apply_job
from app.api.jobs import describe
from app.binaries import BinaryReport
from app.crypto import SecretBox
from app.db import get_session
from app.engines import inspect, rclone, rcloneconf
from app.engines.rcloneconf import RemoteConfigError
from app.jobs import archive, cron, planner
from app.models import (
    ArchiveLayout,
    CompareMode,
    ConflictResolve,
    Connection,
    ConnectionType,
    Credential,
    CredentialKind,
    DeleteMode,
    Direction,
    FilterPreset,
    Job,
    JobRun,
    JobRunChange,
    RcloneMode,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.schemas.connection import ConnectionCreate
from app.schemas.credential import CredentialCreate
from app.schemas.job import JobCreate, JobFilters

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter(include_in_schema=False)

_LOGIN_ERRORS = {
    "invalid": "That username and password combination is not valid.",
    "missing": "Enter both a username and a password.",
}

_PASSWORD_ERRORS = {
    "missing": "Enter your current password and a new one.",
    "wrong": "The current password is not correct.",
    "weak": f"The new password must be at least {security.MIN_PASSWORD_LENGTH} characters long.",
    "same": "The new password must differ from your current one.",
}


def _base_context(request: Request) -> dict[str, Any]:
    report: BinaryReport = request.app.state.binaries
    return {
        "request": request,
        "app_version": __version__,
        "auth_mode": request.app.state.settings.auth_mode,
        "rclone_version": report.rclone.version,
        "lftp_version": report.lftp.version,
        "min_password_length": security.MIN_PASSWORD_LENGTH,
    }


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secrets
    return box


# --------------------------------------------------------------------------
# Auth pages
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    session: Session = Depends(get_session),
) -> Any:
    if security.current_user(request, session) is not None:
        return RedirectResponse(url="/", status_code=303)
    context = _base_context(request)
    context["error"] = _LOGIN_ERRORS.get(error or "")
    context["next"] = safe_redirect_target(next)
    return templates.TemplateResponse(request, "login.html", context)


@router.get("/account/password", response_class=HTMLResponse)
def password_page(
    request: Request, error: str | None = None, session: Session = Depends(get_session)
) -> Any:
    user = security.require_user(request, session)
    context = _base_context(request)
    context["user"] = user
    context["error"] = _PASSWORD_ERRORS.get(error or "")
    context["forced"] = user.must_change_password
    return templates.TemplateResponse(request, "account_password.html", context)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> Any:
    user = security.require_user(request, session)
    if user.must_change_password:
        return RedirectResponse(url="/account/password", status_code=303)
    context = _base_context(request)
    context["user"] = user
    context["connection_count"] = session.scalar(
        select(Connection).with_only_columns(Connection.id).limit(1)
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


def _page_context(request: Request, session: Session) -> dict[str, Any]:
    context = _base_context(request)
    context["user"] = security.require_user(request, session)
    return context


def _extra_opts_text(connection: Connection | None) -> str:
    if connection is None or not connection.extra_opts:
        return ""
    return "\n".join(f"{key} = {value}" for key, value in connection.extra_opts.items())


def _parse_extra_opts(raw: str) -> dict[str, str]:
    """Parse the advanced options textarea, one `key = value` per line."""
    options: dict[str, str] = {}
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(
                f"Advanced options must be 'key = value' per line. Could not read: {stripped}"
            )
        key, _, value = stripped.partition("=")
        options[key.strip()] = value.strip()
    return options


def _form_context(
    request: Request,
    session: Session,
    connection: Connection | None,
    *,
    error: str | None = None,
    test: Any = None,
) -> dict[str, Any]:
    context = _page_context(request, session)
    context["connection"] = connection
    context["error"] = error
    context["test"] = test
    context["extra_opts_text"] = _extra_opts_text(connection)
    context["credentials"] = session.scalars(select(Credential).order_by(Credential.name)).all()

    # The backend list and imported remote list both come from rclone at runtime.
    # Neither is fatal if unavailable: the form still works, it just loses a
    # convenience, so failures become a note rather than an error page.
    context["backends"] = []
    context["imported_remotes"] = []
    context["imported_remotes_error"] = None
    try:
        context["backends"] = [
            str(provider.get("Name", "")) for provider in inspect.list_backends()
        ]
        context["backends"].sort()
    except inspect.InspectError as exc:
        logger.warning("Could not list rclone backends", extra={"error": str(exc)})
    try:
        settings = request.app.state.settings
        context["imported_remotes"] = inspect.list_imported_remotes(settings.user_rclone_conf)
    except inspect.InspectError as exc:
        context["imported_remotes_error"] = str(exc)
    return context


@router.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, session: Session = Depends(get_session)) -> Any:
    context = _page_context(request, session)
    connections = session.scalars(select(Connection).order_by(Connection.name)).all()
    context["connections"] = [to_read(connection, request) for connection in connections]
    return templates.TemplateResponse(request, "connections/list.html", context)


@router.get("/connections/new", response_class=HTMLResponse)
def new_connection_page(request: Request, session: Session = Depends(get_session)) -> Any:
    return templates.TemplateResponse(
        request, "connections/form.html", _form_context(request, session, None)
    )


@router.get("/connections/{connection_id}", response_class=HTMLResponse)
def edit_connection_page(
    connection_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    connection = session.get(Connection, connection_id)
    if connection is None:
        return RedirectResponse(url="/connections", status_code=303)
    return templates.TemplateResponse(
        request, "connections/form.html", _form_context(request, session, connection)
    )


def _connection_payload(form: dict[str, str]) -> ConnectionCreate:
    def clean(name: str) -> str | None:
        value = (form.get(name) or "").strip()
        return value or None

    port_raw = clean("port")
    return ConnectionCreate(
        name=(form.get("name") or "").strip(),
        type=ConnectionType(form.get("type") or "sftp"),
        host=clean("host"),
        port=int(port_raw) if port_raw else None,
        share=clean("share"),
        base_path=(form.get("base_path") or "").strip(),
        username=clean("username"),
        credential_id=int(form["credential_id"]) if clean("credential_id") else None,
        extra_opts=_parse_extra_opts(form.get("extra_opts") or ""),
        rclone_mode=RcloneMode(form["rclone_mode"]) if clean("rclone_mode") else None,
        rclone_remote_name=clean("rclone_remote_name"),
        rclone_backend_type=clean("rclone_backend_type"),
        sentinel_file=clean("sentinel_file"),
    )


def _describe_validation_error(exc: ValidationError) -> str:
    """Turn pydantic's structure into one readable sentence.

    Users see this, so the field path and the 'Value error,' prefix have to go.
    """
    messages = []
    for error in exc.errors():
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        messages.append(message)
    return " ".join(messages) or "That connection could not be saved."


@router.post("/connections", response_class=HTMLResponse)
async def create_connection_form(request: Request, session: Session = Depends(get_session)) -> Any:
    security.require_user(request, session)
    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        payload = _connection_payload(form)
    except (ValidationError, ValueError) as exc:
        error = _describe_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        context = _form_context(request, session, None, error=error)
        return templates.TemplateResponse(
            request, "connections/form.html", context, status_code=400
        )

    connection = Connection()
    for field in payload.model_fields_set | set(payload.__class__.model_fields):
        if hasattr(connection, field):
            setattr(connection, field, getattr(payload, field))
    session.add(connection)
    session.commit()
    return RedirectResponse(url=f"/connections/{connection.id}", status_code=303)


@router.post("/connections/{connection_id}", response_class=HTMLResponse)
async def update_connection_form(
    connection_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    security.require_user(request, session)
    connection = session.get(Connection, connection_id)
    if connection is None:
        return RedirectResponse(url="/connections", status_code=303)

    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        payload = _connection_payload(form)
    except (ValidationError, ValueError) as exc:
        error = _describe_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        context = _form_context(request, session, connection, error=error)
        return templates.TemplateResponse(
            request, "connections/form.html", context, status_code=400
        )

    before = (connection.type, connection.host, connection.port, connection.share)
    for field in payload.__class__.model_fields:
        if hasattr(connection, field):
            setattr(connection, field, getattr(payload, field))
    after = (connection.type, connection.host, connection.port, connection.share)
    if before != after:
        # SPEC 5.4: re-probe on edit. A stale probe that permits an option is
        # worse than no probe at all.
        connection.capabilities = None
        connection.capabilities_probed_at = None
        connection.last_test_ok = None
        connection.last_test_error = None

    session.commit()
    return RedirectResponse(url=f"/connections/{connection.id}", status_code=303)


@router.post("/connections/{connection_id}/test-partial", response_class=HTMLResponse)
def test_connection_partial(
    connection_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    security.require_user(request, session)
    connection = session.get(Connection, connection_id)
    if connection is None:
        return HTMLResponse("", status_code=404)

    outcome = probe.test_connection(
        connection,
        box=_box(request),
        settings=request.app.state.settings,
        session=session,
    )
    context = _base_context(request)
    context["connection"] = connection
    context["test"] = {
        "ok": outcome.ok,
        "message": outcome.message,
        "warnings": outcome.warnings,
        "command": outcome.command,
        "capabilities": capability_summary(connection),
        "host_key": (
            {
                "fingerprint": outcome.host_key.fingerprint,
                "fingerprints": outcome.host_key.fingerprints,
                "prompt": outcome.host_key.prompt,
            }
            if outcome.host_key
            else None
        ),
    }
    return templates.TemplateResponse(request, "connections/_test_result.html", context)


@router.post("/connections/{connection_id}/trust-host-key", response_class=HTMLResponse)
async def trust_host_key_form(
    connection_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    security.require_user(request, session)
    connection = session.get(Connection, connection_id)
    if connection is None:
        return RedirectResponse(url="/connections", status_code=303)
    form = await request.form()
    fingerprint = str(form.get("fingerprint") or "")
    try:
        probe.trust_host_key(connection, fingerprint, session=session)
    except RemoteConfigError as exc:
        context = _form_context(request, session, connection, error=str(exc))
        return templates.TemplateResponse(
            request, "connections/form.html", context, status_code=409
        )
    return RedirectResponse(url=f"/connections/{connection.id}", status_code=303)


@router.post("/connections/{connection_id}/forget-host-key", response_class=HTMLResponse)
def forget_host_key_form(
    connection_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    security.require_user(request, session)
    connection = session.get(Connection, connection_id)
    if connection is not None:
        connection.host_keys = None
        connection.host_keys_trusted = False
        session.commit()
    return RedirectResponse(url=f"/connections/{connection_id}", status_code=303)


@router.get("/connections/{connection_id}/browse-partial", response_class=HTMLResponse)
def browse_partial(
    connection_id: int,
    request: Request,
    path: str | None = None,
    session: Session = Depends(get_session),
) -> Any:
    security.require_user(request, session)
    connection = session.get(Connection, connection_id)
    if connection is None:
        return HTMLResponse("", status_code=404)

    context = _base_context(request)
    context["connection"] = connection
    current = (path or "").strip("/")
    parent = "/".join(current.split("/")[:-1]) if current else None

    try:
        entries = probe.browse(
            connection, path, box=_box(request), settings=request.app.state.settings
        )
        context["error"] = None
    except (RemoteConfigError, inspect.InspectError) as exc:
        entries = []
        context["error"] = str(exc)

    context["browse"] = {
        "path": current,
        "parent": parent,
        "entries": [
            {
                "name": entry.name,
                "is_dir": entry.is_dir,
                "path": f"{current}/{entry.name}" if current else entry.name,
            }
            for entry in entries
        ],
    }
    return templates.TemplateResponse(request, "connections/_browse.html", context)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


@router.get("/credentials", response_class=HTMLResponse)
def credentials_page(
    request: Request, error: str | None = None, session: Session = Depends(get_session)
) -> Any:
    context = _page_context(request, session)
    credentials = session.scalars(select(Credential).order_by(Credential.name)).all()
    rows = []
    for credential in credentials:
        used_by = list(
            session.scalars(
                select(Connection.name).where(Connection.credential_id == credential.id)
            )
        )
        rows.append(
            {
                "id": credential.id,
                "name": credential.name,
                "kind": credential.kind,
                "is_obscured": credential.is_obscured,
                "used_by": used_by,
            }
        )
    context["credentials"] = rows
    context["error"] = error
    return templates.TemplateResponse(request, "credentials/list.html", context)


@router.post("/credentials", response_class=HTMLResponse)
async def create_credential_form(request: Request, session: Session = Depends(get_session)) -> Any:
    security.require_user(request, session)
    form = await request.form()
    try:
        payload = CredentialCreate(
            name=str(form.get("name") or "").strip(),
            kind=CredentialKind(str(form.get("kind") or "password")),
            secret=str(form.get("secret") or ""),
            key_passphrase=(str(form.get("key_passphrase")) or None)
            if form.get("key_passphrase")
            else None,
            is_obscured=str(form.get("is_obscured") or "") == "true",
        )
    except (ValidationError, ValueError) as exc:
        error = _describe_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        return RedirectResponse(url=f"/credentials?error={_slug(error)}", status_code=303)

    box = _box(request)
    session.add(
        Credential(
            name=payload.name,
            kind=payload.kind,
            secret_ciphertext=box.encrypt(payload.secret),
            key_passphrase_ciphertext=(
                box.encrypt(payload.key_passphrase) if payload.key_passphrase else None
            ),
            is_obscured=payload.is_obscured,
        )
    )
    session.commit()
    logger.info("Created credential", extra={"credential": payload.name})
    return RedirectResponse(url="/credentials", status_code=303)


def _slug(message: str) -> str:
    """Collapse a message into a short query-safe token.

    The page maps a handful of codes to copy; anything else becomes 'invalid', so
    nothing user supplied is reflected into the rendered page.
    """
    return "invalid" if message else ""


@router.post("/credentials/{credential_id}/delete", response_class=HTMLResponse)
def delete_credential_form(
    credential_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    security.require_user(request, session)
    credential = session.get(Credential, credential_id)
    if credential is None:
        return RedirectResponse(url="/credentials", status_code=303)
    in_use = session.scalars(
        select(Connection.name).where(Connection.credential_id == credential_id)
    ).all()
    if in_use:
        return RedirectResponse(url="/credentials?error=invalid", status_code=303)
    session.delete(credential)
    session.commit()
    return RedirectResponse(url="/credentials", status_code=303)


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------


@router.get("/compatibility", response_class=HTMLResponse)
def compatibility_page(
    request: Request,
    source_id: int | None = None,
    dest_id: int | None = None,
    session: Session = Depends(get_session),
) -> Any:
    """The capability intersection of two endpoints, with reasons.

    Stands in for the job editor until it exists, so the constraint logic and its
    explanations are exercised from M1 rather than shipped unseen. See CLAUDE.md
    on acceptance criterion four.
    """
    context = _page_context(request, session)
    connections = list(session.scalars(select(Connection).order_by(Connection.name)))
    context["connections"] = connections
    context["result"] = None
    context["source"] = None
    context["dest"] = None

    if len(connections) < 2:
        return templates.TemplateResponse(request, "compatibility.html", context)

    source = session.get(Connection, source_id) if source_id else connections[0]
    dest = session.get(Connection, dest_id) if dest_id else connections[1]
    if source is None or dest is None:
        return templates.TemplateResponse(request, "compatibility.html", context)

    intersection = capabilities.intersect(source, dest)
    context["source"] = source
    context["dest"] = dest
    context["result"] = {
        "source": source.name,
        "dest": dest.name,
        "source_capabilities": capability_summary(source),
        "dest_capabilities": capability_summary(dest),
        "checksum": intersection.checksum,
        "bidirectional": intersection.bidirectional,
        "archive": intersection.archive,
        "empty_dirs": intersection.empty_dirs,
        "shared_hashes": sorted(intersection.shared_hashes),
        "warnings": list(intersection.warnings),
        "stale": intersection.stale,
    }
    return templates.TemplateResponse(request, "compatibility.html", context)


# --------------------------------------------------------------------------
# Jobs and runs
# --------------------------------------------------------------------------


def _job_form_context(
    request: Request,
    session: Session,
    job: Job | None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    context = _page_context(request, session)
    connections = list(session.scalars(select(Connection).order_by(Connection.name)))
    context["job"] = job
    context["error"] = error
    context["connections"] = connections
    context["presets"] = list(session.scalars(select(FilterPreset).order_by(FilterPreset.name)))
    context["selected_preset_ids"] = [preset.id for preset in job.filter_presets] if job else []
    context["exclude_text"] = "\n".join((job.filters or {}).get("exclude", []) or []) if job else ""
    context["description"] = describe(job) if job else None
    context["schedule_preview"] = cron.preview(job.schedule_cron, job.timezone) if job else None
    scheduler = getattr(request.app.state, "scheduler", None)
    context["next_run"] = (
        scheduler.next_run_time(job.id) if job and scheduler and scheduler.running else None
    )
    context["runs"] = planner.latest_runs(session, job.id) if job else []

    # The same intersection and the same reason strings the compatibility page
    # uses, so a disabled option explains itself inline. SPEC section 5.4.
    context["compat"] = None
    if job is not None and job.source_connection and job.dest_connection:
        context["compat"] = capabilities.intersect(job.source_connection, job.dest_connection)
    elif len(connections) >= 2:
        context["compat"] = capabilities.intersect(connections[0], connections[1])

    context["archive_preview"], context["archive_error"] = _archive_preview(job)
    return context


def _archive_preview(job: Job | None) -> tuple[archive.ArchivePlan | None, str | None]:
    """The resolved archive path, so it can be read before a run creates it.

    "beside the destination" is not something an operator can check against their
    own filesystem, and neither is the exclude this injects into their filters.
    Both are shown. A configuration that cannot work says so here rather than at
    two in the morning when the schedule fires.
    """
    if job is None or job.delete_mode != DeleteMode.archive:
        return None, None
    if not job.source_connection or not job.dest_connection:
        return None, None
    try:
        # The written side, which for a dest_to_source job is the connection
        # named source. Shown without the synthetic alias.
        _source, dest, _read, write = rclone.endpoints_and_paths(job)
        return archive.plan_for(job, rcloneconf.display_path(dest, write or None)), None
    except (archive.ArchiveError, RemoteConfigError) as exc:
        return None, str(exc)


def _job_payload(form: dict[str, str], preset_ids: list[int]) -> JobCreate:
    def number(name: str) -> int | None:
        raw = (form.get(name) or "").strip()
        return int(raw) if raw else None

    excludes = [
        line.strip() for line in (form.get("filters_exclude") or "").splitlines() if line.strip()
    ]
    delete_mode = DeleteMode(form.get("delete_mode") or "none")
    # The archive inputs stay in the form when hidden, so a job switched away
    # from archiving would otherwise submit a stale path and be refused for
    # setting one it is no longer using.
    archive_base = (form.get("archive_base") or "").strip() or None
    if delete_mode != DeleteMode.archive:
        archive_base = None
    return JobCreate(
        name=(form.get("name") or "").strip(),
        source_connection_id=int(form["source_connection_id"]),
        source_path=(form.get("source_path") or "").strip(),
        dest_connection_id=int(form["dest_connection_id"]),
        dest_path=(form.get("dest_path") or "").strip(),
        direction=Direction(form.get("direction") or "source_to_dest"),
        compare_mode=CompareMode(form.get("compare_mode") or "mtime_size"),
        modify_window=(form.get("modify_window") or "1s").strip(),
        delete_mode=delete_mode,
        archive_base=archive_base,
        archive_layout=ArchiveLayout(form.get("archive_layout") or "timestamped_dir"),
        # These two were absent from the form until now, so every web edit of a
        # bidirectional job silently reset its conflict policy to the default.
        conflict_resolve=ConflictResolve(form.get("conflict_resolve") or "newer"),
        check_access=form.get("check_access") == "true",
        max_delete_pct=number("max_delete_pct") or 0,
        transfers=number("transfers"),
        checkers=number("checkers"),
        bwlimit=(form.get("bwlimit") or "").strip() or None,
        filters=JobFilters(exclude=excludes),
        filter_preset_ids=preset_ids,
        schedule_cron=(form.get("schedule_cron") or "").strip() or None,
        timezone=(form.get("timezone") or "UTC").strip() or "UTC",
        # An unchecked checkbox is simply absent from the form.
        enabled=form.get("enabled") == "true",
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: Session = Depends(get_session)) -> Any:
    context = _page_context(request, session)
    jobs = list(session.scalars(select(Job).order_by(Job.name)))
    context["jobs"] = [
        {
            "job": job,
            "description": describe(job),
            "last_run": next(iter(planner.latest_runs(session, job.id, limit=1)), None),
        }
        for job in jobs
    ]
    return templates.TemplateResponse(request, "jobs/list.html", context)


@router.get("/jobs/new", response_class=HTMLResponse)
def new_job_page(request: Request, session: Session = Depends(get_session)) -> Any:
    return templates.TemplateResponse(
        request, "jobs/form.html", _job_form_context(request, session, None)
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def edit_job_page(job_id: int, request: Request, session: Session = Depends(get_session)) -> Any:
    job = session.get(Job, job_id)
    if job is None:
        return RedirectResponse(url="/jobs", status_code=303)
    return templates.TemplateResponse(
        request, "jobs/form.html", _job_form_context(request, session, job)
    )


async def _save_job(request: Request, session: Session, job: Job | None) -> Any:
    security.require_user(request, session)
    form_data = await request.form()
    form = {key: str(value) for key, value in form_data.items()}
    preset_ids = [
        int(value)
        for value in form_data.getlist("filter_preset_ids")
        if isinstance(value, str) and value.isdigit()
    ]

    try:
        payload = _job_payload(form, preset_ids)
    except (ValidationError, ValueError, KeyError) as exc:
        message = (
            _describe_validation_error(exc)
            if isinstance(exc, ValidationError)
            else "Every field needs a value before this job can be saved."
        )
        context = _job_form_context(request, session, job, error=message)
        return templates.TemplateResponse(request, "jobs/form.html", context, status_code=400)

    target = job or Job()
    try:
        apply_job(session, target, payload)
    except HTTPException as exc:
        context = _job_form_context(request, session, job, error=str(exc.detail))
        return templates.TemplateResponse(request, "jobs/form.html", context, status_code=400)

    if job is None:
        session.add(target)
    session.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and scheduler.running:
        scheduler.reload()
    return RedirectResponse(url=f"/jobs/{target.id}", status_code=303)


@router.post("/jobs", response_class=HTMLResponse)
async def create_job_form(request: Request, session: Session = Depends(get_session)) -> Any:
    return await _save_job(request, session, None)


@router.post("/jobs/{job_id}", response_class=HTMLResponse)
async def update_job_form(
    job_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    job = session.get(Job, job_id)
    if job is None:
        return RedirectResponse(url="/jobs", status_code=303)
    return await _save_job(request, session, job)


@router.post("/jobs/{job_id}/run", response_class=HTMLResponse)
def run_job_form(job_id: int, request: Request, session: Session = Depends(get_session)) -> Any:
    security.require_user(request, session)
    job = session.get(Job, job_id)
    if job is None:
        return RedirectResponse(url="/jobs", status_code=303)
    try:
        run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    except planner.RunConflict:
        # Already running. Show the run in flight rather than an error: it is
        # what the operator wanted to look at anyway.
        existing = next(iter(planner.latest_runs(session, job.id, limit=1)), None)
        destination = f"/runs/{existing.id}" if existing else f"/jobs/{job.id}"
        return RedirectResponse(url=destination, status_code=303)

    request.app.state.plan_runner.submit(run.id)
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@router.post("/jobs/{job_id}/run-live", response_class=HTMLResponse)
def run_job_live_form(
    job_id: int, request: Request, session: Session = Depends(get_session)
) -> Any:
    """Start a live run. It plans first and refuses before changing anything if
    the delete brake would be exceeded. See jobs.runner."""
    security.require_user(request, session)
    job = session.get(Job, job_id)
    if job is None:
        return RedirectResponse(url="/jobs", status_code=303)
    try:
        run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    except planner.RunConflict:
        existing = next(iter(planner.latest_runs(session, job.id, limit=1)), None)
        destination = f"/runs/{existing.id}" if existing else f"/jobs/{job.id}"
        return RedirectResponse(url=destination, status_code=303)

    request.app.state.live_runner.submit(run.id)
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@router.post("/jobs/{job_id}/resync", response_class=HTMLResponse)
def resync_job_form(job_id: int, request: Request, session: Session = Depends(get_session)) -> Any:
    """Explicit first sync. Never automatic: a resync makes one side match the
    other for any file that differs. SPEC section 10.1."""
    security.require_user(request, session)
    job = session.get(Job, job_id)
    if job is None or job.direction != Direction.bidirectional:
        return RedirectResponse(url="/jobs", status_code=303)
    try:
        run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    except planner.RunConflict:
        existing = next(iter(planner.latest_runs(session, job.id, limit=1)), None)
        return RedirectResponse(
            url=f"/runs/{existing.id}" if existing else f"/jobs/{job.id}", status_code=303
        )
    run.is_resync = True
    session.commit()
    request.app.state.live_runner.submit(run.id)
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
def cancel_run_form(run_id: int, request: Request, session: Session = Depends(get_session)) -> Any:
    security.require_user(request, session)
    run = session.get(JobRun, run_id)
    if run is None:
        return RedirectResponse(url="/jobs", status_code=303)
    if run.status in (RunStatus.queued, RunStatus.running):
        run.status = RunStatus.cancelled
        session.commit()
        request.app.state.live_runner.cancel(run_id)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(
    run_id: int,
    request: Request,
    action: str | None = None,
    session: Session = Depends(get_session),
) -> Any:
    context = _page_context(request, session)
    run = session.get(JobRun, run_id)
    if run is None:
        return RedirectResponse(url="/jobs", status_code=303)

    query = select(JobRunChange).where(JobRunChange.run_id == run_id)
    if action:
        query = query.where(JobRunChange.action == action)
    changes = list(
        session.scalars(query.order_by(JobRunChange.action, JobRunChange.path).limit(1000))
    )

    context["run"] = run
    context["job"] = session.get(Job, run.job_id)
    context["changes"] = changes
    context["active_action"] = action or ""
    return templates.TemplateResponse(request, "runs/detail.html", context)

"""Connection testing and browsing.

Kept out of the API layer so the orchestration is testable without HTTP, and out
of engines/ because it is policy rather than plumbing.

A test is: reachability, then the capability probe, then the guards that only
apply to some connection types. SPEC 5.5.

Host key handling implements SPEC section 15's trust on first use. rclone
validates host keys only when known_hosts_file is set, so nothing is verified
until a key is pinned. The flow is therefore:

1. First test of an SFTP connection scans the host key and returns it for
   approval. The test does not pass, and nothing is stored.
2. The operator approves a specific fingerprint, which pins it.
3. Later tests and runs pass known_hosts, so a changed key fails loudly.

Refusing to pass at step 1 is the whole point. A test that goes green while
trusting anything trains people to click through the one prompt that matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import capabilities
from app.config import Settings
from app.crypto import SecretBox
from app.engines import inspect, rcloneconf
from app.engines.rcloneconf import ALIAS_PROBE, RemoteConfigError
from app.models import Connection, ConnectionType, utcnow

logger = logging.getLogger(__name__)


@dataclass
class HostKeyPrompt:
    fingerprint: str
    key_line: str
    prompt: str


@dataclass
class TestOutcome:
    ok: bool
    message: str
    probe_payload: dict[str, object] | None = None
    host_key: HostKeyPrompt | None = None
    warnings: list[str] = field(default_factory=list)
    command: str | None = None


def _needs_host_key_trust(connection: Connection) -> bool:
    return (
        connection.type == ConnectionType.sftp
        and not (connection.host_key_fingerprint or "").strip()
    )


def _host_key_prompt(connection: Connection) -> HostKeyPrompt | None:
    scanned = inspect.scan_host_key(connection.host or "", connection.port or 22)
    if scanned is None:
        return None
    key_line, fingerprint = scanned
    return HostKeyPrompt(
        fingerprint=fingerprint,
        key_line=key_line,
        prompt=(
            f"The host key for {connection.host} has not been seen before. Confirm "
            "this fingerprint matches the server before trusting it, by running "
            "`ssh-keyscan -p "
            f"{connection.port or 22} {connection.host}` on a machine you trust, or "
            "by checking it against the server's own records."
        ),
    )


def test_connection(
    connection: Connection,
    *,
    box: SecretBox,
    settings: Settings,
    session: Session | None = None,
) -> TestOutcome:
    """Test reachability and probe capabilities. Records the result on the model.

    Never raises for an ordinary failure: an unreachable host is a result the
    operator needs to read, not an exception.
    """
    outcome = _run_test(connection, box=box, settings=settings)

    connection.last_test_at = utcnow()
    connection.last_test_ok = outcome.ok
    connection.last_test_error = None if outcome.ok else outcome.message

    if outcome.probe_payload is not None:
        connection.capabilities = outcome.probe_payload
        connection.capabilities_probed_at = utcnow()

    if session is not None:
        session.commit()

    return outcome


def _run_test(connection: Connection, *, box: SecretBox, settings: Settings) -> TestOutcome:
    # Host key first: for SFTP there is no point testing anything else while the
    # connection would be trusting whatever answers.
    if _needs_host_key_trust(connection):
        prompt = _host_key_prompt(connection)
        if prompt is None:
            return TestOutcome(
                ok=False,
                message=(
                    f"Could not read an SSH host key from {connection.host} on port "
                    f"{connection.port or 22}. Check the host, the port, and that "
                    "the server is reachable."
                ),
            )
        return TestOutcome(
            ok=False,
            message=(
                "This host's SSH key is not trusted yet. Review the fingerprint and "
                "approve it to finish testing."
            ),
            host_key=prompt,
        )

    try:
        with rcloneconf.prepare({ALIAS_PROBE: connection}, box=box, settings=settings) as prepared:
            result = inspect.check_reachable(prepared, ALIAS_PROBE)
            command = result.command_line

            if not result.ok:
                return TestOutcome(
                    ok=False,
                    message=_interpret_failure(connection, result.failure_summary()),
                    command=command,
                )

            warnings: list[str] = []

            # Stale mount guard. SPEC 6.4: a dead cifs or NFS mount presents as an
            # empty directory, which is exactly what causes a mass delete.
            sentinel = (connection.sentinel_file or "").strip()
            if sentinel and connection.type == ConnectionType.local:
                entries = inspect.list_dirs(prepared, ALIAS_PROBE)
                if not any(entry.name == sentinel for entry in entries):
                    return TestOutcome(
                        ok=False,
                        message=(
                            f"The sentinel file '{sentinel}' was not found at the base "
                            "path. The mount is probably stale or not mounted. Syncing "
                            "now could look like every file was deleted."
                        ),
                        command=command,
                    )

            try:
                payload = inspect.probe_features(prepared, ALIAS_PROBE)
            except inspect.InspectError as exc:
                # Reachable but unprobeable is a partial success worth reporting as
                # such: the endpoint works, the job editor just cannot constrain
                # itself yet.
                return TestOutcome(
                    ok=False,
                    message=(
                        "The connection worked but the capability probe failed, so "
                        f"job options cannot be checked against it. {exc}"
                    ),
                    command=command,
                )

            caps = capabilities.from_probe(payload, utcnow())
            if not caps.hashes:
                warnings.append(
                    f"'{connection.name}' exposes no hash types, so checksum "
                    "comparison will not be available on jobs that use it."
                )
            if not caps.can_set_modtime:
                warnings.append(
                    f"'{connection.name}' cannot write modification times, so "
                    "bidirectional sync will not be available."
                )
            if not caps.supports_move:
                warnings.append(
                    f"'{connection.name}' does not support server-side move, so "
                    "deletion archiving here costs a full download and upload."
                )

            return TestOutcome(
                ok=True,
                message="Connected successfully.",
                probe_payload=payload,
                warnings=warnings,
                command=command,
            )
    except RemoteConfigError as exc:
        return TestOutcome(ok=False, message=str(exc))
    except inspect.InspectError as exc:
        return TestOutcome(ok=False, message=str(exc))


def _interpret_failure(connection: Connection, summary: str) -> str:
    """Turn an rclone diagnostic into something actionable.

    A changed SSH host key is the case worth special handling: it is either a
    rebuilt server or an interception attempt, and the raw message does not say
    what to do about either.
    """
    lowered = summary.lower()
    if "host key" in lowered or "knownhosts" in lowered:
        return (
            f"The SSH host key for {connection.host} does not match the one pinned "
            "for this connection. Either the server was rebuilt, or the connection "
            "is being intercepted. Confirm the new key out of band, then clear and "
            f"re-approve the host key. Original error: {summary}"
        )
    if "permission denied" in lowered or "auth" in lowered:
        return (
            f"Authentication was refused. Check the username and credential. "
            f"Original error: {summary}"
        )
    if "no such host" in lowered or "lookup" in lowered:
        return f"The host name could not be resolved. Original error: {summary}"
    if "connection refused" in lowered:
        return (
            f"The connection was refused. Check the port and that the service is "
            f"running. Original error: {summary}"
        )
    if "directory not found" in lowered or "not found" in lowered:
        return (
            "Connected, but the base path does not exist. Check the path, and for "
            f"SMB check the share name. Original error: {summary}"
        )
    return summary


def trust_host_key(
    connection: Connection,
    fingerprint: str,
    *,
    session: Session | None = None,
) -> None:
    """Pin a host key after explicit approval.

    Re-scans and requires the presented fingerprint to still match, so approving
    cannot pin a different key than the one that was reviewed.
    """
    scanned = inspect.scan_host_key(connection.host or "", connection.port or 22)
    if scanned is None:
        raise RemoteConfigError(
            f"Could not read an SSH host key from {connection.host} to confirm it. "
            "Check the host is reachable and try again."
        )
    key_line, scanned_fingerprint = scanned
    if scanned_fingerprint != fingerprint:
        raise RemoteConfigError(
            "The host key changed between being shown and being approved, so "
            "nothing was trusted. Review the new fingerprint and try again."
        )
    # Store the key line, which is what a known_hosts file needs. The fingerprint
    # is derived from it for display.
    connection.host_key_fingerprint = key_line
    if session is not None:
        session.commit()
    logger.info(
        "Pinned SSH host key",
        extra={"connection": connection.name, "fingerprint": fingerprint},
    )


def browse(
    connection: Connection,
    subpath: str | None,
    *,
    box: SecretBox,
    settings: Settings,
) -> list[inspect.DirEntry]:
    """List a directory under the connection's base path.

    Traversal above the base path is refused by join_subpath rather than
    normalised away, because a picker that can walk to / is a bug.
    """
    with rcloneconf.prepare({ALIAS_PROBE: connection}, box=box, settings=settings) as prepared:
        return inspect.list_dirs(prepared, ALIAS_PROBE, subpath)


def resolved_path(connection: Connection, *, box: SecretBox, settings: Settings) -> str | None:
    """The `remote:path` form for display. SPEC 13 wants this visible.

    Returns None when the connection is not currently resolvable, since a display
    string is never worth failing a page render for.
    """
    try:
        with rcloneconf.prepare({ALIAS_PROBE: connection}, box=box, settings=settings) as prepared:
            endpoint = prepared.endpoints[ALIAS_PROBE]
            # Show the user's own name rather than the synthetic alias.
            label = (
                endpoint.alias
                if connection.type == ConnectionType.rclone_remote
                and connection.rclone_mode is not None
                and endpoint.alias != ALIAS_PROBE
                else connection.name
            )
            return f"{label}:{endpoint.path}"
    except (RemoteConfigError, inspect.InspectError):
        return None

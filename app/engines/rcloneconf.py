"""Turning a Connection into something rclone can address.

Every remote is defined through environment variables of the form
`RCLONE_CONFIG_<NAME>_<KEY>`, verified working in rclone 1.74.4. Consequences,
all of them good:

- No credential is ever written to disk. SPEC section 5.3 describes a temp file
  fallback; it is not needed and is not implemented. Env vars, a read only
  `--config` for imported remotes, and `key_pem` for SSH keys cover every case.
- No credential ever appears in argv, so a stored command cannot leak one.

Secrets that rclone marks `IsPassword` must be obscured or rclone refuses them
outright. Obscuring goes through `rclone obscure -`, which reads the plaintext
from stdin, so the plaintext never reaches a command line either.

Remote names are synthetic, `hs_src` and `hs_dst`, never the user's connection
name. A user supplied name cannot then affect env var construction.

One file is written: a known_hosts file for SFTP host key validation, which
rclone enables only when `known_hosts_file` is set. It contains a public key, not
a secret, so it does not weaken the no-plaintext-on-disk guarantee.
"""

from __future__ import annotations

import configparser
import json
import logging
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.crypto import Redactor, SecretBox
from app.engines import process
from app.models import Connection, ConnectionType, Credential, CredentialKind, RcloneMode

logger = logging.getLogger(__name__)

ALIAS_SOURCE = "hs_src"
ALIAS_DEST = "hs_dst"
ALIAS_PROBE = "hs_probe"

RCLONE = "rclone"


class RemoteConfigError(Exception):
    """A connection cannot be turned into a usable rclone remote.

    The message is user facing: it says what is missing and what to do.
    """


def env_var_name(alias: str, key: str) -> str:
    return f"RCLONE_CONFIG_{alias.upper()}_{key.upper()}"


def obscure(value: str, *, redactor: Redactor | None = None) -> str:
    """Obscure a secret using rclone itself, passing the plaintext on stdin.

    Deliberately not reimplemented in Python. rclone's obscure format is an
    AES-CTR wrapper around a key baked into its source, and reproducing that
    would be inventing an output format, which CLAUDE.md rule 3 forbids.
    """
    result = process.run(
        [RCLONE, "obscure", "-"],
        stdin_text=value,
        redactor=redactor,
        timeout_seconds=15,
    )
    if not result.ok:
        raise RemoteConfigError(
            f"Could not prepare a credential for rclone. {result.failure_summary()}"
        )
    obscured = result.stdout.strip()
    if not obscured:
        raise RemoteConfigError("rclone obscure returned nothing for a credential.")
    return obscured


@dataclass(frozen=True)
class SecretValues:
    """Decrypted credential material for one connection, plus redaction inputs."""

    # rclone option name to already-obscured-or-raw value, ready for an env var.
    options: dict[str, str] = field(default_factory=dict)
    # Every plaintext and obscured form seen, for the Redactor. An obscured value
    # is still a credential: rclone reveal undoes it trivially.
    redactable: tuple[str, ...] = ()


def _decrypt_credential(credential: Credential, box: SecretBox, redactor: Redactor) -> SecretValues:
    """Map a stored credential onto rclone option values.

    Values marked IsPassword by rclone are obscured here unless they arrived
    already obscured. key_pem is passed raw: rclone marks it Sensitive but not
    IsPassword, so obscuring it would corrupt the key.
    """
    plaintext = box.decrypt(credential.secret_ciphertext)
    options: dict[str, str] = {}
    redactable: list[str] = [plaintext]

    def prepare(value: str, *, already_obscured: bool) -> str:
        if already_obscured:
            redactable.append(value)
            return value
        result = obscure(value, redactor=redactor)
        redactable.extend([value, result])
        return result

    if credential.kind in (CredentialKind.password, CredentialKind.smb_ntlm):
        options["pass"] = prepare(plaintext, already_obscured=credential.is_obscured)

    elif credential.kind == CredentialKind.ssh_key:
        # Raw PEM, never obscured.
        options["key_pem"] = plaintext
        if credential.key_passphrase_ciphertext:
            passphrase = box.decrypt(credential.key_passphrase_ciphertext)
            options["key_file_pass"] = prepare(passphrase, already_obscured=credential.is_obscured)

    elif credential.kind == CredentialKind.backend_secret:
        try:
            payload = json.loads(plaintext)
        except ValueError as exc:
            raise RemoteConfigError(
                f"Credential '{credential.name}' is stored in a form this version "
                "cannot read. Delete it and enter it again."
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteConfigError(
                f"Credential '{credential.name}' is stored in a form this version "
                "cannot read. Delete it and enter it again."
            )
        for option_name, entry in payload.items():
            if isinstance(entry, dict):
                value = str(entry.get("value", ""))
                already = bool(entry.get("obscured", False))
            else:
                value = str(entry)
                already = False
            options[str(option_name)] = prepare(value, already_obscured=already)

    return SecretValues(options=options, redactable=tuple(redactable))


def _base_path_for(connection: Connection) -> str:
    """The path portion that follows `remote:`.

    SMB is the special case: rclone addresses it as `remote:Share/sub/path`, so
    the share is the first path element rather than a backend option. SPEC 5.1.
    """
    base = (connection.base_path or "").strip("/")
    if connection.type == ConnectionType.smb:
        share = (connection.share or "").strip("/")
        if not share:
            raise RemoteConfigError(
                f"Connection '{connection.name}' is SMB but has no share set. "
                "rclone addresses SMB as remote:Share/path, so the share is required."
            )
        return f"{share}/{base}" if base else share
    if connection.type == ConnectionType.local:
        # Local paths are absolute and must not be stripped.
        return connection.base_path or "/"
    return base


def display_path(connection: Connection, subpath: str | None = None) -> str:
    """The endpoint path as an operator would recognise it.

    Without the synthetic `hs_src:`/`hs_dst:` alias, which is an implementation
    detail of a single run and means nothing to the person reading the screen.
    Suitable for showing a resolved path, not for handing to rclone.
    """
    return join_subpath(_base_path_for(connection), subpath)


def join_subpath(base: str, subpath: str | None) -> str:
    """Join a browse or job subpath onto a base path, refusing to escape it.

    A directory picker that can walk out of its base path is a bug, so traversal
    is rejected rather than normalised away silently.
    """
    if not subpath:
        return base
    candidate = subpath.replace("\\", "/").strip("/")
    if not candidate:
        return base
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise RemoteConfigError(
                "That path is not allowed: it points outside the configured base path."
            )
        parts.append(part)
    if not parts:
        return base
    suffix = "/".join(parts)
    if not base:
        return suffix
    return f"{base.rstrip('/')}/{suffix}"


@dataclass
class Endpoint:
    """One resolved endpoint, addressable as `alias:path`."""

    alias: str
    path: str
    env: dict[str, str]
    redactable: tuple[str, ...]
    uses_user_config: bool

    def spec(self, subpath: str | None = None) -> str:
        return f"{self.alias}:{join_subpath(self.path, subpath)}"


def _native_env(connection: Connection, alias: str) -> dict[str, str]:
    """Env vars for the non-secret options of a native connection type."""
    env: dict[str, str] = {}

    def put(key: str, value: object) -> None:
        if value is None or value == "":
            return
        env[env_var_name(alias, key)] = str(value)

    if connection.type == ConnectionType.local:
        put("type", "local")
        return env

    if connection.type == ConnectionType.sftp:
        put("type", "sftp")
    elif connection.type in (ConnectionType.ftp, ConnectionType.ftps):
        put("type", "ftp")
        if connection.type == ConnectionType.ftps:
            # Explicit FTPS, AUTH TLS on the control port, which is what "FTPS"
            # means in almost every deployment. Implicit FTPS is reachable by
            # setting tls=true through extra_opts.
            put("explicit_tls", "true")
    elif connection.type == ConnectionType.smb:
        put("type", "smb")
        # `or {}` because the column default only applies on insert: an instance
        # built but not yet flushed, or an older NULL row, has None here.
        put("domain", (connection.extra_opts or {}).get("domain") or "WORKGROUP")
    else:  # pragma: no cover - rclone_remote handled by the caller
        raise RemoteConfigError(f"Unsupported connection type: {connection.type}")

    if not connection.host:
        raise RemoteConfigError(
            f"Connection '{connection.name}' needs a host. Add one and test again."
        )
    put("host", connection.host)
    put("user", connection.username)
    put("port", connection.port)
    return env


def _extra_opts_env(connection: Connection, alias: str, env: dict[str, str]) -> None:
    """Overlay user supplied advanced options.

    Applied last so an operator can override a default this module chose, which
    is the whole point of an advanced options box.
    """
    for key, value in (connection.extra_opts or {}).items():
        if value is None or value == "":
            continue
        if key == "domain" and connection.type == ConnectionType.smb:
            continue  # already applied
        env[env_var_name(alias, str(key))] = str(value)


def known_hosts_content(connection: Connection) -> str | None:
    """The pinned host keys as a known_hosts file body, or None if none are pinned.

    Every stored key is written. The client negotiates one of them, so pinning
    only a single algorithm would break as soon as the server stopped offering it.
    """
    if not connection.host_keys_trusted:
        # Scanned but not approved. Writing them would validate against keys no
        # human has confirmed, which is the same as not validating at all.
        return None
    stored = (connection.host_keys or "").strip()
    if not stored:
        return None
    host = connection.host or ""
    port = connection.port or 22
    # known_hosts brackets the host when the port is not the default.
    target = f"[{host}]:{port}" if port != 22 else host
    lines = [f"{target} {entry.strip()}" for entry in stored.splitlines() if entry.strip()]
    if not lines:
        return None
    return "\n".join(lines) + "\n"


@dataclass
class Prepared:
    """Everything needed to invoke rclone for a set of endpoints."""

    endpoints: dict[str, Endpoint]
    env: dict[str, str]
    base_args: list[str]
    redactor: Redactor

    def argv(self, *args: str) -> list[str]:
        # --color NEVER, because bisync colours its output and ANSI escapes in a
        # stored log or a parsed error message help nobody. The flag is
        # `--color NEVER`; there is no `--no-color`, and passing one makes rclone
        # misparse the whole command.
        return [RCLONE, *self.base_args, "--color", "NEVER", *args]


@contextmanager
def prepare(
    connections: Mapping[str, Connection],
    *,
    box: SecretBox,
    settings: Settings,
) -> Iterator[Prepared]:
    """Resolve connections into an invocation context.

    `connections` maps an alias to a Connection. Use ALIAS_SOURCE, ALIAS_DEST or
    ALIAS_PROBE so the alias is never derived from user input.

    Cleans up the known_hosts files it creates. Those hold public keys only.
    """
    endpoints: dict[str, Endpoint] = {}
    merged_env: dict[str, str] = {}
    redactable: list[str] = []
    uses_user_config = False
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    # Built early with what we know, so obscure() calls are already redacted.
    redactor = Redactor([])

    try:
        for alias, connection in connections.items():
            if connection.type == ConnectionType.rclone_remote:
                endpoint = _resolve_rclone_remote(connection, alias, box, redactor, settings)
            else:
                endpoint = _resolve_native(connection, alias, box, redactor)

            if endpoint.uses_user_config:
                uses_user_config = True

            # SFTP host key validation needs a real file on disk: rclone enables
            # it only when known_hosts_file is set. Public keys only, so this does
            # not weaken the no-plaintext-on-disk guarantee.
            if connection.type == ConnectionType.sftp:
                content = known_hosts_content(connection)
                if content:
                    if temp_dir is None:
                        temp_dir = tempfile.TemporaryDirectory(prefix="hivesync-")
                    path = Path(temp_dir.name) / f"known_hosts_{alias}"
                    path.write_text(content, encoding="ascii")
                    endpoint.env[env_var_name(alias, "known_hosts_file")] = str(path)

            endpoints[alias] = endpoint
            merged_env.update(endpoint.env)
            redactable.extend(endpoint.redactable)

        redactor = Redactor(redactable)

        if uses_user_config:
            config_path = settings.user_rclone_conf
            if not config_path.is_file():
                raise RemoteConfigError(
                    "This job uses a remote from the mounted rclone config, but no "
                    f"file exists at {config_path}. Mount your rclone.conf there, "
                    "read only, and try again."
                )
            base_args = ["--config", str(config_path)]
        else:
            # Empty string disables config file lookup entirely, so no stray
            # config can be picked up and no NOTICE is emitted.
            base_args = ["--config", ""]

        yield Prepared(
            endpoints=endpoints,
            env=merged_env,
            base_args=base_args,
            redactor=redactor,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _resolve_native(
    connection: Connection, alias: str, box: SecretBox, redactor: Redactor
) -> Endpoint:
    env = _native_env(connection, alias)
    redactable: list[str] = []

    if connection.credential is not None:
        secrets = _decrypt_credential(connection.credential, box, redactor)
        for key, value in secrets.options.items():
            env[env_var_name(alias, key)] = value
        redactable.extend(secrets.redactable)

    _extra_opts_env(connection, alias, env)

    return Endpoint(
        alias=alias,
        path=_base_path_for(connection),
        env=env,
        redactable=tuple(redactable),
        uses_user_config=False,
    )


def _resolve_rclone_remote(
    connection: Connection,
    alias: str,
    box: SecretBox,
    redactor: Redactor,
    settings: Settings,
) -> Endpoint:
    if connection.rclone_mode == RcloneMode.imported:
        name = (connection.rclone_remote_name or "").strip()
        if not name:
            raise RemoteConfigError(
                f"Connection '{connection.name}' is an imported rclone remote but "
                "no remote name is selected. Pick one and try again."
            )
        # Addressed by its own name from the user's config. Nothing to define,
        # and nothing of theirs enters the database.
        return Endpoint(
            alias=name,
            path=(connection.base_path or "").strip("/"),
            env={},
            redactable=(),
            uses_user_config=True,
        )

    backend = (connection.rclone_backend_type or "").strip()
    if not backend:
        raise RemoteConfigError(
            f"Connection '{connection.name}' is an inline rclone remote but no "
            "backend type is set. Choose one and try again."
        )
    env = {env_var_name(alias, "type"): backend}
    redactable: list[str] = []

    if connection.credential is not None:
        secrets = _decrypt_credential(connection.credential, box, redactor)
        for key, value in secrets.options.items():
            env[env_var_name(alias, key)] = value
        redactable.extend(secrets.redactable)

    _extra_opts_env(connection, alias, env)

    return Endpoint(
        alias=alias,
        path=(connection.base_path or "").strip("/"),
        env=env,
        redactable=tuple(redactable),
        uses_user_config=False,
    )


@dataclass(frozen=True)
class ParsedStanza:
    """An rclone.conf section parsed into editable rows."""

    name: str
    backend_type: str
    options: dict[str, str]
    secret_option_names: tuple[str, ...]


def parse_stanza(text: str, *, password_options: Sequence[str] = ()) -> ParsedStanza:
    """Parse a pasted rclone.conf stanza.

    SPEC 5.2 calls this the highest value convenience in the feature, and it is
    also where obscured values enter the system: anything copied out of a real
    rclone.conf is already obscured, and is flagged as such so it is not obscured
    a second time.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise RemoteConfigError(
            "That does not look like an rclone config section. It should start "
            "with a name in square brackets, followed by key = value lines."
        ) from exc

    sections = parser.sections()
    if not sections:
        raise RemoteConfigError(
            "No config section was found. Paste a block that starts with a name "
            "in square brackets, for example [mydrive]."
        )
    name = sections[0]
    values = {key.strip(): (value or "").strip() for key, value in parser.items(name)}

    backend_type = values.pop("type", "")
    if not backend_type:
        raise RemoteConfigError(
            f"The section [{name}] has no 'type' line, so the backend is unknown. "
            "Add one, for example: type = s3"
        )

    known_passwords = {option.lower() for option in password_options}
    secrets = tuple(key for key in values if key.lower() in known_passwords)

    return ParsedStanza(
        name=name,
        backend_type=backend_type,
        options=values,
        secret_option_names=secrets,
    )

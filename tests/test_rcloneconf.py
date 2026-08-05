"""Remote definition rendering.

The central assertion in this file is that no secret ever reaches argv. Secrets
travel in the environment, so a stored or logged command has nothing to leak. If
that property breaks, one of these tests fails.

`obscure` is patched out where a test does not care about it, because it shells
out to rclone, which is not installed on a developer workstation. The real thing
is exercised by the integration suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import crypto
from app.config import Settings
from app.crypto import SecretBox
from app.engines import rcloneconf
from app.engines.rcloneconf import (
    ALIAS_DEST,
    ALIAS_SOURCE,
    RemoteConfigError,
    env_var_name,
    join_subpath,
)
from app.models import (
    Connection,
    ConnectionType,
    Credential,
    CredentialKind,
    RcloneMode,
)
from tests.conftest import TEST_SECRET_KEY, make_settings

SFTP_PASSWORD = "correct-horse-battery-staple"
FAKE_OBSCURED = "OBSCURED-ncqLbrLh2eYEVFr"


@pytest.fixture(autouse=True)
def _no_real_rclone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for `rclone obscure -`, which needs the real binary."""
    monkeypatch.setattr(
        rcloneconf, "obscure", lambda value, redactor=None: f"{FAKE_OBSCURED}:{len(value)}"
    )


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(TEST_SECRET_KEY)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def _password_credential(box: SecretBox, value: str = SFTP_PASSWORD) -> Credential:
    return Credential(
        name="sftp-pass",
        kind=CredentialKind.password,
        secret_ciphertext=box.encrypt(value),
        is_obscured=False,
    )


def _sftp(box: SecretBox, **overrides: object) -> Connection:
    connection = Connection(
        name="prod-sftp",
        type=ConnectionType.sftp,
        host="sftp.example.test",
        port=2222,
        username="svc",
        base_path="/var/www",
    )
    connection.credential = _password_credential(box)
    for key, value in overrides.items():
        setattr(connection, key, value)
    return connection


def test_env_var_name_uppercases_both_parts() -> None:
    assert env_var_name("hs_src", "type") == "RCLONE_CONFIG_HS_SRC_TYPE"


def test_native_sftp_renders_expected_env(box: SecretBox, settings: Settings) -> None:
    with rcloneconf.prepare({ALIAS_SOURCE: _sftp(box)}, box=box, settings=settings) as prepared:
        env = prepared.env
        assert env["RCLONE_CONFIG_HS_SRC_TYPE"] == "sftp"
        assert env["RCLONE_CONFIG_HS_SRC_HOST"] == "sftp.example.test"
        assert env["RCLONE_CONFIG_HS_SRC_PORT"] == "2222"
        assert env["RCLONE_CONFIG_HS_SRC_USER"] == "svc"
        assert env["RCLONE_CONFIG_HS_SRC_PASS"].startswith(FAKE_OBSCURED)


def test_secret_never_appears_in_argv(box: SecretBox, settings: Settings) -> None:
    """The property the whole design rests on."""
    with rcloneconf.prepare({ALIAS_SOURCE: _sftp(box)}, box=box, settings=settings) as prepared:
        argv = prepared.argv("lsd", prepared.endpoints[ALIAS_SOURCE].spec())
        joined = " ".join(argv)
        assert SFTP_PASSWORD not in joined
        assert FAKE_OBSCURED not in joined
        # And the plaintext is not in the env either, only the obscured form.
        assert SFTP_PASSWORD not in " ".join(prepared.env.values())


def test_redactor_masks_both_plaintext_and_obscured(box: SecretBox, settings: Settings) -> None:
    """An obscured value is still a credential: rclone reveal undoes it."""
    with rcloneconf.prepare({ALIAS_SOURCE: _sftp(box)}, box=box, settings=settings) as prepared:
        obscured = prepared.env["RCLONE_CONFIG_HS_SRC_PASS"]
        text = f"leaked {SFTP_PASSWORD} and {obscured}"
        redacted = prepared.redactor.redact(text)
        assert SFTP_PASSWORD not in redacted
        assert obscured not in redacted
        assert crypto.REDACTED in redacted


def test_already_obscured_credential_is_not_obscured_twice(
    box: SecretBox, settings: Settings
) -> None:
    """A value pasted from a real rclone.conf arrives obscured. Obscuring it again
    makes rclone reject it."""
    stored = "ncqLbrLh2eYEVFr_u1NtvbuiiHFiT6h17YVImQmrGY4"
    connection = _sftp(box)
    connection.credential = Credential(
        name="imported",
        kind=CredentialKind.password,
        secret_ciphertext=box.encrypt(stored),
        is_obscured=True,
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.env["RCLONE_CONFIG_HS_SRC_PASS"] == stored


def test_ssh_key_goes_inline_and_is_not_obscured(box: SecretBox, settings: Settings) -> None:
    """key_pem is Sensitive but not IsPassword, so obscuring would corrupt it.
    Passing it inline is also what removes the need for a temp key file."""
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    connection = _sftp(box)
    connection.credential = Credential(
        name="ssh",
        kind=CredentialKind.ssh_key,
        secret_ciphertext=box.encrypt(pem),
        key_passphrase_ciphertext=box.encrypt("keypass"),
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.env["RCLONE_CONFIG_HS_SRC_KEY_PEM"] == pem
        assert prepared.env["RCLONE_CONFIG_HS_SRC_KEY_FILE_PASS"].startswith(FAKE_OBSCURED)


def test_ftps_maps_to_ftp_with_explicit_tls(box: SecretBox, settings: Settings) -> None:
    """rclone has no ftps backend. FTPS is type=ftp plus explicit_tls."""
    connection = Connection(
        name="ftps", type=ConnectionType.ftps, host="ftp.example.test", base_path="pub"
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.env["RCLONE_CONFIG_HS_SRC_TYPE"] == "ftp"
        assert prepared.env["RCLONE_CONFIG_HS_SRC_EXPLICIT_TLS"] == "true"


def test_smb_share_becomes_the_first_path_element(box: SecretBox, settings: Settings) -> None:
    """SPEC 5.1: rclone addresses SMB as remote:Share/path."""
    connection = Connection(
        name="synology",
        type=ConnectionType.smb,
        host="10.0.20.15",
        share="Media",
        base_path="photos/2026",
    )
    with rcloneconf.prepare({ALIAS_DEST: connection}, box=box, settings=settings) as prepared:
        assert prepared.endpoints[ALIAS_DEST].spec() == "hs_dst:Media/photos/2026"
        assert prepared.env["RCLONE_CONFIG_HS_DST_DOMAIN"] == "WORKGROUP"


def test_smb_without_a_share_is_refused(box: SecretBox, settings: Settings) -> None:
    connection = Connection(
        name="broken-smb", type=ConnectionType.smb, host="10.0.20.15", base_path="x"
    )
    with (
        pytest.raises(RemoteConfigError, match="no share set"),
        rcloneconf.prepare({ALIAS_DEST: connection}, box=box, settings=settings),
    ):
        pass


def test_missing_host_is_refused_with_actionable_message(
    box: SecretBox, settings: Settings
) -> None:
    connection = Connection(name="nohost", type=ConnectionType.sftp, base_path="/x")
    with (
        pytest.raises(RemoteConfigError, match="needs a host"),
        rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings),
    ):
        pass


def test_extra_opts_override_defaults(box: SecretBox, settings: Settings) -> None:
    """An advanced options box that cannot override a default is pointless."""
    connection = Connection(
        name="implicit-ftps",
        type=ConnectionType.ftps,
        host="ftp.example.test",
        base_path="",
        extra_opts={"explicit_tls": "false", "tls": "true"},
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.env["RCLONE_CONFIG_HS_SRC_TLS"] == "true"
        assert prepared.env["RCLONE_CONFIG_HS_SRC_EXPLICIT_TLS"] == "false"


def test_local_path_is_not_stripped(box: SecretBox, settings: Settings) -> None:
    connection = Connection(name="disk", type=ConnectionType.local, base_path="/data/media")
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.endpoints[ALIAS_SOURCE].spec() == "hs_src:/data/media"


def test_config_is_disabled_when_no_imported_remote(box: SecretBox, settings: Settings) -> None:
    """--config "" stops rclone picking up a config file we did not intend."""
    with rcloneconf.prepare({ALIAS_SOURCE: _sftp(box)}, box=box, settings=settings) as prepared:
        assert prepared.base_args == ["--config", ""]


def test_imported_remote_uses_the_user_config(box: SecretBox, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.user_rclone_conf.parent.mkdir(parents=True, exist_ok=True)
    settings.user_rclone_conf.write_text("[mydrive]\ntype = local\n", encoding="utf-8")
    connection = Connection(
        name="imported",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.imported,
        rclone_remote_name="mydrive",
        base_path="folder",
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.base_args == ["--config", str(settings.user_rclone_conf)]
        # Addressed by its own name, and nothing of the user's enters the env.
        assert prepared.endpoints[ALIAS_SOURCE].spec() == "mydrive:folder"
        assert prepared.env == {}


def test_missing_user_config_is_a_clear_error(box: SecretBox, settings: Settings) -> None:
    connection = Connection(
        name="imported",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.imported,
        rclone_remote_name="mydrive",
        base_path="",
    )
    with (
        pytest.raises(RemoteConfigError, match="no file exists at"),
        rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings),
    ):
        pass


def test_inline_and_imported_endpoints_coexist(box: SecretBox, tmp_path: Path) -> None:
    """Verified against rclone 1.74.4: env var remotes and --config coexist. This
    is what makes the temp file fallback in SPEC 5.3 unnecessary."""
    settings = make_settings(tmp_path)
    settings.user_rclone_conf.parent.mkdir(parents=True, exist_ok=True)
    settings.user_rclone_conf.write_text("[mydrive]\ntype = local\n", encoding="utf-8")
    imported = Connection(
        name="imported",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.imported,
        rclone_remote_name="mydrive",
        base_path="a",
    )
    with rcloneconf.prepare(
        {ALIAS_SOURCE: _sftp(box), ALIAS_DEST: imported}, box=box, settings=settings
    ) as prepared:
        assert prepared.base_args == ["--config", str(settings.user_rclone_conf)]
        assert prepared.env["RCLONE_CONFIG_HS_SRC_TYPE"] == "sftp"
        assert prepared.endpoints[ALIAS_DEST].spec() == "mydrive:a"


def test_inline_rclone_remote_needs_a_backend_type(box: SecretBox, settings: Settings) -> None:
    connection = Connection(
        name="inline",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.inline,
        base_path="",
    )
    with (
        pytest.raises(RemoteConfigError, match="no backend type"),
        rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings),
    ):
        pass


def test_inline_backend_secret_json_expands_to_options(box: SecretBox, settings: Settings) -> None:
    """An inline backend can need several secrets, and Connection carries one
    credential_id, so they travel together as JSON."""
    payload = (
        '{"access_key_id": {"value": "AKIA123", "obscured": false},'
        ' "secret_access_key": {"value": "shhh-very-secret", "obscured": false}}'
    )
    connection = Connection(
        name="s3",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.inline,
        rclone_backend_type="s3",
        base_path="bucket",
    )
    connection.credential = Credential(
        name="s3-keys",
        kind=CredentialKind.backend_secret,
        secret_ciphertext=box.encrypt(payload),
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert prepared.env["RCLONE_CONFIG_HS_SRC_TYPE"] == "s3"
        assert prepared.env["RCLONE_CONFIG_HS_SRC_ACCESS_KEY_ID"].startswith(FAKE_OBSCURED)
        assert "shhh-very-secret" not in " ".join(prepared.env.values())


def test_corrupt_backend_secret_gives_an_actionable_error(
    box: SecretBox, settings: Settings
) -> None:
    connection = Connection(
        name="s3",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.inline,
        rclone_backend_type="s3",
        base_path="",
    )
    connection.credential = Credential(
        name="broken",
        kind=CredentialKind.backend_secret,
        secret_ciphertext=box.encrypt("not json at all"),
    )
    with (
        pytest.raises(RemoteConfigError, match="Delete it and enter it again"),
        rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings),
    ):
        pass


def test_pinned_host_key_becomes_a_known_hosts_file(box: SecretBox, settings: Settings) -> None:
    """rclone validates host keys only when known_hosts_file is set."""
    connection = _sftp(
        box, host_keys="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA", host_keys_trusted=True
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        path = Path(prepared.env["RCLONE_CONFIG_HS_SRC_KNOWN_HOSTS_FILE"])
        assert path.is_file()
        content = path.read_text(encoding="ascii")
        # Non-default port must be bracketed, per known_hosts format.
        assert content.startswith("[sftp.example.test]:2222 ssh-ed25519 ")
    # Cleaned up when the context exits.
    assert not path.exists()


def test_no_known_hosts_file_when_nothing_is_pinned(box: SecretBox, settings: Settings) -> None:
    with rcloneconf.prepare({ALIAS_SOURCE: _sftp(box)}, box=box, settings=settings) as prepared:
        assert "RCLONE_CONFIG_HS_SRC_KNOWN_HOSTS_FILE" not in prepared.env


def test_recorded_but_unapproved_keys_are_not_honoured(box: SecretBox, settings: Settings) -> None:
    """Scanned is not trusted. Validating against keys no human confirmed is the
    same as not validating at all."""
    connection = _sftp(
        box, host_keys="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA", host_keys_trusted=False
    )
    with rcloneconf.prepare({ALIAS_SOURCE: connection}, box=box, settings=settings) as prepared:
        assert "RCLONE_CONFIG_HS_SRC_KNOWN_HOSTS_FILE" not in prepared.env


@pytest.mark.parametrize(
    ("base", "sub", "expected"),
    [
        ("Media/photos", None, "Media/photos"),
        ("Media/photos", "", "Media/photos"),
        ("Media/photos", "2026", "Media/photos/2026"),
        ("Media/photos", "/2026/", "Media/photos/2026"),
        ("Media/photos", "a/b", "Media/photos/a/b"),
        ("Media/photos", "./a", "Media/photos/a"),
        ("Media/photos", "a\\b", "Media/photos/a/b"),
        ("", "top", "top"),
    ],
)
def test_join_subpath(base: str, sub: str | None, expected: str) -> None:
    assert join_subpath(base, sub) == expected


@pytest.mark.parametrize("evil", ["..", "../etc", "a/../..", "/../x", "a/b/../../.."])
def test_join_subpath_refuses_traversal(evil: str) -> None:
    """A directory picker that can walk out of its base path is a bug."""
    with pytest.raises(RemoteConfigError, match="outside the configured base path"):
        join_subpath("Media/photos", evil)


def test_parse_stanza_extracts_type_and_options() -> None:
    parsed = rcloneconf.parse_stanza(
        """
        [mydrive]
        type = drive
        client_id = abc123
        token = {"access_token":"x"}
        """,
        password_options=["pass"],
    )
    assert parsed.name == "mydrive"
    assert parsed.backend_type == "drive"
    assert parsed.options["client_id"] == "abc123"
    assert "type" not in parsed.options


def test_parse_stanza_flags_password_options() -> None:
    parsed = rcloneconf.parse_stanza(
        "[nas]\ntype = smb\nhost = 10.0.0.5\npass = ncqLbrLh2eYEVFr\n",
        password_options=["pass"],
    )
    assert parsed.secret_option_names == ("pass",)


def test_parse_stanza_without_type_is_refused() -> None:
    with pytest.raises(RemoteConfigError, match="no 'type' line"):
        rcloneconf.parse_stanza("[nas]\nhost = 10.0.0.5\n")


def test_parse_stanza_without_a_section_is_refused() -> None:
    with pytest.raises(RemoteConfigError, match="square brackets"):
        rcloneconf.parse_stanza("type = smb\nhost = 10.0.0.5\n")

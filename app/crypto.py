"""Secret handling. This is the only module in the application that touches
ciphertext or plaintext credentials.

Rules enforced here, per CLAUDE.md:

- No other module imports Fernet, HKDF, or handles a raw key.
- No secret value reaches a log line, a repr, an exception message, or a
  stored command. Exceptions raised from this module carry no secret material.
- The encryption key is supplied by the environment and never persisted.

The key fingerprint below exists because a lost or swapped HIVESYNC_SECRET_KEY
otherwise stays invisible until a scheduled job fails at 2:30 AM with an opaque
InvalidToken. Storing a fingerprint lets startup say plainly that the key changed.
"""

from __future__ import annotations

import base64
import hmac
import re
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Fixed, non-secret domain separation constants. Changing either value
# invalidates every stored fingerprint or session cookie respectively.
_FINGERPRINT_MESSAGE: Final[bytes] = b"hivesync/secret-key-fingerprint/v1"
_SESSION_INFO: Final[bytes] = b"hivesync/session-cookie-signing/v1"

_FERNET_KEY_BYTES: Final[int] = 32


class CryptoKeyError(Exception):
    """The configured encryption key is missing, malformed, or has changed.

    Never carries key material in its message.
    """


class SecretDecryptError(Exception):
    """Stored ciphertext could not be decrypted with the configured key."""


def generate_key_suggestion() -> str:
    """Return a fresh, valid key that a user can paste into their environment."""
    return Fernet.generate_key().decode("ascii")


def _key_bytes(raw_key: str) -> bytes:
    """Decode and validate a key, raising CryptoKeyError on anything unusable."""
    if not raw_key or not raw_key.strip():
        raise CryptoKeyError(
            "HIVESYNC_SECRET_KEY is not set. HiveSync will not start without it, "
            "because every stored credential is encrypted with it. Generate one "
            "with `make secret-key` and set it in your environment or .env file."
        )
    try:
        decoded = base64.urlsafe_b64decode(raw_key.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CryptoKeyError(
            "HIVESYNC_SECRET_KEY is not valid urlsafe base64. It must be a "
            "Fernet key, which is 32 random bytes encoded as urlsafe base64. "
            "Generate a correct one with `make secret-key`."
        ) from exc
    if len(decoded) != _FERNET_KEY_BYTES:
        raise CryptoKeyError(
            f"HIVESYNC_SECRET_KEY decodes to {len(decoded)} bytes, but a Fernet "
            f"key must be exactly {_FERNET_KEY_BYTES} bytes. Generate a correct "
            "one with `make secret-key`."
        )
    return decoded


def validate_key(raw_key: str) -> None:
    """Raise CryptoKeyError unless raw_key is a usable Fernet key."""
    _key_bytes(raw_key)


def key_fingerprint(raw_key: str) -> str:
    """Return a stable, non-reversing identifier for a key.

    An HMAC over a fixed constant, keyed by the secret. Two processes with the
    same key agree; the fingerprint reveals nothing about the key itself.
    """
    digest = hmac.new(_key_bytes(raw_key), _FINGERPRINT_MESSAGE, sha256).hexdigest()
    return digest[:32]


def derive_session_secret(raw_key: str) -> str:
    """Derive the session cookie signing key from the master key.

    Separate derivation so the signing key and the encryption key are never the
    same bytes, even though both come from one environment variable.
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_SESSION_INFO)
    return base64.urlsafe_b64encode(hkdf.derive(_key_bytes(raw_key))).decode("ascii")


class SecretBox:
    """Encrypts and decrypts credential material.

    Holds the key in a private attribute and overrides repr so the key cannot
    leak through a traceback frame, a debugger, or a logged object.
    """

    __slots__ = ("_fernet",)

    def __init__(self, raw_key: str) -> None:
        self._fernet = Fernet(base64.urlsafe_b64encode(_key_bytes(raw_key)))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            # Deliberately no ciphertext, no key, no plaintext in the message.
            raise SecretDecryptError(
                "A stored credential could not be decrypted. This usually means "
                "HIVESYNC_SECRET_KEY differs from the key used when the "
                "credential was saved. Restore the original key, or delete and "
                "re-enter the affected credential."
            ) from exc

    def __repr__(self) -> str:
        return "SecretBox(key=<redacted>)"

    __str__ = __repr__


REDACTED = "***REDACTED***"

# Values shorter than this are not substring-redacted, because masking every
# occurrence of a two character string would corrupt unrelated output. A secret
# that short is refused at input validation instead.
_MIN_REDACTABLE_LENGTH: Final[int] = 4

# Env var names whose values are masked wholesale, regardless of content. Belt
# and braces alongside value based redaction: if a secret is somehow not in the
# known set, the variable carrying it is still masked.
_SENSITIVE_ENV_RE: Final[re.Pattern[str]] = re.compile(
    r"(PASS|PASSWORD|SECRET|TOKEN|KEY_PEM|KEY_FILE_PASS|CREDENTIAL)", re.IGNORECASE
)


class Redactor:
    """Removes known secret values from text, argv lists and environments.

    Built per operation from the plaintext and obscured forms of every secret
    that operation touches. The obscured form matters as much as the plaintext:
    `rclone obscure` is reversible with `rclone reveal`, so an obscured password
    is still a credential and must never be logged or stored.

    Redaction is a backstop, not the primary defence. The primary defence is that
    secrets are passed to rclone through the environment and never appear in a
    command line, so a stored command has nothing to leak in the first place.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Iterable[str]) -> None:
        unique = {value for value in values if value and len(value) >= _MIN_REDACTABLE_LENGTH}
        # Longest first, so a secret that contains a shorter one is masked whole
        # rather than being partially replaced and left recognisable.
        self._values = sorted(unique, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, REDACTED)
        return text

    def redact_argv(self, argv: Sequence[str]) -> list[str]:
        return [self.redact(part) for part in argv]

    def redact_env(self, env: Mapping[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in env.items():
            result[name] = REDACTED if _SENSITIVE_ENV_RE.search(name) else self.redact(value)
        return result

    def command_line(self, argv: Sequence[str]) -> str:
        """A redacted, human readable command for JobRun.command_redacted."""
        return " ".join(self.redact_argv(argv))

    def __repr__(self) -> str:
        return f"Redactor(values=<{len(self._values)} redacted>)"

    __str__ = __repr__

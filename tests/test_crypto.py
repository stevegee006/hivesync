"""Tests for the one module that handles secrets.

The redaction assertions here are the seed of the wider redaction test SPEC
section 17 asks for at M8, which greps every log file and DB text column for
known test secrets.
"""

from __future__ import annotations

import base64

import pytest

from app import crypto

KEY_A = crypto.generate_key_suggestion()
KEY_B = crypto.generate_key_suggestion()
PLAINTEXT = "correct horse battery staple"


def test_generated_key_is_valid() -> None:
    crypto.validate_key(crypto.generate_key_suggestion())


def test_roundtrip() -> None:
    box = crypto.SecretBox(KEY_A)
    assert box.decrypt(box.encrypt(PLAINTEXT)) == PLAINTEXT


def test_ciphertext_does_not_contain_plaintext() -> None:
    ciphertext = crypto.SecretBox(KEY_A).encrypt(PLAINTEXT)
    assert PLAINTEXT.encode() not in ciphertext
    # Fernet tokens are base64, so check the decoded form too.
    decoded = base64.urlsafe_b64decode(ciphertext + b"=" * (-len(ciphertext) % 4))
    assert PLAINTEXT.encode() not in decoded


def test_unicode_roundtrip() -> None:
    secret = "pässwörd-日本語-\U0001f512"
    box = crypto.SecretBox(KEY_A)
    assert box.decrypt(box.encrypt(secret)) == secret


def test_wrong_key_raises_without_leaking() -> None:
    ciphertext = crypto.SecretBox(KEY_A).encrypt(PLAINTEXT)
    with pytest.raises(crypto.SecretDecryptError) as excinfo:
        crypto.SecretBox(KEY_B).decrypt(ciphertext)
    message = str(excinfo.value)
    assert PLAINTEXT not in message
    assert KEY_A not in message
    assert KEY_B not in message
    # The message has to be actionable, not just safe.
    assert "HIVESYNC_SECRET_KEY" in message


def test_repr_hides_the_key() -> None:
    box = crypto.SecretBox(KEY_A)
    assert KEY_A not in repr(box)
    assert KEY_A not in str(box)
    assert "redacted" in repr(box)


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        "not base64 at all !!!",
        base64.urlsafe_b64encode(b"too short").decode(),
        base64.urlsafe_b64encode(b"x" * 64).decode(),
    ],
)
def test_invalid_keys_are_rejected(bad_key: str) -> None:
    with pytest.raises(crypto.CryptoKeyError) as excinfo:
        crypto.validate_key(bad_key)
    assert "HIVESYNC_SECRET_KEY" in str(excinfo.value)


def test_key_error_messages_never_echo_the_key() -> None:
    bad = base64.urlsafe_b64encode(b"short").decode()
    with pytest.raises(crypto.CryptoKeyError) as excinfo:
        crypto.validate_key(bad)
    assert bad not in str(excinfo.value)


def test_fingerprint_is_stable_and_key_specific() -> None:
    assert crypto.key_fingerprint(KEY_A) == crypto.key_fingerprint(KEY_A)
    assert crypto.key_fingerprint(KEY_A) != crypto.key_fingerprint(KEY_B)


def test_fingerprint_does_not_reveal_the_key() -> None:
    fingerprint = crypto.key_fingerprint(KEY_A)
    assert KEY_A not in fingerprint
    assert fingerprint not in KEY_A


def test_session_secret_is_derived_not_reused() -> None:
    derived = crypto.derive_session_secret(KEY_A)
    assert derived != KEY_A
    assert derived == crypto.derive_session_secret(KEY_A)
    assert derived != crypto.derive_session_secret(KEY_B)


def test_the_documented_key_commands_produce_a_usable_key() -> None:
    """The README tells people to use `openssl rand -base64 32`, which emits
    standard base64 with `+` and `/` where Fernet's documentation says url-safe
    with `-` and `_`. Both decode to the same 32 bytes and both are accepted, so
    the advice is sound, but it is the kind of thing that is true until a
    dependency tightens its validation.
    """
    import base64
    import os

    from app.crypto import SecretBox

    # A key whose standard spelling really does contain both characters, rather
    # than random bytes that might encode identically in either alphabet.
    while True:
        raw = os.urandom(32)
        standard = base64.b64encode(raw).decode()
        if "+" in standard and "/" in standard:
            break
    urlsafe = base64.urlsafe_b64encode(raw).decode()
    assert standard != urlsafe

    for spelling in (standard, urlsafe):
        box = SecretBox(spelling)
        assert box.decrypt(box.encrypt("hunter2")) == "hunter2"

    # And they are the same key, so a key copied in either form still reads the
    # database written with the other.
    assert SecretBox(urlsafe).decrypt(SecretBox(standard).encrypt("same")) == "same"

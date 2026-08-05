"""Capability interpretation and the two-endpoint intersection.

The reason strings are asserted on directly. M1's fourth acceptance criterion is
that a hash-less backend disables checksum comparison with a visible reason, and
a reason string that silently changes to something unhelpful is a regression even
though nothing raises.

The probe payloads here are shaped like real rclone 1.74.4 output, recorded in
CLAUDE.md, not invented.
"""

from __future__ import annotations

from datetime import timedelta

from app import capabilities
from app.capabilities import MODTIME_UNSUPPORTED_PRECISION
from app.models import Connection, ConnectionType, utcnow

# Trimmed from real `rclone backend features` output.
LOCAL_PROBE = {
    "Name": "local",
    "Precision": 1,
    "Hashes": ["md5", "sha1", "crc32", "sha256"],
    "Features": {
        "Move": True,
        "DirMove": True,
        "Copy": False,
        "CanHaveEmptyDirectories": True,
        "CaseInsensitive": False,
        "About": True,
        "BucketBased": False,
    },
}

# SMB: no hashes at all, but Move and modtimes work. SPEC 11.1.
SMB_PROBE = {
    "Name": "smb",
    "Precision": 1,
    "Hashes": [],
    "Features": {
        "Move": True,
        "DirMove": True,
        "Copy": False,
        "CanHaveEmptyDirectories": True,
        "CaseInsensitive": True,
        "About": True,
        "BucketBased": False,
    },
}

SFTP_PROBE = {
    "Name": "sftp",
    "Precision": 1,
    "Hashes": ["md5", "sha1"],
    "Features": {
        "Move": True,
        "DirMove": True,
        "Copy": True,
        "CanHaveEmptyDirectories": True,
        "CaseInsensitive": False,
        "About": True,
        "BucketBased": False,
    },
}

# A backend that cannot set modification times signals it through Precision,
# because there is no file level Features flag for it.
NO_MODTIME_PROBE = {
    "Name": "someobjectstore",
    "Precision": MODTIME_UNSUPPORTED_PRECISION,
    "Hashes": ["md5"],
    "Features": {
        "Move": False,
        "CanHaveEmptyDirectories": False,
        "CaseInsensitive": False,
        "BucketBased": True,
    },
}


def _connection(name: str, probe: dict | None, *, age_days: int = 0) -> Connection:
    connection = Connection(
        name=name,
        type=ConnectionType.smb if probe is SMB_PROBE else ConnectionType.sftp,
        host="example.test",
        base_path="/srv",
    )
    if probe is not None:
        connection.capabilities = probe
        connection.capabilities_probed_at = utcnow() - timedelta(days=age_days)
    return connection


def test_precision_decides_modtime_support() -> None:
    """There is no CanSetModTime flag, so this must read Precision."""
    assert capabilities.from_probe(LOCAL_PROBE, utcnow()).can_set_modtime is True
    assert capabilities.from_probe(NO_MODTIME_PROBE, utcnow()).can_set_modtime is False


def test_hashes_and_features_parsed() -> None:
    caps = capabilities.from_probe(SFTP_PROBE, utcnow())
    assert caps.hashes == frozenset({"md5", "sha1"})
    assert caps.supports_move is True
    assert caps.supports_empty_dirs is True
    assert caps.case_insensitive is False


def test_non_boolean_features_are_ignored() -> None:
    """Features is documented as booleans. Anything else must not crash the probe."""
    caps = capabilities.from_probe(
        {"Precision": 1, "Hashes": [], "Features": {"Move": True, "Weird": "yes"}}, utcnow()
    )
    assert caps.features == {"Move": True}


def test_missing_precision_is_treated_as_no_modtime() -> None:
    """Fail closed: an unreadable probe must not enable bidirectional sync."""
    caps = capabilities.from_probe({"Hashes": [], "Features": {}}, utcnow())
    assert caps.can_set_modtime is False


def test_checksum_available_when_hashes_overlap() -> None:
    result = capabilities.intersect(_connection("src", SFTP_PROBE), _connection("dst", LOCAL_PROBE))
    assert result.checksum.available is True
    assert "md5" in result.shared_hashes


def test_hashless_backend_disables_checksum_with_a_reason() -> None:
    """M1 acceptance criterion four."""
    result = capabilities.intersect(
        _connection("prod-sftp", SFTP_PROBE), _connection("synology", SMB_PROBE)
    )
    assert result.checksum.available is False
    reason = result.checksum.reason
    assert reason is not None
    assert "Checksum comparison unavailable" in reason
    assert "synology" in reason
    assert "no hash types" in reason
    # It must also say what happens instead, not just what is refused.
    assert "modification time and size" in reason


def test_both_hashless_endpoints_are_both_named() -> None:
    result = capabilities.intersect(
        _connection("nas-a", SMB_PROBE), _connection("nas-b", SMB_PROBE)
    )
    reason = result.checksum.reason
    assert reason is not None
    assert "nas-a" in reason
    assert "nas-b" in reason
    assert "expose" in reason  # plural agreement


def test_disjoint_hashes_are_distinguished_from_no_hashes() -> None:
    """Two endpoints that both hash but share nothing get a different explanation."""
    a = _connection("a", {"Precision": 1, "Hashes": ["md5"], "Features": {"Move": True}})
    b = _connection("b", {"Precision": 1, "Hashes": ["sha1"], "Features": {"Move": True}})
    result = capabilities.intersect(a, b)
    assert result.checksum.available is False
    assert result.checksum.reason is not None
    assert "share no common hash type" in result.checksum.reason


def test_bidirectional_blocked_when_modtimes_unwritable() -> None:
    result = capabilities.intersect(
        _connection("src", LOCAL_PROBE), _connection("objectstore", NO_MODTIME_PROBE)
    )
    assert result.bidirectional.available is False
    reason = result.bidirectional.reason
    assert reason is not None
    assert "objectstore" in reason
    assert "modification times" in reason


def test_bidirectional_allowed_when_both_write_modtimes() -> None:
    result = capabilities.intersect(_connection("src", SFTP_PROBE), _connection("dst", SMB_PROBE))
    assert result.bidirectional.available is True


def test_unprobed_endpoint_reports_unknown_not_unsupported() -> None:
    """Saying SMB has no hashes when nobody has looked sends people debugging the
    wrong thing."""
    result = capabilities.intersect(
        _connection("tested", SFTP_PROBE), _connection("never-tested", None)
    )
    assert result.checksum.available is False
    reason = result.checksum.reason
    assert reason is not None
    assert "never-tested" in reason
    assert "not been tested" in reason
    assert "no hash types" not in reason


def test_archive_warns_when_move_is_not_server_side() -> None:
    """SPEC 7.3: archiving without server-side Move is a full round trip."""
    result = capabilities.intersect(
        _connection("src", LOCAL_PROBE), _connection("slow-dst", NO_MODTIME_PROBE)
    )
    assert result.archive.available is True
    assert result.archive.warning is not None
    assert "server-side move" in result.archive.warning
    assert any("server-side move" in warning for warning in result.warnings)


def test_archive_has_no_warning_with_server_side_move() -> None:
    result = capabilities.intersect(_connection("src", SFTP_PROBE), _connection("dst", SMB_PROBE))
    assert result.archive.available is True
    assert result.archive.warning is None


def test_case_sensitivity_mismatch_warns() -> None:
    """SPEC 10.7: the symptom is a sync that never converges, so say so."""
    result = capabilities.intersect(
        _connection("linux-sftp", SFTP_PROBE), _connection("synology-smb", SMB_PROBE)
    )
    joined = " ".join(result.warnings)
    assert "case insensitive" in joined
    assert "case sensitive" in joined
    assert "synology-smb" in joined and "linux-sftp" in joined


def test_matching_case_sensitivity_does_not_warn() -> None:
    result = capabilities.intersect(_connection("a", SFTP_PROBE), _connection("b", LOCAL_PROBE))
    assert not any("case" in warning for warning in result.warnings)


def test_empty_dirs_blocked_when_unsupported() -> None:
    result = capabilities.intersect(
        _connection("src", LOCAL_PROBE), _connection("bucket", NO_MODTIME_PROBE)
    )
    assert result.empty_dirs.available is False
    assert result.empty_dirs.reason is not None
    assert "bucket" in result.empty_dirs.reason


def test_stale_probe_is_flagged() -> None:
    result = capabilities.intersect(
        _connection("fresh", SFTP_PROBE),
        _connection("ancient", LOCAL_PROBE, age_days=capabilities.STALE_PROBE_DAYS + 5),
    )
    assert result.stale is True
    assert any("ancient" in warning and "days ago" in warning for warning in result.warnings)


def test_fresh_probe_is_not_flagged_stale() -> None:
    result = capabilities.intersect(
        _connection("a", SFTP_PROBE), _connection("b", LOCAL_PROBE, age_days=1)
    )
    assert result.stale is False


def test_blocked_reasons_collects_every_explanation() -> None:
    result = capabilities.intersect(
        _connection("src", SMB_PROBE), _connection("bucket", NO_MODTIME_PROBE)
    )
    reasons = result.blocked_reasons()
    assert len(reasons) >= 3
    assert all(isinstance(reason, str) and reason for reason in reasons)


def test_no_capabilities_at_all_is_stale_and_unprobed() -> None:
    caps = capabilities.empty()
    assert caps.probed is False
    assert caps.is_stale is True
    assert caps.can_set_modtime is False

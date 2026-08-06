"""/api/health and /api/health/detail.

M0's acceptance criterion is that health reports the pinned binary versions. M8
moved that report behind authentication: an unauthenticated inventory of the
binaries on a box is a free list of things to attack. Liveness stays open,
because the container HEALTHCHECK has no session.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import binaries
from app.api import health as health_module
from app.binaries import BinaryInfo, BinaryReport


def test_health_is_reachable_without_a_session(client: TestClient) -> None:
    """The container HEALTHCHECK calls this, so it cannot require a login."""
    assert client.get("/api/health").status_code == 200


def test_liveness_discloses_no_versions(client: TestClient) -> None:
    """The whole point of the split. Anyone can reach this endpoint."""
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True
    assert "rclone" not in body
    assert "lftp" not in body
    assert "1.74.4" not in client.get("/api/health").text


def test_detail_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/health/detail").status_code == 401


def test_health_reports_pinned_binary_versions(authed_client: TestClient) -> None:
    """M0's criterion, now behind a session."""
    body = authed_client.get("/api/health/detail").json()
    assert body["status"] == "ok"
    assert body["rclone"]["version"] == "1.74.4"
    assert body["rclone"]["expected_version"] == "1.74.4"
    assert body["rclone"]["matches_expected"] is True
    assert body["lftp"]["version"] == "4.9.3"
    assert body["database"]["ok"] is True
    assert body["app_version"]


def test_version_mismatch_is_degraded_not_ok(app: FastAPI, authed_client: TestClient) -> None:
    """An rclone that is not the reviewed build must be visible. bisync flags vary
    between versions, so a silent substitution is a real hazard."""
    app.state.binaries = BinaryReport(
        rclone=BinaryInfo(name="rclone", ok=True, version="1.75.0"),
        lftp=BinaryInfo(name="lftp", ok=True, version="4.9.3"),
        expected_rclone_version="1.74.4",
    )
    body = authed_client.get("/api/health/detail").json()
    assert body["status"] == "degraded"
    assert body["rclone"]["matches_expected"] is False


def test_missing_binary_is_degraded_with_a_reason(app: FastAPI, authed_client: TestClient) -> None:
    """Degraded rather than a 503, so the UI stays reachable for diagnosis instead
    of the container entering a restart loop."""
    app.state.binaries = BinaryReport(
        rclone=BinaryInfo(name="rclone", ok=False, error="rclone was not found on PATH."),
        lftp=BinaryInfo(name="lftp", ok=True, version="4.9.3"),
        expected_rclone_version=None,
    )
    response = authed_client.get("/api/health/detail")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["rclone"]["ok"] is False
    assert "not found" in body["rclone"]["error"]


def test_database_failure_is_a_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable database means the app cannot work, so the healthcheck must
    fail and let the container be restarted."""
    monkeypatch.setattr(health_module, "database_ok", lambda _engine: (False, "OperationalError"))
    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["database"]["ok"] is False


def test_no_expectation_declared_is_not_a_mismatch() -> None:
    report = BinaryReport(
        rclone=BinaryInfo(name="rclone", ok=True, version="1.74.4"),
        lftp=BinaryInfo(name="lftp", ok=True, version="4.9.3"),
        expected_rclone_version=None,
    )
    assert report.rclone_matches_expected is None
    assert report.all_ok is True


def test_probe_reports_a_missing_binary_instead_of_raising() -> None:
    """Binary discovery never raises. Health has to be able to report."""
    info = binaries._probe("definitely-not-installed-hivesync", ["--version"], [])
    assert info.ok is False
    assert info.error is not None
    assert "not found" in info.error

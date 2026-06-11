"""Regression: probe_fn wiring arity between the router and the adapter.

The P0 this guards
------------------
``AnonymousPreviewBackendAdapter._safe_probe`` invokes the injected probe as
``probe_fn(upload)`` — a SINGLE ``UploadFacts`` argument. ``build_probe_fn``
returns a TWO-arg ``_probe(source_path, source_hash)`` callable. The router
originally wired ``build_probe_fn(settings)`` raw, so at runtime every upload
raised ``TypeError`` inside ``_safe_probe`` → fail-closed → a ``FAILED``
record, *after* the rate-limit slot was already burned. The feature was
100% non-functional in production, yet the unit suite stayed green because
every other test injects its own single-arg ``probe_fn`` and never routes
``build_probe_fn`` through the adapter.

``build_intake_probe_fn`` is the named seam the router now wires; these tests
run the REAL ``build_intake_probe_fn`` through the REAL adapter so the arity
contract can never silently regress again. ``test_raw_build_probe_fn_*``
reproduces the original defect to prove the wrapper is load-bearing.

ffmpeg/ffprobe are mocked — no real binaries required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple

import pytest

from gateway.anonymous_preview_probe import build_intake_probe_fn, build_probe_fn
from src.services.anonymous_preview_backend_adapter import (
    AnonymousPreviewBackendAdapter,
    RequestFacts,
    UploadFacts,
)
from src.services.anonymous_preview_intake import (
    ComplianceResult,
    ComplianceStatus,
    IntakeConfig,
    PreviewStatus,
    SourceType,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _AlwaysAcquireStore:
    """Counter store that always admits — isolates the probe seam from the
    rate-limit path."""

    def get(self, key: str) -> int:
        return 0

    def increment(self, key: str) -> int:
        return 1

    def try_acquire(self, key: str, cap: int) -> Tuple[bool, int]:
        return (True, 1)


def _pass_compliance(_probe_result) -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="ok",
        audit_metadata={},
    )


@pytest.fixture
def _mock_ffmpeg(monkeypatch):
    """Mock the subprocess layer so build_probe_fn runs without real ffmpeg.

    Source probe → ok; teaser cut → 30s mp4 with audio (well under the 180s
    cap), so the probe wrapper reaches a clean ``ProbeResult``.
    """
    monkeypatch.setattr(
        "gateway.anonymous_preview_probe.probe_source",
        lambda _source_path: {"ok": True},
    )
    monkeypatch.setattr(
        "gateway.anonymous_preview_probe.cut_teaser",
        lambda _src, _dest, max_seconds=180.0: SimpleNamespace(
            failure_reason=None,
            duration_seconds=30.0,
            container_format="mp4",
            has_audio=True,
        ),
    )


def _config(tmp_path: Path) -> IntakeConfig:
    return IntakeConfig(
        allowed_upload_types=("mp4",),
        max_upload_bytes=500_000_000,
        max_source_duration_seconds=3600,
        temp_upload_dir=tmp_path,
        temp_storage_available=True,
    )


def _upload(tmp_path: Path) -> UploadFacts:
    stored = tmp_path / "clip.mp4"
    stored.write_bytes(b"\x00")
    return UploadFacts(
        file_name="clip.mp4",
        byte_length=1_000_000,
        duration_seconds=0.0,
        source_hash="abc123def4567890",
        stored_path=stored,
    )


def _request() -> RequestFacts:
    return RequestFacts(
        raw_session_id="sess-raw",
        raw_ip="1.2.3.4",
        raw_device_cookie="dev-raw",
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=True,
        day_key="2026-01-01",
    )


def _adapter(config: IntakeConfig, probe_fn) -> AnonymousPreviewBackendAdapter:
    return AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=_AlwaysAcquireStore(),
        probe_fn=probe_fn,
        compliance_fn=_pass_compliance,
        hasher=lambda prefix, value: f"{prefix}:{value}",
        now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# The seam works (positive contract)
# ---------------------------------------------------------------------------


def test_build_intake_probe_fn_is_single_arg_and_returns_probe_result(
    _mock_ffmpeg, tmp_path
):
    """The wrapper accepts ONE UploadFacts (what the adapter passes) and
    echoes the source_hash through unchanged."""
    upload = _upload(tmp_path)
    probe_fn = build_intake_probe_fn(settings=None)

    result = probe_fn(upload)  # single arg — must not raise

    assert result.failure_reason is None
    assert result.source_hash == upload.source_hash
    assert result.duration_seconds == 30.0


def test_wrapped_probe_through_adapter_reaches_ready_for_mode(
    _mock_ffmpeg, tmp_path
):
    """Full intake through the REAL adapter with the REAL build_intake_probe_fn
    yields READY_FOR_MODE — the integrated path the router runs in prod."""
    adapter = _adapter(_config(tmp_path), build_intake_probe_fn(settings=None))

    record = adapter.handle_intake(_request(), _upload(tmp_path))

    assert record.status == PreviewStatus.READY_FOR_MODE
    assert record.source_hash == "abc123def4567890"


# ---------------------------------------------------------------------------
# The original defect (regression proof: wrapper is load-bearing)
# ---------------------------------------------------------------------------


def test_raw_build_probe_fn_through_adapter_fails_closed(_mock_ffmpeg, tmp_path):
    """Passing build_probe_fn RAW (2-arg) to the adapter reproduces the P0:
    _safe_probe calls probe_fn(upload) with one arg → TypeError → FAILED.

    This is the exact bug the wrapper fixes; if someone re-wires the router to
    ``build_probe_fn`` directly, this test stays green BUT
    ``test_wrapped_probe_through_adapter_reaches_ready_for_mode`` goes red —
    together they pin the contract.
    """
    raw_two_arg = build_probe_fn(settings=None)
    adapter = _adapter(_config(tmp_path), raw_two_arg)

    record = adapter.handle_intake(_request(), _upload(tmp_path))

    assert record.status == PreviewStatus.FAILED

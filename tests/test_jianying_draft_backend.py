"""Tests for JianyingDraftBackend integration layer (Task J4).

Covers the write() method that wraps J2 (writer) + J3 (validator) and
translates exceptions to structured JianyingDraftResult.validation_status values.

All 10 scenarios run on a clean env (no pyJianYingDraft installed).
Scenario 4 (happy path) is gated with pytest.importorskip.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.1
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from modules.output.jianying.jianying_draft_models import (
    JianyingDraftRequest,
    JianyingDraftResult,
)
from modules.output.jianying.jianying_draft_writer import JianyingEngineUnavailable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(tmp_path: Path, *, project_id: str = "test_proj") -> JianyingDraftRequest:
    """Build a minimal JianyingDraftRequest pointing at tmp_path."""
    return JianyingDraftRequest(
        project_id=project_id,
        project_title="Backend Test Project",
        source_video_path=str(tmp_path / "source.mp4"),
        dubbed_audio_path=str(tmp_path / "dubbed.wav"),
        subtitle_path=str(tmp_path / "subtitles.srt"),
        output_dir=str(tmp_path / "output"),
    )


def _make_ok_result(tmp_path: Path, project_id: str = "test_proj") -> JianyingDraftResult:
    """Build a mock successful JianyingDraftResult (pre-compatibility-report)."""
    jianying_dir = tmp_path / "output" / "jianying"
    draft_dir = jianying_dir / "draft" / project_id
    exports_dir = jianying_dir / "exports"
    draft_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)
    draft_content = draft_dir / "draft_content.json"
    draft_meta = draft_dir / "draft_meta_info.json"
    zip_path = exports_dir / f"jianying_draft_{project_id}.zip"

    # Write minimal stub files so J3 can read them
    import zipfile

    draft_content.write_text(
        json.dumps({
            "platform": {"app_version": "6.5.0"},
            "id": "draft_id_stub",
            "duration": 5000000,
            "canvas": {"width": 1920, "height": 1080},
            "fps": 30,
            "tracks": [
                {"type": "text", "name": "zh_subtitle", "segments": [{"id": "s1"}]},
            ],
        }),
        encoding="utf-8",
    )
    draft_meta.write_text(
        json.dumps({"draft_name": project_id, "draft_root_path": str(draft_dir)}),
        encoding="utf-8",
    )
    # Write a zip large enough for J3's size check
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("draft_content.json", "x" * 2000)
        zf.writestr("draft_meta_info.json", "y" * 200)

    return JianyingDraftResult(
        draft_dir=str(draft_dir),
        draft_zip_path=str(zip_path),
        draft_content_path=str(draft_content),
        draft_meta_info_path=str(draft_meta),
        manifest_path=None,
        compatibility_report_path="",  # backend will fill this
        validation_status="ok",
    )


# ---------------------------------------------------------------------------
# Scenario 1: JianyingEngineUnavailable -> skipped_no_engine
# ---------------------------------------------------------------------------


def test_engine_unavailable_produces_skipped_no_engine(tmp_path: Path) -> None:
    """Mock writer.write() raises JianyingEngineUnavailable.

    Expect: validation_status='skipped_no_engine', draft paths empty,
    compatibility_report_path set + file written.
    """
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = JianyingEngineUnavailable("pyJianYingDraft missing")

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path)

    result = backend.write(request)

    assert result.validation_status == "skipped_no_engine"
    assert result.draft_zip_path == ""
    assert result.draft_dir == ""
    assert result.draft_content_path == ""
    assert result.draft_meta_info_path == ""
    assert result.manifest_path is None

    # Compatibility report must be a real path + file must exist
    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path), (
        f"compatibility report not written: {result.compatibility_report_path}"
    )

    # Report content must document the skip reason
    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    assert report["validation_status"] in ("failed", "skipped_no_engine")
    issue_codes = [i["code"] for i in report.get("issues", [])]
    assert "skipped_no_engine" in issue_codes


# ---------------------------------------------------------------------------
# Scenario 2: Missing subtitle (FileNotFoundError) -> skipped_missing_input
# ---------------------------------------------------------------------------


def test_file_not_found_produces_skipped_missing_input(tmp_path: Path) -> None:
    """Mock writer.write() raises FileNotFoundError.

    Expect: validation_status='skipped_missing_input', draft paths empty,
    compatibility_report_path set + file written with issue recorded.
    """
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = FileNotFoundError("subtitle file not found: '/no/such/file.srt'")

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path)

    result = backend.write(request)

    assert result.validation_status == "skipped_missing_input"
    assert result.draft_zip_path == ""
    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path)

    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    issue_codes = [i["code"] for i in report.get("issues", [])]
    assert "skipped_missing_input" in issue_codes


# ---------------------------------------------------------------------------
# Scenario 3: Generic exception -> failed
# ---------------------------------------------------------------------------


def test_generic_exception_produces_failed(tmp_path: Path) -> None:
    """Mock writer.write() raises RuntimeError.

    Expect: validation_status='failed', draft paths empty,
    report records the exception details.
    """
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = RuntimeError("boom: unexpected failure")

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path)

    result = backend.write(request)

    assert result.validation_status == "failed"
    assert result.draft_zip_path == ""
    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path)

    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    issue_codes = [i["code"] for i in report.get("issues", [])]
    assert "writer_exception" in issue_codes

    # Exception type + message should appear somewhere in the issue
    messages = [i.get("message", "") for i in report.get("issues", [])]
    assert any("RuntimeError" in m for m in messages), (
        f"Expected RuntimeError in issue messages; got: {messages}"
    )


# ---------------------------------------------------------------------------
# Scenario 4: Happy path (requires pyJianYingDraft)
# ---------------------------------------------------------------------------


def test_happy_path_full_integration(tmp_path: Path) -> None:
    """Full backend write with real writer + real validator.

    Skipped if pyJianYingDraft not installed.
    """
    import struct
    import wave

    pytest.importorskip("pyJianYingDraft")

    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    # Create real WAV
    dubbed = tmp_path / "dubbed.wav"
    sample_rate = 44100
    n_samples = int(sample_rate * 2.0)
    with wave.open(str(dubbed), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            value = 16000 if (i // 50) % 2 == 0 else -16000
            wf.writeframes(struct.pack("<h", value))

    # Create real SRT
    srt = tmp_path / "subtitles.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n测试字幕\n\n",
        encoding="utf-8",
    )

    request = JianyingDraftRequest(
        project_id="happy_path_j4",
        project_title="J4 Happy Path",
        source_video_path=str(tmp_path / "missing_video.mp4"),
        dubbed_audio_path=str(dubbed),
        subtitle_path=str(srt),
        output_dir=str(tmp_path / "output"),
    )

    backend = JianyingDraftBackend(writer=JianyingDraftWriter())
    result = backend.write(request)

    assert result.validation_status == "ok"
    assert result.draft_zip_path != ""
    assert os.path.isfile(result.draft_zip_path)
    assert result.draft_content_path != ""
    assert os.path.isfile(result.draft_content_path)
    assert result.draft_meta_info_path != ""
    assert os.path.isfile(result.draft_meta_info_path)
    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path)

    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    assert report["schema_version"] == "jianying_compatibility_report_v1"


# ---------------------------------------------------------------------------
# Scenario 5: Mocked successful writer -> backend wraps with compatibility report
# ---------------------------------------------------------------------------


def test_mocked_success_sets_compatibility_report_path(tmp_path: Path) -> None:
    """When mock writer returns status='ok', backend adds compatibility_report_path."""
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    project_id = "wrap_test"
    ok_result = _make_ok_result(tmp_path, project_id=project_id)

    mock_writer = mock.MagicMock()
    mock_writer.write.return_value = ok_result

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path, project_id=project_id)

    result = backend.write(request)

    assert result.validation_status == "ok"
    # Backend must have populated compatibility_report_path
    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path), (
        f"report not written at: {result.compatibility_report_path}"
    )
    # Non-empty draft paths preserved
    assert result.draft_zip_path == ok_result.draft_zip_path


# ---------------------------------------------------------------------------
# Scenario 6: Custom engine_name passes through to skip report
# ---------------------------------------------------------------------------


def test_custom_engine_name_in_skip_report(tmp_path: Path) -> None:
    """engine_name='internal' appears in the compatibility report engine.name field."""
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = JianyingEngineUnavailable("test")

    backend = JianyingDraftBackend(writer=mock_writer, engine_name="internal")
    request = _make_request(tmp_path)

    result = backend.write(request)

    assert result.compatibility_report_path != ""
    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))
    assert report["engine"]["name"] == "internal"


# ---------------------------------------------------------------------------
# Scenario 7: Logger called on failure
# ---------------------------------------------------------------------------


def test_logger_records_error_on_exception(tmp_path: Path, caplog) -> None:
    """Generic exception causes an ERROR-level log message including the exception type."""
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = RuntimeError("disk full")

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path)

    with caplog.at_level(logging.ERROR, logger="modules.output.jianying.jianying_draft_backend"):
        backend.write(request)

    # At least one ERROR message must mention the exception type
    error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("RuntimeError" in m for m in error_messages), (
        f"Expected RuntimeError in log; got: {error_messages}"
    )


# ---------------------------------------------------------------------------
# Scenario 8: Skip report has minimal but valid schema
# ---------------------------------------------------------------------------


def test_skip_report_has_valid_schema(tmp_path: Path) -> None:
    """Skip report (skipped_no_engine) includes required schema fields.

    schema_version, engine, validation_status, materials=[], tracks=[] must all be present.
    """
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = JianyingEngineUnavailable("no library")

    backend = JianyingDraftBackend(writer=mock_writer)
    result = backend.write(_make_request(tmp_path))

    assert os.path.isfile(result.compatibility_report_path)
    report = json.loads(Path(result.compatibility_report_path).read_text(encoding="utf-8"))

    # Required top-level keys
    for key in ("schema_version", "engine", "validation_status", "materials", "tracks",
                "generated_at", "project_id"):
        assert key in report, f"Missing key in skip report: {key!r}"

    # Lists must be present and empty (no draft to inspect)
    assert isinstance(report["materials"], list)
    assert isinstance(report["tracks"], list)
    assert report["materials"] == []
    assert report["tracks"] == []

    # engine sub-object
    assert "name" in report["engine"]
    assert "version" in report["engine"]


# ---------------------------------------------------------------------------
# Scenario 9: output_dir created if missing
# ---------------------------------------------------------------------------


def test_output_dir_created_if_missing(tmp_path: Path) -> None:
    """Backend creates {output_dir}/jianying/ before writing report, even if missing."""
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    # Point output_dir at a path that does NOT yet exist
    nonexistent_output = tmp_path / "deep" / "nested" / "output"
    request = JianyingDraftRequest(
        project_id="mkdir_test",
        project_title="MkDir Test",
        source_video_path=str(tmp_path / "source.mp4"),
        dubbed_audio_path=str(tmp_path / "dubbed.wav"),
        subtitle_path=str(tmp_path / "subtitles.srt"),
        output_dir=str(nonexistent_output),
    )

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = JianyingEngineUnavailable("not installed")

    backend = JianyingDraftBackend(writer=mock_writer)
    result = backend.write(request)  # must NOT raise FileNotFoundError

    assert result.compatibility_report_path != ""
    assert os.path.isfile(result.compatibility_report_path)


# ---------------------------------------------------------------------------
# Scenario 10: Idempotent re-run
# ---------------------------------------------------------------------------


def test_idempotent_rerun(tmp_path: Path) -> None:
    """Calling backend.write(request) twice succeeds; second call overwrites report."""
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend  # noqa: PLC0415

    mock_writer = mock.MagicMock()
    mock_writer.write.side_effect = JianyingEngineUnavailable("test")

    backend = JianyingDraftBackend(writer=mock_writer)
    request = _make_request(tmp_path)

    result1 = backend.write(request)
    result2 = backend.write(request)

    # Both calls succeed and produce the same canonical report path
    assert result1.validation_status == "skipped_no_engine"
    assert result2.validation_status == "skipped_no_engine"
    assert result1.compatibility_report_path == result2.compatibility_report_path

    # Only one report file exists (overwritten, not duplicated)
    report_dir = Path(result1.compatibility_report_path).parent
    reports = list(report_dir.glob("jianying_compatibility_report*.json"))
    assert len(reports) == 1, f"Expected 1 report file, found {len(reports)}: {reports}"

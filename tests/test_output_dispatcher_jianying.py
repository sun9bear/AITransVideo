"""Tests for OutputDispatcher Jianying draft double-gate wiring (Task J6).

10 scenarios covering:
1. Gate 1a: include_jianying_draft=False -> backend never called
2. Gate 1b: service_mode != "studio" (express) -> backend never called
3. Gate 1c: service_mode=None -> backend never called
4. Gate 2a: missing editor.subtitle_cues -> backend never called
5. Gate 2b: quality_report has hard error -> backend never called
6. All gates pass -> backend called with populated JianyingDraftRequest
7. Backend returns "ok" -> all 3 artifact keys registered
8. Backend returns "skipped_no_engine" -> only compatibility_report registered
9. AVT_JIANYING_DRAFT_WIDTH/HEIGHT env override -> request has those values
10. source.original_video missing -> request.source_video_path == ""

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md (J6)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.jianying.jianying_draft_models import JianyingDraftRequest, JianyingDraftResult
from modules.output.output_dispatcher import OutputDispatcher
from modules.output.output_models import OutputRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_editor_result(tmp_path: Path) -> ProjectOutputResult:
    return ProjectOutputResult(
        dubbed_audio_path=str(tmp_path / "output" / "dubbed_audio_complete.wav"),
        ambient_audio_path=str(tmp_path / "output" / "ambient_audio.wav"),
        segments_dir=str(tmp_path / "output" / "segments"),
        segment_count=1,
        subtitles_path=str(tmp_path / "output" / "subtitles.srt"),
        subtitles_en_path=str(tmp_path / "output" / "subtitles_en.srt"),
        subtitles_bilingual_path=str(tmp_path / "output" / "subtitles_bilingual.srt"),
        background_sounds_path=str(tmp_path / "output" / "background_sounds.txt"),
        alignment_report_path=str(tmp_path / "output" / "alignment_report.md"),
        needs_review_count=0,
    )


class _FakeEditorBackend:
    """Fake editor backend that creates output dirs and returns a fixed result."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def write(self, output) -> ProjectOutputResult:
        output_dir = Path(output.output_dir) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        return _make_fake_editor_result(self._tmp_path)


class _FakeManifestWriter:
    """Fake manifest writer — does nothing, returns a dummy path."""

    def write(self, *, project_root, localized_project, artifact_index, request, output_bundle):
        manifest_path = str(project_root / "manifest.json")
        return manifest_path


def _build_localized_project(
    tmp_path: Path,
    *,
    with_metadata_title: bool = False,
    with_semantic_blocks: bool = True,
) -> LocalizedProject:
    """Minimal LocalizedProject with optional semantic blocks + 1 caption."""
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    captions = [
        SubtitleLine(
            index=0,
            start_ms=0,
            end_ms=2000,
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            en_text="Hello world.",
            cn_text="你好世界。",
        )
    ]
    semantic_blocks = []
    if with_semantic_blocks:
        semantic_blocks = [
            SemanticBlock(
                block_id="block_0001",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[0],
                first_start_ms=0,
                last_end_ms=2000,
                target_duration_ms=2000,
                merged_cn_text="你好世界。",
                actual_audio_duration_ms=2000,
                aligned_audio_path=str(aligned_audio),
                status="align_done",
            )
        ]
    source_info: dict = {
        "source_kind": "local_video",
        "source_path": str(tmp_path / "source.mp4"),
    }
    if with_metadata_title:
        source_info["metadata"] = {"video_title": "My Test Video"}

    return LocalizedProject(
        project_id="proj_jianying_test",
        source_info=source_info,
        artifacts=ArtifactIndex(),
        stage_snapshot={},
        semantic_blocks=semantic_blocks,
        aligned_blocks=semantic_blocks,
        captions=captions,
    )


def _make_quality_report_json(
    tmp_path: Path,
    *,
    validation_status: str = "passed",
    issues: list | None = None,
) -> Path:
    """Write a minimal subtitle_quality_report.json and return its path."""
    report_path = tmp_path / "subtitle_quality_report.json"
    payload = {
        "schema_version": "subtitle_quality_report_v2",
        "project_id": "proj_jianying_test",
        "validation_status": validation_status,
        "issues": issues or [],
        "block_summaries": [],
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return report_path


def _make_cues_file(tmp_path: Path) -> Path:
    """Write a minimal subtitle_cues.json and return its path."""
    cues_path = tmp_path / "subtitle_cues.json"
    payload = {
        "schema_version": "subtitle_cues_v2",
        "project_id": "proj_jianying_test",
        "cues": [
            {
                "cue_id": "cue_001",
                "block_id": "block_0001",
                "speaker_id": "speaker_a",
                "speaker_name": "Speaker A",
                "text": "你好世界。",
                "en_text": "Hello world.",
                "start_ms": 0,
                "end_ms": 2000,
                "source": "builder",
                "needs_review": False,
                "review_reason": None,
            }
        ],
    }
    cues_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cues_path


def _make_ok_jianying_result(tmp_path: Path) -> JianyingDraftResult:
    return JianyingDraftResult(
        draft_dir=str(tmp_path / "jianying" / "draft"),
        draft_zip_path=str(tmp_path / "jianying" / "draft.zip"),
        draft_content_path=str(tmp_path / "jianying" / "draft" / "draft_content.json"),
        draft_meta_info_path=str(tmp_path / "jianying" / "draft" / "draft_meta_info.json"),
        manifest_path=str(tmp_path / "jianying" / "manifest.json"),
        compatibility_report_path=str(tmp_path / "jianying" / "jianying_compatibility_report.json"),
        validation_status="ok",
    )


def _make_skip_jianying_result(tmp_path: Path, *, status: str) -> JianyingDraftResult:
    return JianyingDraftResult(
        draft_dir="",
        draft_zip_path="",
        draft_content_path="",
        draft_meta_info_path="",
        manifest_path=None,
        compatibility_report_path=str(tmp_path / "jianying" / "jianying_compatibility_report.json"),
        validation_status=status,
    )


def _make_artifact_index_with_subtitle_v2(
    tmp_path: Path,
    *,
    include_cues: bool = True,
    include_report: bool = True,
    validation_status: str = "passed",
    issues: list | None = None,
) -> ArtifactIndex:
    """Create an ArtifactIndex pre-populated with subtitle_cues + quality_report keys."""
    ai = ArtifactIndex()
    if include_cues:
        cues_path = _make_cues_file(tmp_path)
        ai.register("editor.subtitle_cues", str(cues_path))
    if include_report:
        report_path = _make_quality_report_json(
            tmp_path,
            validation_status=validation_status,
            issues=issues,
        )
        ai.register("editor.subtitle_quality_report", str(report_path))
    return ai


def _dispatch_with_gates_passing(
    tmp_path: Path,
    *,
    fake_jianying_backend,
    extra_artifact_fn=None,
    service_mode: str = "studio",
    include_jianying_draft: bool = True,
) -> tuple[OutputDispatcher, ArtifactIndex]:
    """Helper: dispatch with jianying gates set to pass, returning (dispatcher, artifact_index)."""
    project = _build_localized_project(tmp_path)
    artifact_index = _make_artifact_index_with_subtitle_v2(tmp_path)

    # Register dubbed_audio and subtitles (required by _build_jianying_request)
    dubbed_audio = tmp_path / "dubbed_audio_complete.wav"
    dubbed_audio.write_bytes(b"RIFF")
    artifact_index.register("editor.dubbed_audio_complete", str(dubbed_audio))

    subtitles = tmp_path / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:02,000\n你好世界。\n\n", encoding="utf-8")
    artifact_index.register("editor.subtitles", str(subtitles))

    if extra_artifact_fn:
        extra_artifact_fn(artifact_index, tmp_path)

    fake_editor = _FakeEditorBackend(tmp_path)
    fake_manifest = _FakeManifestWriter()

    dispatcher = OutputDispatcher(
        editor_backend=fake_editor,
        manifest_writer=fake_manifest,
        jianying_backend=fake_jianying_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=include_jianying_draft,
        service_mode=service_mode,
    )
    dispatcher.dispatch(project, artifact_index, request)
    return dispatcher, artifact_index


# ---------------------------------------------------------------------------
# Scenario 1: Gate 1a — include_jianying_draft=False -> backend never called
# ---------------------------------------------------------------------------


def test_gate1a_include_false_skips_backend(tmp_path: Path) -> None:
    """When include_jianying_draft=False, jianying backend is never invoked."""
    fake_backend = MagicMock()
    fake_backend.write.side_effect = AssertionError("jianying_backend must not be called")

    project = _build_localized_project(tmp_path)
    artifact_index = _make_artifact_index_with_subtitle_v2(tmp_path)
    fake_editor = _FakeEditorBackend(tmp_path)
    fake_manifest = _FakeManifestWriter()

    dispatcher = OutputDispatcher(
        editor_backend=fake_editor,
        manifest_writer=fake_manifest,
        jianying_backend=fake_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=False,
        service_mode="studio",
    )
    dispatcher.dispatch(project, artifact_index, request)  # should not raise

    fake_backend.write.assert_not_called()
    # No jianying artifacts registered
    assert artifact_index.get("editor.jianying_draft_dir") is None
    assert artifact_index.get("editor.jianying_draft_zip") is None
    assert artifact_index.get("editor.jianying_compatibility_report") is None


# ---------------------------------------------------------------------------
# Scenario 2: Gate 1b — service_mode="express" -> backend never called
# ---------------------------------------------------------------------------


def test_gate1b_service_mode_express_skips_backend(tmp_path: Path) -> None:
    """When service_mode='express', jianying backend is skipped."""
    fake_backend = MagicMock()

    _, artifact_index = _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
        service_mode="express",
        include_jianying_draft=True,
    )

    fake_backend.write.assert_not_called()
    assert artifact_index.get("editor.jianying_draft_dir") is None
    assert artifact_index.get("editor.jianying_compatibility_report") is None


# ---------------------------------------------------------------------------
# Scenario 3: Gate 1c — service_mode=None -> backend never called
# ---------------------------------------------------------------------------


def test_gate1c_service_mode_none_skips_backend(tmp_path: Path) -> None:
    """When service_mode=None, jianying backend is skipped."""
    fake_backend = MagicMock()

    _, artifact_index = _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
        service_mode=None,  # type: ignore[arg-type]
        include_jianying_draft=True,
    )

    fake_backend.write.assert_not_called()
    assert artifact_index.get("editor.jianying_draft_dir") is None
    assert artifact_index.get("editor.jianying_compatibility_report") is None


# ---------------------------------------------------------------------------
# Scenario 4: Gate 2a — missing editor.subtitle_cues -> backend never called
# ---------------------------------------------------------------------------


def test_gate2a_missing_subtitle_cues_skips_backend(tmp_path: Path) -> None:
    """When editor.subtitle_cues is not registered, jianying backend is skipped.

    Uses with_semantic_blocks=False to prevent the subtitle v2 pipeline from
    auto-registering editor.subtitle_cues, so our gate check can see the absence.
    """
    fake_backend = MagicMock()

    # no semantic_blocks -> cue pipeline produces no result -> won't overwrite artifact_index
    project = _build_localized_project(tmp_path, with_semantic_blocks=False)
    # Build artifact_index WITHOUT subtitle_cues
    artifact_index = _make_artifact_index_with_subtitle_v2(
        tmp_path, include_cues=False, include_report=True
    )
    fake_editor = _FakeEditorBackend(tmp_path)
    fake_manifest = _FakeManifestWriter()

    dispatcher = OutputDispatcher(
        editor_backend=fake_editor,
        manifest_writer=fake_manifest,
        jianying_backend=fake_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )
    dispatcher.dispatch(project, artifact_index, request)

    fake_backend.write.assert_not_called()
    assert artifact_index.get("editor.jianying_draft_dir") is None
    assert artifact_index.get("editor.jianying_compatibility_report") is None


# ---------------------------------------------------------------------------
# Scenario 5: Gate 2b — quality_report has hard error -> backend never called
# ---------------------------------------------------------------------------


def test_gate2b_hard_error_in_quality_report_skips_backend(tmp_path: Path) -> None:
    """When quality_report has severity='error', jianying backend is skipped.

    Uses with_semantic_blocks=False to prevent the subtitle v2 pipeline from
    overwriting our pre-registered "failed" quality report with a fresh "passed" one.
    """
    fake_backend = MagicMock()

    # no semantic_blocks -> cue pipeline produces no result -> won't overwrite artifact_index
    project = _build_localized_project(tmp_path, with_semantic_blocks=False)
    # Build artifact_index WITH cues but quality_report has a hard error
    artifact_index = _make_artifact_index_with_subtitle_v2(
        tmp_path,
        include_cues=True,
        include_report=True,
        validation_status="failed",
        issues=[
            {
                "block_id": "block_0001",
                "cue_id": None,
                "code": "text_mismatch",
                "severity": "error",
                "message": "Text mismatch.",
            }
        ],
    )
    fake_editor = _FakeEditorBackend(tmp_path)
    fake_manifest = _FakeManifestWriter()

    dispatcher = OutputDispatcher(
        editor_backend=fake_editor,
        manifest_writer=fake_manifest,
        jianying_backend=fake_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )
    dispatcher.dispatch(project, artifact_index, request)

    fake_backend.write.assert_not_called()
    assert artifact_index.get("editor.jianying_draft_dir") is None


# ---------------------------------------------------------------------------
# Scenario 6: All gates pass -> backend called once with populated request
# ---------------------------------------------------------------------------


def test_all_gates_pass_backend_called(tmp_path: Path) -> None:
    """When all gates pass, backend.write() is called exactly once with a JianyingDraftRequest."""
    ok_result = _make_ok_jianying_result(tmp_path)
    fake_backend = MagicMock()
    fake_backend.write.return_value = ok_result

    _, artifact_index = _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
    )

    fake_backend.write.assert_called_once()
    # Inspect the single call's argument
    jianying_request = fake_backend.write.call_args[0][0]
    assert isinstance(jianying_request, JianyingDraftRequest)
    assert jianying_request.project_id == "proj_jianying_test"
    # dubbed_audio and subtitle must be set
    assert jianying_request.dubbed_audio_path != ""
    assert jianying_request.subtitle_path != ""
    # output_dir must be set
    assert jianying_request.output_dir != ""


# ---------------------------------------------------------------------------
# Scenario 7: Backend returns "ok" -> all 3 artifact keys registered
# ---------------------------------------------------------------------------


def test_backend_ok_registers_all_artifact_keys(tmp_path: Path) -> None:
    """When backend returns validation_status='ok', all 3 jianying keys are registered."""
    ok_result = _make_ok_jianying_result(tmp_path)
    fake_backend = MagicMock()
    fake_backend.write.return_value = ok_result

    _, artifact_index = _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
    )

    assert artifact_index.get("editor.jianying_draft_dir") == ok_result.draft_dir
    assert artifact_index.get("editor.jianying_draft_zip") == ok_result.draft_zip_path
    assert artifact_index.get("editor.jianying_compatibility_report") == ok_result.compatibility_report_path


# ---------------------------------------------------------------------------
# Scenario 8: Backend returns "skipped_no_engine" -> only compatibility_report registered
# ---------------------------------------------------------------------------


def test_backend_skipped_no_engine_only_compatibility_report_registered(tmp_path: Path) -> None:
    """When backend returns 'skipped_no_engine', only compatibility_report is registered."""
    skip_result = _make_skip_jianying_result(tmp_path, status="skipped_no_engine")
    fake_backend = MagicMock()
    fake_backend.write.return_value = skip_result

    _, artifact_index = _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
    )

    # draft_dir and zip should NOT be registered
    assert artifact_index.get("editor.jianying_draft_dir") is None
    assert artifact_index.get("editor.jianying_draft_zip") is None
    # compatibility_report SHOULD be registered
    assert artifact_index.get("editor.jianying_compatibility_report") == skip_result.compatibility_report_path


# ---------------------------------------------------------------------------
# Scenario 9: AVT_JIANYING_DRAFT_WIDTH/HEIGHT env override
# ---------------------------------------------------------------------------


def test_env_width_height_override_passed_to_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVT_JIANYING_DRAFT_WIDTH/HEIGHT env vars are respected in JianyingDraftRequest."""
    monkeypatch.setenv("AVT_JIANYING_DRAFT_WIDTH", "3840")
    monkeypatch.setenv("AVT_JIANYING_DRAFT_HEIGHT", "2160")

    ok_result = _make_ok_jianying_result(tmp_path)
    fake_backend = MagicMock()
    fake_backend.write.return_value = ok_result

    _dispatch_with_gates_passing(tmp_path, fake_jianying_backend=fake_backend)

    jianying_request = fake_backend.write.call_args[0][0]
    assert jianying_request.width == 3840
    assert jianying_request.height == 2160


# ---------------------------------------------------------------------------
# Scenario 10: source.original_video missing -> request.source_video_path == ""
# ---------------------------------------------------------------------------


def test_missing_source_video_passes_empty_string(tmp_path: Path) -> None:
    """When source.original_video is absent, source_video_path in request is '' (not a crash)."""
    ok_result = _make_ok_jianying_result(tmp_path)
    fake_backend = MagicMock()
    fake_backend.write.return_value = ok_result

    # No extra_artifact_fn -> source.original_video not registered
    _dispatch_with_gates_passing(
        tmp_path,
        fake_jianying_backend=fake_backend,
        # explicitly leave source.original_video absent
    )

    jianying_request = fake_backend.write.call_args[0][0]
    assert jianying_request.source_video_path == ""


# ---------------------------------------------------------------------------
# Bonus: Gate 2 passes with validation_status="needs_review" and only review issues
# ---------------------------------------------------------------------------


def test_gate2_passes_when_needs_review_without_hard_errors(tmp_path: Path) -> None:
    """Gate 2 passes when quality_report is 'needs_review' with only review-severity issues."""
    ok_result = _make_ok_jianying_result(tmp_path)
    fake_backend = MagicMock()
    fake_backend.write.return_value = ok_result

    project = _build_localized_project(tmp_path)
    # Needs_review but NO severity=error issues
    artifact_index = _make_artifact_index_with_subtitle_v2(
        tmp_path,
        validation_status="needs_review",
        issues=[
            {
                "block_id": "block_0001",
                "cue_id": "cue_001",
                "code": "short_display_duration",
                "severity": "review",
                "message": "Cue is short.",
            }
        ],
    )
    dubbed_audio = tmp_path / "dubbed_audio_complete.wav"
    dubbed_audio.write_bytes(b"RIFF")
    artifact_index.register("editor.dubbed_audio_complete", str(dubbed_audio))
    subtitles = tmp_path / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:02,000\n你好世界。\n\n", encoding="utf-8")
    artifact_index.register("editor.subtitles", str(subtitles))

    fake_editor = _FakeEditorBackend(tmp_path)
    fake_manifest = _FakeManifestWriter()

    dispatcher = OutputDispatcher(
        editor_backend=fake_editor,
        manifest_writer=fake_manifest,
        jianying_backend=fake_backend,
    )
    request = OutputRequest(
        targets=[OutputTarget.EDITOR],
        output_dir=str(tmp_path),
        include_jianying_draft=True,
        service_mode="studio",
    )
    dispatcher.dispatch(project, artifact_index, request)

    # Gate should pass -> backend was called
    fake_backend.write.assert_called_once()
    assert artifact_index.get("editor.jianying_draft_dir") == ok_result.draft_dir

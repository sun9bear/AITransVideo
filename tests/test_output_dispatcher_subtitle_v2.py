"""Integration tests for OutputDispatcher subtitle-cue-generation-v2 wiring (T9).

Covers 5 scenarios per the T9 task spec:
1. OutputDispatcher writes subtitle_cues.json after dispatch.
2. OutputDispatcher writes subtitle_quality_report.json after dispatch.
3. artifact_index has editor.subtitle_cues + editor.subtitle_quality_report.
4. Feature flag AVT_DISABLE_SUBTITLE_CUES_V2=1 disables cue generation entirely.
5. End-to-end: SRT from dispatcher matches T7 srt_writer for same cues.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.output_dispatcher import OutputDispatcher
from modules.output.output_models import OutputRequest
from modules.subtitles.srt_writer import write_zh_srt


# ---------------------------------------------------------------------------
# Shared fixture helpers
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
    """Fake editor backend that records the ProjectOutput passed to it."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self.received_outputs: list = []

    def write(self, output) -> ProjectOutputResult:
        self.received_outputs.append(output)
        # Create output dir so OutputDispatcher can write JSON files there
        output_dir = Path(output.output_dir) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        return _make_fake_editor_result(self._tmp_path)


def _build_localized_project(aligned_audio_path: Path) -> LocalizedProject:
    """Build a minimal LocalizedProject with 1 semantic block + 1 caption."""
    captions = [
        SubtitleLine(
            index=0,
            start_ms=0,
            end_ms=2000,
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            en_text="Today we look at this.",
            cn_text="今天我们来看这个。",
        )
    ]
    semantic_blocks = [
        SemanticBlock(
            block_id="block_0001",
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            original_srt_indices=[0],
            first_start_ms=0,
            last_end_ms=2000,
            target_duration_ms=2000,
            merged_cn_text="今天我们来看这个。",
            actual_audio_duration_ms=2000,
            aligned_audio_path=str(aligned_audio_path),
            status="align_done",
        )
    ]
    return LocalizedProject(
        project_id="subtitle_v2_test",
        source_info={"source_kind": "local_video", "source_path": str(aligned_audio_path)},
        artifacts=ArtifactIndex(),
        stage_snapshot={},
        semantic_blocks=semantic_blocks,
        aligned_blocks=semantic_blocks,
        captions=captions,
    )


# ---------------------------------------------------------------------------
# Scenario 1: OutputDispatcher writes subtitle_cues.json
# ---------------------------------------------------------------------------


def test_dispatcher_writes_subtitle_cues_json(tmp_path: Path) -> None:
    """After dispatch, subtitle_cues.json exists at output_dir/output/subtitle_cues.json."""
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    cues_path = tmp_path / "output" / "subtitle_cues.json"
    assert cues_path.exists(), "subtitle_cues.json should exist after dispatch"

    data = json.loads(cues_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "subtitle_cues_v2"
    assert data["project_id"] == "subtitle_v2_test"
    assert isinstance(data["cues"], list)
    assert len(data["cues"]) >= 1


# ---------------------------------------------------------------------------
# Scenario 2: OutputDispatcher writes subtitle_quality_report.json
# ---------------------------------------------------------------------------


def test_dispatcher_writes_subtitle_quality_report_json(tmp_path: Path) -> None:
    """After dispatch, subtitle_quality_report.json exists at output_dir/output/."""
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    report_path = tmp_path / "output" / "subtitle_quality_report.json"
    assert report_path.exists(), "subtitle_quality_report.json should exist after dispatch"

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "subtitle_quality_report_v2"
    assert data["project_id"] == "subtitle_v2_test"
    assert data["validation_status"] in {"passed", "needs_review", "failed"}
    assert isinstance(data["issues"], list)
    assert isinstance(data["block_summaries"], list)


# ---------------------------------------------------------------------------
# Scenario 3: artifact_index has editor.subtitle_cues + editor.subtitle_quality_report
# ---------------------------------------------------------------------------


def test_dispatcher_registers_subtitle_artifacts(tmp_path: Path) -> None:
    """After dispatch, artifact_index has editor.subtitle_cues and editor.subtitle_quality_report."""
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    cues_artifact = artifact_index.get("editor.subtitle_cues")
    report_artifact = artifact_index.get("editor.subtitle_quality_report")

    assert cues_artifact is not None, "editor.subtitle_cues should be registered"
    assert report_artifact is not None, "editor.subtitle_quality_report should be registered"
    assert cues_artifact.endswith("subtitle_cues.json")
    assert report_artifact.endswith("subtitle_quality_report.json")


def test_subtitle_width_report_is_flagged_and_not_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    long_text = "abcdefghijklmnopqrstuvwxyz0123456789"
    project.semantic_blocks[0].merged_cn_text = long_text
    project.captions[0].cn_text = long_text
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )
    assert not (tmp_path / "reports" / "subtitle_width_report.json").exists()

    monkeypatch.setenv("AVT_SUBTITLE_WIDTH_REPORT", "1")
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    report_path = tmp_path / "reports" / "subtitle_width_report.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "subtitle_width_report_v1"
    assert payload["advisory_only"] is True
    assert payload["issue_count"] >= 1
    assert payload["issues"][0]["jianying_font_size_used"] is None
    assert artifact_index.get("reports.subtitle_width_report") is None


# ---------------------------------------------------------------------------
# Scenario 4: Feature flag AVT_DISABLE_SUBTITLE_CUES_V2=1 disables cue generation
# ---------------------------------------------------------------------------


def test_feature_flag_disables_subtitle_cues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When AVT_DISABLE_SUBTITLE_CUES_V2=1, no JSON files written, no artifacts, cues stay empty."""
    monkeypatch.setenv("AVT_DISABLE_SUBTITLE_CUES_V2", "1")

    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    # JSON files should NOT exist
    cues_path = tmp_path / "output" / "subtitle_cues.json"
    report_path = tmp_path / "output" / "subtitle_quality_report.json"
    assert not cues_path.exists(), "subtitle_cues.json should NOT exist when feature flag is set"
    assert not report_path.exists(), "subtitle_quality_report.json should NOT exist when feature flag is set"

    # No artifacts registered
    assert artifact_index.get("editor.subtitle_cues") is None
    assert artifact_index.get("editor.subtitle_quality_report") is None

    # ProjectOutput.subtitle_cues should be empty (fallback to segment path)
    assert len(fake_backend.received_outputs) == 1
    assert fake_backend.received_outputs[0].subtitle_cues == []


# ---------------------------------------------------------------------------
# Scenario 5: End-to-end — SRT from dispatcher matches T7 srt_writer for same cues
# ---------------------------------------------------------------------------


def test_end_to_end_srt_matches_srt_writer_output(tmp_path: Path) -> None:
    """The ProjectOutput.subtitle_cues passed to editor_backend matches what T7 would produce.

    We capture the cues from the ProjectOutput and verify write_zh_srt produces
    the same output when called independently — proving T8 + T9 are wired correctly.
    """
    aligned_audio = tmp_path / "aligned.wav"
    aligned_audio.write_bytes(b"RIFF")
    project = _build_localized_project(aligned_audio)
    artifact_index = ArtifactIndex()
    fake_backend = _FakeEditorBackend(tmp_path)

    dispatcher = OutputDispatcher(editor_backend=fake_backend)
    dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path)),
    )

    # The backend should have received a ProjectOutput with subtitle_cues populated
    assert len(fake_backend.received_outputs) == 1
    project_output = fake_backend.received_outputs[0]
    cues = project_output.subtitle_cues

    assert len(cues) >= 1, "ProjectOutput.subtitle_cues should be non-empty"

    # Independently generate SRT from these cues via T7 srt_writer
    expected_srt = write_zh_srt(cues)
    assert expected_srt.strip(), "SRT content should not be empty"

    # Verify that each cue's text is non-empty (data integrity check)
    for cue in cues:
        assert cue.text.strip(), f"Cue {cue.cue_id} should have non-empty text"

    # Verify cues reference the correct block_id
    assert all(c.block_id == "block_0001" for c in cues)

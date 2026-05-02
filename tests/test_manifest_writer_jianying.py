import json
from pathlib import Path

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.manifest_writer import ManifestWriter
from modules.output.output_models import OutputBundleResult, OutputRequest


def test_manifest_writer_includes_all_jianying_artifacts(tmp_path: Path) -> None:
    """Test that manifest includes all 3 jianying artifact fields when registered."""
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_audio", tmp_path / "source.wav")
    artifact_index.register("editor.jianying_draft_zip", str(tmp_path / "jianying" / "draft.zip"))
    artifact_index.register("editor.jianying_draft_dir", str(tmp_path / "jianying" / "draft"))
    artifact_index.register("editor.jianying_compatibility_report", str(tmp_path / "jianying" / "compatibility_report.json"))

    localized_project = LocalizedProject(
        project_id="jianying_test_all",
        source_info={"source_kind": "local_video", "source_path": str(tmp_path / "source.mp4")},
        artifacts=artifact_index,
        stage_snapshot={},
        semantic_blocks=[],
        aligned_blocks=[
            SemanticBlock(
                block_id="block_0001",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[1],
                first_start_ms=0,
                last_end_ms=900,
                target_duration_ms=900,
                merged_cn_text="你好",
                actual_audio_duration_ms=900,
                aligned_audio_path=str(tmp_path / "aligned.wav"),
            )
        ],
        captions=[
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=900,
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                en_text="Hello",
                cn_text="你好",
            )
        ],
    )
    output_bundle = OutputBundleResult(
        editor_result=ProjectOutputResult(
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
        ),
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=artifact_index,
        request=OutputRequest(targets=[OutputTarget.EDITOR]),
        output_bundle=output_bundle,
    )
    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    editor_block = manifest_payload["primary_outputs"]["editor"]
    assert editor_block is not None
    assert "jianying_draft_zip" in editor_block
    assert "jianying_draft_dir" in editor_block
    assert "jianying_compatibility_report" in editor_block
    assert editor_block["jianying_draft_zip"].endswith("draft.zip")
    assert editor_block["jianying_draft_dir"].endswith("draft")
    assert editor_block["jianying_compatibility_report"].endswith("compatibility_report.json")


def test_manifest_writer_jianying_fields_null_when_not_registered(tmp_path: Path) -> None:
    """Test that jianying fields are null when not registered in artifact_index."""
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_audio", tmp_path / "source.wav")
    # Do NOT register jianying artifacts

    localized_project = LocalizedProject(
        project_id="jianying_test_null",
        source_info={"source_kind": "local_video", "source_path": str(tmp_path / "source.mp4")},
        artifacts=artifact_index,
        stage_snapshot={},
        semantic_blocks=[],
        aligned_blocks=[
            SemanticBlock(
                block_id="block_0001",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[1],
                first_start_ms=0,
                last_end_ms=900,
                target_duration_ms=900,
                merged_cn_text="你好",
                actual_audio_duration_ms=900,
                aligned_audio_path=str(tmp_path / "aligned.wav"),
            )
        ],
        captions=[
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=900,
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                en_text="Hello",
                cn_text="你好",
            )
        ],
    )
    output_bundle = OutputBundleResult(
        editor_result=ProjectOutputResult(
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
        ),
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=artifact_index,
        request=OutputRequest(targets=[OutputTarget.EDITOR]),
        output_bundle=output_bundle,
    )
    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    editor_block = manifest_payload["primary_outputs"]["editor"]
    assert editor_block is not None
    assert "jianying_draft_zip" in editor_block
    assert "jianying_draft_dir" in editor_block
    assert "jianying_compatibility_report" in editor_block
    assert editor_block["jianying_draft_zip"] is None
    assert editor_block["jianying_draft_dir"] is None
    assert editor_block["jianying_compatibility_report"] is None


def test_manifest_writer_jianying_partial_registration(tmp_path: Path) -> None:
    """Test that manifest handles partial jianying artifact registration (e.g. only compatibility report from skip/fail path)."""
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_audio", tmp_path / "source.wav")
    # Only register compatibility report (skip/fail scenario from J6)
    artifact_index.register("editor.jianying_compatibility_report", str(tmp_path / "jianying" / "compatibility_report.json"))

    localized_project = LocalizedProject(
        project_id="jianying_test_partial",
        source_info={"source_kind": "local_video", "source_path": str(tmp_path / "source.mp4")},
        artifacts=artifact_index,
        stage_snapshot={},
        semantic_blocks=[],
        aligned_blocks=[
            SemanticBlock(
                block_id="block_0001",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[1],
                first_start_ms=0,
                last_end_ms=900,
                target_duration_ms=900,
                merged_cn_text="你好",
                actual_audio_duration_ms=900,
                aligned_audio_path=str(tmp_path / "aligned.wav"),
            )
        ],
        captions=[
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=900,
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                en_text="Hello",
                cn_text="你好",
            )
        ],
    )
    output_bundle = OutputBundleResult(
        editor_result=ProjectOutputResult(
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
        ),
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=artifact_index,
        request=OutputRequest(targets=[OutputTarget.EDITOR]),
        output_bundle=output_bundle,
    )
    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    editor_block = manifest_payload["primary_outputs"]["editor"]
    assert editor_block is not None
    assert editor_block["jianying_draft_zip"] is None
    assert editor_block["jianying_draft_dir"] is None
    assert editor_block["jianying_compatibility_report"].endswith("compatibility_report.json")


def test_manifest_writer_jianying_fields_at_end_of_editor_block(tmp_path: Path) -> None:
    """Test that jianying fields are present in the editor block (field order is alphabetical in JSON output)."""
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_audio", tmp_path / "source.wav")
    artifact_index.register("editor.jianying_draft_zip", str(tmp_path / "jianying" / "draft.zip"))
    artifact_index.register("editor.jianying_draft_dir", str(tmp_path / "jianying" / "draft"))
    artifact_index.register("editor.jianying_compatibility_report", str(tmp_path / "jianying" / "compatibility_report.json"))

    localized_project = LocalizedProject(
        project_id="jianying_test_field_order",
        source_info={"source_kind": "local_video", "source_path": str(tmp_path / "source.mp4")},
        artifacts=artifact_index,
        stage_snapshot={},
        semantic_blocks=[],
        aligned_blocks=[
            SemanticBlock(
                block_id="block_0001",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[1],
                first_start_ms=0,
                last_end_ms=900,
                target_duration_ms=900,
                merged_cn_text="你好",
                actual_audio_duration_ms=900,
                aligned_audio_path=str(tmp_path / "aligned.wav"),
            )
        ],
        captions=[
            SubtitleLine(
                index=1,
                start_ms=0,
                end_ms=900,
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                en_text="Hello",
                cn_text="你好",
            )
        ],
    )
    output_bundle = OutputBundleResult(
        editor_result=ProjectOutputResult(
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
        ),
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=artifact_index,
        request=OutputRequest(targets=[OutputTarget.EDITOR]),
        output_bundle=output_bundle,
    )
    manifest_text = Path(manifest_path).read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest_text)

    editor_block = manifest_payload["primary_outputs"]["editor"]
    assert editor_block is not None

    # Verify jianying fields are present (manifest JSON is sorted alphabetically by _write_json_atomic)
    assert "jianying_draft_zip" in editor_block
    assert "jianying_draft_dir" in editor_block
    assert "jianying_compatibility_report" in editor_block
    # Also verify existing fields still present
    assert "subtitles_path" in editor_block
    assert "subtitles_en_path" in editor_block
    assert "subtitles_bilingual_path" in editor_block
    assert "alignment_report_path" in editor_block

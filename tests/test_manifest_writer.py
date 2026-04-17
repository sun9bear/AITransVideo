import json
from pathlib import Path

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.manifest_writer import ManifestWriter
from modules.output.output_models import OutputBundleResult, OutputRequest
from modules.output.publish.publish_models import PublishResult


def test_manifest_writer_writes_minimal_manifest_payload(tmp_path: Path) -> None:
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_audio", tmp_path / "source.wav")
    artifact_index.register("working.speech_for_asr", tmp_path / "speech_for_asr.wav")
    artifact_index.register("working.ambient_audio", tmp_path / "ambient.wav")
    localized_project = LocalizedProject(
        project_id="manifest_demo",
        source_info={"source_kind": "local_video", "source_path": str(tmp_path / "source.mp4")},
        artifacts=artifact_index,
        stage_snapshot={
            "audio_preparation": {
                "status": "done",
                "payload": {
                    "execution_mode": "fresh_prepare",
                    "skipped": False,
                    "rerun_reason": "audio_preparation_cache_miss",
                },
            },
            "alignment": {
                "status": "done",
                "payload": {
                    "execution_mode": "fresh_run",
                    "fallback_applied": True,
                    "fallback_reason": "dsp_fallback",
                    "fallback_trigger": "duration_overshoot",
                },
            },
        },
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
        publish_result=PublishResult(
            project_id="manifest_demo",
            dubbed_video_path=str(tmp_path / "publish" / "dubbed_video.mp4"),
            original_video_path=str(tmp_path / "source.mp4"),
            dubbed_audio_path=str(tmp_path / "output" / "dubbed_audio_complete.wav"),
        ),
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=artifact_index,
        request=OutputRequest(targets=[OutputTarget.BOTH]),
        output_bundle=output_bundle,
    )
    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    assert Path(manifest_path).exists()
    assert manifest_payload["project_id"] == "manifest_demo"
    assert manifest_payload["requested_targets"] == ["editor", "publish"]
    assert manifest_payload["source_info"]["source_kind"] == "local_video"
    assert manifest_payload["key_audio_assets"]["source.original_audio"].endswith("source.wav")
    assert manifest_payload["primary_outputs"]["editor"]["subtitles_path"].endswith("subtitles.srt")
    assert manifest_payload["primary_outputs"]["publish"]["dubbed_video_path"].endswith("dubbed_video.mp4")
    assert manifest_payload["fallback_summary"]["alignment"]["fallback_reason"] == "dsp_fallback"


def test_manifest_writer_only_includes_fallback_relevant_stages(tmp_path: Path) -> None:
    localized_project = LocalizedProject(
        project_id="manifest_filter_demo",
        source_info={"source_kind": "local_audio", "source_path": str(tmp_path / "source.wav")},
        artifacts=ArtifactIndex(),
        stage_snapshot={
            "translation": {
                "status": "done",
                "payload": {
                    "execution_mode": "fresh_run",
                },
            },
            "audio_preparation": {
                "status": "done",
                "payload": {
                    "execution_mode": "restore",
                    "restore_reason": "cache_hit",
                },
            },
            "media_understanding": {
                "status": "failed",
                "payload": {
                    "execution_mode": "fresh_run",
                    "error_type": "missing_dependency",
                },
            },
        },
        semantic_blocks=[],
        aligned_blocks=[],
        captions=[],
    )

    manifest_path = ManifestWriter().write(
        project_root=tmp_path / "project",
        localized_project=localized_project,
        artifact_index=ArtifactIndex(),
        request=OutputRequest(targets=[OutputTarget.EDITOR]),
        output_bundle=OutputBundleResult(),
    )
    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    assert manifest_payload["requested_targets"] == ["editor"]
    assert manifest_payload["primary_outputs"]["editor"] is None
    assert manifest_payload["primary_outputs"]["publish"] is None
    assert "translation" not in manifest_payload["fallback_summary"]
    assert manifest_payload["fallback_summary"]["audio_preparation"]["restore_reason"] == "cache_hit"
    assert manifest_payload["fallback_summary"]["media_understanding"]["error_type"] == "missing_dependency"

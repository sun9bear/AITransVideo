import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.artifact_index import ArtifactIndex
from core.enums import OutputTarget
from core.exceptions import PublishError
from core.models import SemanticBlock, SubtitleLine
from core.project_model import LocalizedProject
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.output_dispatcher import OutputDispatcher
from modules.output.output_models import OutputRequest
from modules.output.publish.publish_models import PublishResult


def _build_localized_project(source_audio_path: Path) -> LocalizedProject:
    captions = [
        SubtitleLine(
            index=1,
            start_ms=0,
            end_ms=900,
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            en_text="Hello",
            cn_text="浣犲ソ",
        )
    ]
    aligned_blocks = [
        SemanticBlock(
            block_id="block_0001",
            speaker_id="speaker_a",
            speaker_name="Speaker A",
            original_srt_indices=[1],
            first_start_ms=0,
            last_end_ms=900,
            target_duration_ms=900,
            merged_cn_text="浣犲ソ",
            actual_audio_duration_ms=900,
            aligned_audio_path=str(source_audio_path),
            status="align_done",
        )
    ]
    return LocalizedProject(
        project_id="dispatcher_demo",
        source_info={"source_kind": "local_video", "source_path": str(source_audio_path)},
        artifacts=ArtifactIndex(),
        stage_snapshot={"draft": {"status": "done"}},
        semantic_blocks=list(aligned_blocks),
        aligned_blocks=aligned_blocks,
        captions=captions,
    )


def test_output_request_expands_both_target() -> None:
    request = OutputRequest(targets=[OutputTarget.BOTH])

    assert request.expanded_targets() == (OutputTarget.EDITOR, OutputTarget.PUBLISH)


def test_output_dispatcher_routes_both_targets_and_registers_outputs(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned.wav"
    source_audio_path.write_bytes(b"RIFF")
    project = _build_localized_project(source_audio_path)
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_video", tmp_path / "original.mp4")

    class FakeEditorBackend:
        def __init__(self) -> None:
            self.outputs = []

        def write(self, output) -> ProjectOutputResult:
            self.outputs.append(output)
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=0,
            )

    class FakePublishBackend:
        def __init__(self) -> None:
            self.requests = []

        def publish(self, request) -> PublishResult:
            self.requests.append(request)
            return PublishResult(
                project_id=request.project_id,
                dubbed_video_path=str(tmp_path / "project" / "publish" / "dubbed_video.mp4"),
                original_video_path=request.original_video_path,
                dubbed_audio_path=request.dubbed_audio_path,
            )

    editor_backend = FakeEditorBackend()
    publish_backend = FakePublishBackend()
    dispatcher = OutputDispatcher(editor_backend=editor_backend, publish_backend=publish_backend)

    result = dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.BOTH], output_dir=str(tmp_path / "project")),
    )

    assert len(editor_backend.outputs) == 1
    assert editor_backend.outputs[0].project_id == "dispatcher_demo"
    assert editor_backend.outputs[0].output_dir == str((tmp_path / "project").resolve(strict=False))
    assert editor_backend.outputs[0].segments[0].aligned_audio_path == str(source_audio_path)
    assert len(publish_backend.requests) == 1
    assert publish_backend.requests[0].dubbed_audio_path == result.editor_result.dubbed_audio_path
    assert publish_backend.requests[0].original_video_path == str(tmp_path / "original.mp4")
    assert result.editor_result is not None
    assert result.publish_result is not None
    assert result.manifest_path is not None
    assert Path(result.manifest_path).exists()
    manifest_payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest_payload["requested_targets"] == ["editor", "publish"]
    assert manifest_payload["primary_outputs"]["publish"]["dubbed_video_path"].endswith("dubbed_video.mp4")
    assert artifact_index.require("editor.dubbed_audio_complete") == result.editor_result.dubbed_audio_path
    assert artifact_index.require("publish.dubbed_video") == result.publish_result.dubbed_video_path
    assert artifact_index.require("manifest.root") == result.manifest_path


def test_output_dispatcher_resolves_project_root_from_draft_artifact(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned.wav"
    source_audio_path.write_bytes(b"RIFF")
    project = _build_localized_project(source_audio_path)
    artifact_index = ArtifactIndex()
    artifact_index.register("editor.draft_dir", tmp_path / "project" / "draft")

    class FakeEditorBackend:
        def __init__(self) -> None:
            self.outputs = []

        def write(self, output) -> ProjectOutputResult:
            self.outputs.append(output)
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=0,
            )

    dispatcher = OutputDispatcher(editor_backend=FakeEditorBackend())
    result = dispatcher.dispatch(project, artifact_index, OutputRequest(targets=[OutputTarget.EDITOR]))

    assert result.editor_result is not None
    assert result.manifest_path is not None
    assert artifact_index.require("editor.subtitles").endswith("subtitles.srt")
    assert artifact_index.require("manifest.root") == result.manifest_path


def test_output_dispatcher_editor_only_writes_manifest_without_publish_output(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned.wav"
    source_audio_path.write_bytes(b"RIFF")
    project = _build_localized_project(source_audio_path)

    class FakeEditorBackend:
        def write(self, output) -> ProjectOutputResult:
            del output
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=0,
            )

    dispatcher = OutputDispatcher(editor_backend=FakeEditorBackend())

    result = dispatcher.dispatch(
        project,
        ArtifactIndex(),
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path / "project")),
    )

    assert result.editor_result is not None
    assert result.publish_result is None
    assert result.manifest_path is not None
    manifest_payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest_payload["requested_targets"] == ["editor"]
    assert manifest_payload["primary_outputs"]["editor"]["subtitles_path"].endswith("subtitles.srt")
    assert manifest_payload["primary_outputs"]["publish"] is None


def test_output_dispatcher_publish_only_still_registers_editor_and_publish_outputs(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned.wav"
    source_audio_path.write_bytes(b"RIFF")
    project = _build_localized_project(source_audio_path)
    artifact_index = ArtifactIndex()
    artifact_index.register("source.original_video", tmp_path / "original.mp4")

    class FakeEditorBackend:
        def __init__(self) -> None:
            self.call_count = 0

        def write(self, output) -> ProjectOutputResult:
            del output
            self.call_count += 1
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=0,
            )

    class FakePublishBackend:
        def __init__(self) -> None:
            self.call_count = 0

        def publish(self, request) -> PublishResult:
            self.call_count += 1
            return PublishResult(
                project_id=request.project_id,
                dubbed_video_path=str(tmp_path / "project" / "publish" / "dubbed_video.mp4"),
                original_video_path=request.original_video_path,
                dubbed_audio_path=request.dubbed_audio_path,
            )

    editor_backend = FakeEditorBackend()
    publish_backend = FakePublishBackend()
    dispatcher = OutputDispatcher(editor_backend=editor_backend, publish_backend=publish_backend)

    result = dispatcher.dispatch(
        project,
        artifact_index,
        OutputRequest(targets=[OutputTarget.PUBLISH], output_dir=str(tmp_path / "project")),
    )

    assert editor_backend.call_count == 1
    assert publish_backend.call_count == 1
    assert result.editor_result is not None
    assert result.publish_result is not None
    assert result.manifest_path is not None
    manifest_payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest_payload["requested_targets"] == ["publish"]
    assert manifest_payload["primary_outputs"]["editor"]["dubbed_audio_path"].endswith("dubbed_audio_complete.wav")
    assert manifest_payload["primary_outputs"]["publish"]["dubbed_video_path"].endswith("dubbed_video.mp4")
    assert artifact_index.require("editor.dubbed_audio_complete") == result.editor_result.dubbed_audio_path
    assert artifact_index.require("publish.dubbed_video") == result.publish_result.dubbed_video_path
    assert artifact_index.require("manifest.root") == result.manifest_path


def test_output_dispatcher_preserves_explicit_segment_id_and_alignment_method(tmp_path: Path) -> None:
    aligned_audio_path = tmp_path / "aligned.wav"
    aligned_audio_path.write_bytes(b"RIFF")

    @dataclass(slots=True)
    class LegacyAlignedBlock:
        segment_id: int
        block_id: str
        speaker_id: str
        speaker_name: str | None
        original_srt_indices: list[int]
        first_start_ms: int
        last_end_ms: int
        target_duration_ms: int
        merged_cn_text: str
        actual_audio_duration_ms: int
        aligned_audio_path: str
        tts_audio_path: str | None
        status: str
        alignment_method: str
        needs_review: bool

    class FakeEditorBackend:
        def __init__(self) -> None:
            self.outputs = []

        def write(self, output) -> ProjectOutputResult:
            self.outputs.append(output)
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=1,
            )

    project = LocalizedProject(
        project_id="dispatcher_legacy_process",
        source_info={"source_kind": "youtube_url", "locator": "https://youtube.example/watch?v=legacy"},
        artifacts=ArtifactIndex(),
        stage_snapshot={"legacy_process_output": {"status": "done"}},
        semantic_blocks=[],
        aligned_blocks=[
            LegacyAlignedBlock(
                segment_id=42,
                block_id="segment_042",
                speaker_id="speaker_a",
                speaker_name="Speaker A",
                original_srt_indices=[42],
                first_start_ms=0,
                last_end_ms=1200,
                target_duration_ms=1200,
                merged_cn_text="legacy text",
                actual_audio_duration_ms=1200,
                aligned_audio_path=str(aligned_audio_path.resolve(strict=False)),
                tts_audio_path=None,
                status="align_done_fallback",
                alignment_method="rewrite_dsp",
                needs_review=True,
            )
        ],
        captions=[],
    )
    editor_backend = FakeEditorBackend()
    result = OutputDispatcher(editor_backend=editor_backend).dispatch(
        project,
        ArtifactIndex(),
        OutputRequest(targets=[OutputTarget.EDITOR], output_dir=str(tmp_path / "project")),
    )

    assert result.editor_result is not None
    assert len(editor_backend.outputs) == 1
    assert editor_backend.outputs[0].segments[0].segment_id == 42
    assert editor_backend.outputs[0].segments[0].alignment_method == "rewrite_dsp"
    assert editor_backend.outputs[0].segments[0].needs_review is True


def test_output_dispatcher_publish_requires_original_video_artifact(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned.wav"
    source_audio_path.write_bytes(b"RIFF")
    project = _build_localized_project(source_audio_path)

    class FakeEditorBackend:
        def write(self, output) -> ProjectOutputResult:
            del output
            return ProjectOutputResult(
                dubbed_audio_path=str(tmp_path / "project" / "output" / "dubbed_audio_complete.wav"),
                ambient_audio_path=str(tmp_path / "project" / "output" / "ambient_audio.wav"),
                segments_dir=str(tmp_path / "project" / "output" / "segments"),
                segment_count=1,
                subtitles_path=str(tmp_path / "project" / "output" / "subtitles.srt"),
                subtitles_en_path=str(tmp_path / "project" / "output" / "subtitles_en.srt"),
                subtitles_bilingual_path=str(tmp_path / "project" / "output" / "subtitles_bilingual.srt"),
                background_sounds_path=str(tmp_path / "project" / "output" / "background_sounds.txt"),
                alignment_report_path=str(tmp_path / "project" / "output" / "alignment_report.md"),
                needs_review_count=0,
            )

    dispatcher = OutputDispatcher(editor_backend=FakeEditorBackend())

    with pytest.raises(PublishError, match="source.original_video"):
        dispatcher.dispatch(
            project,
            ArtifactIndex(),
            OutputRequest(targets=[OutputTarget.PUBLISH], output_dir=str(tmp_path / "project")),
        )

from pathlib import Path

from pydub import AudioSegment

from modules.output.editor.editor_package_backend import EditorPackageBackend
from modules.output.project_output import AlignedSegment, ProjectOutput


def _export_silent_wav(path: Path, *, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    AudioSegment.silent(duration=duration_ms).export(path, format="wav")
    return path


def test_editor_package_backend_writes_editor_output_bundle(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "segment.wav", duration_ms=1_000)
    project_dir = tmp_path / "editor_backend_project"
    project_dir.mkdir(parents=True, exist_ok=True)

    output = ProjectOutput(
        project_id="editor_backend_project",
        youtube_url="https://youtube.example/watch?v=editor-backend",
        video_title="Editor Backend Demo",
        total_duration_ms=1_500,
        segments=[
            AlignedSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_000,
                cn_text="Editor backend output.",
                en_text="Editor backend output.",
                aligned_audio_path=str(source_audio),
                actual_duration_ms=1_000,
                alignment_method="direct",
                needs_review=False,
            )
        ],
        output_dir=str(project_dir),
    )

    result = EditorPackageBackend().write(output)

    assert Path(result.dubbed_audio_path).exists()
    assert Path(result.ambient_audio_path).exists()
    assert Path(result.subtitles_path).exists()
    assert Path(result.alignment_report_path).exists()
    assert Path(result.segments_dir).exists()
    assert result.segment_count == 1
    assert result.needs_review_count == 0

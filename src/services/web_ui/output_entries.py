from __future__ import annotations

from pathlib import Path

from services.manifest_reader import (
    load_manifest_artifact_index,
    resolve_manifest_artifact_path,
)

RESULT_DOWNLOAD_KEY_MANIFEST = "manifest.file"
PUBLIC_RESULT_DOWNLOAD_KEYS = frozenset(
    {
        RESULT_DOWNLOAD_KEY_MANIFEST,
        "translation.segments",
        "editor.subtitles",
        "editor.dubbed_audio_complete",
        "publish.dubbed_video",
    }
)


def _resolve_artifact_path(
    project_dir: Path,
    artifact_key: str,
    *,
    artifact_index: dict[str, str] | None = None,
) -> Path | None:
    return resolve_manifest_artifact_path(
        project_dir,
        artifact_key,
        artifact_index=artifact_index,
    )


def _build_output_entry_from_artifact(
    label: str,
    *,
    project_dir: Path,
    artifact_index: dict[str, str],
    artifact_key: str,
    fallback_path: Path,
) -> dict[str, object]:
    artifact_path = _resolve_artifact_path(
        project_dir,
        artifact_key,
        artifact_index=artifact_index,
    )
    return _build_output_entry(
        label,
        artifact_path or fallback_path,
        download_key=artifact_key if artifact_path is not None else None,
    )


def _resolve_review_state_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "state.review") or (
        project_dir / "review_state.json"
    ).resolve(strict=False)


def _resolve_transcript_structured_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "media.transcript_structured") or (
        project_dir / "transcript" / "transcript.json"
    ).resolve(strict=False)


def _resolve_translation_segments_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "translation.segments") or (
        project_dir / "translation" / "segments.json"
    ).resolve(strict=False)


def _build_editor_output_entries(project_dir: Path) -> list[dict[str, object]]:
    artifact_index = load_manifest_artifact_index(project_dir=project_dir)
    output_dir = project_dir / "output"
    return [
        _build_output_entry("项目目录", project_dir),
        _build_output_entry("输出目录", output_dir),
        _build_output_entry_from_artifact(
            "完整配音",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.dubbed_audio_complete",
            fallback_path=output_dir / "dubbed_audio_complete.wav",
        ),
        _build_output_entry_from_artifact(
            "环境音轨",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.ambient_audio",
            fallback_path=output_dir / "ambient_audio.wav",
        ),
        _build_output_entry_from_artifact(
            "字幕文件",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.subtitles",
            fallback_path=output_dir / "subtitles.srt",
        ),
        _build_output_entry_from_artifact(
            "分段音频目录",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.segments_dir",
            fallback_path=output_dir / "segments",
        ),
        _build_output_entry_from_artifact(
            "对齐报告",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.alignment_report",
            fallback_path=output_dir / "alignment_report.md",
        ),
        _build_output_entry("背景音说明", output_dir / "background_sounds.txt"),
        _build_output_entry_from_artifact(
            "翻译分段",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="translation.segments",
            fallback_path=project_dir / "translation" / "segments.json",
        ),
    ]


def _build_publish_output_entries(project_dir: Path) -> list[dict[str, object]]:
    artifact_index = load_manifest_artifact_index(project_dir=project_dir)
    return [
        _build_output_entry(
            "Manifest",
            project_dir / "manifest.json",
            download_key=RESULT_DOWNLOAD_KEY_MANIFEST,
        ),
        _build_output_entry("发布目录", project_dir / "publish"),
        _build_output_entry_from_artifact(
            "成品视频",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="publish.dubbed_video",
            fallback_path=project_dir / "publish" / "dubbed_video.mp4",
        ),
    ]


def _build_output_entry(label: str, path: Path, *, download_key: str | None = None) -> dict[str, object]:
    resolved_path = path.resolve(strict=False)
    exists = resolved_path.exists()
    return {
        "label": label,
        "path": str(resolved_path) if exists else None,
        "exists": exists,
        "download_key": download_key if exists and download_key in PUBLIC_RESULT_DOWNLOAD_KEYS else None,
    }

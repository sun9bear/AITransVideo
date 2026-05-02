"""Tests for JianyingDraftValidator (Task J3).

Pure on-disk inspection validator that produces jianying_compatibility_report.json.
Tests run on clean env (no pyJianYingDraft import) using synthetic fixture drafts.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md (J3)
"""

import json
from pathlib import Path

import pytest

from modules.output.jianying.jianying_draft_models import JianyingDraftRequest
from modules.output.jianying.jianying_draft_validator import write_compatibility_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_synthetic_draft(
    tmp_path: Path,
    *,
    has_draft_content: bool = True,
    has_draft_meta: bool = True,
    create_materials_dir: bool = True,
    materials: dict[str, tuple[str, int]] | None = None,
    draft_content_json: dict | None = None,
    draft_meta_json: dict | None = None,
    tracks: list[dict] | None = None,
    text_track_segments: int = 1,
) -> tuple[Path, Path, Path]:
    """Create a synthetic draft directory structure.

    Returns (draft_dir, materials_dir, draft_zip_path).
    materials: dict[key] = (filename, size_bytes). If None, creates defaults.
    draft_content_json: full JSON structure. If None, uses defaults with tracks.
    draft_meta_json: full JSON structure. If None, uses defaults.
    """
    draft_dir = tmp_path / "synthetic_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)

    materials_dir = draft_dir / "materials" if create_materials_dir else None
    if create_materials_dir:
        materials_dir.mkdir(exist_ok=True)

    # Create material files (with random-like data to defeat compression)
    if materials:
        for key, (filename, size_bytes) in materials.items():
            if materials_dir:
                mat_file = materials_dir / filename
                # Create pseudo-random data to avoid compression
                import hashlib
                data = b""
                for i in range(0, size_bytes, 32):
                    chunk = hashlib.sha256(str(i).encode()).digest()
                    data += chunk
                data = data[:size_bytes]
                mat_file.write_bytes(data)

    # Create draft_content.json
    if has_draft_content:
        if draft_content_json is None:
            default_tracks = tracks or [
                {
                    "type": "video",
                    "name": "video_main",
                    "segments": [{"id": "seg_1"}],
                },
                {
                    "type": "audio",
                    "name": "dubbed_audio",
                    "segments": [{"id": "seg_2"}],
                },
            ]
            # Add text track with configurable segment count
            text_segments = [{"id": f"seg_text_{i}"} for i in range(text_track_segments)]
            default_tracks.append({
                "type": "text",
                "name": "zh_subtitle",
                "segments": text_segments,
            })

            draft_content_json = {
                "platform": {
                    "app_version": "6.5.0",
                },
                "id": "draft_id_12345",
                "duration": 120000000,  # in microseconds
                "canvas": {
                    "width": 1920,
                    "height": 1080,
                },
                "fps": 30,
                "tracks": default_tracks,
            }

        (draft_dir / "draft_content.json").write_text(
            json.dumps(draft_content_json, ensure_ascii=False),
            encoding="utf-8",
        )

    # Create draft_meta_info.json
    if has_draft_meta:
        if draft_meta_json is None:
            draft_meta_json = {
                "draft_name": "test_draft",
                "draft_root_path": str(draft_dir),
            }
        (draft_dir / "draft_meta_info.json").write_text(
            json.dumps(draft_meta_json, ensure_ascii=False),
            encoding="utf-8",
        )

    # Create zip file with materials to ensure it's large enough (>= 1 KB)
    import zipfile
    zip_path = tmp_path / "jianying_draft_test.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("draft_content.json", json.dumps(draft_content_json or {}, ensure_ascii=False))
        if draft_meta_json:
            zf.writestr("draft_meta_info.json", json.dumps(draft_meta_json, ensure_ascii=False))
        if materials_dir and materials_dir.is_dir():
            for mat_file in materials_dir.iterdir():
                if mat_file.is_file():
                    zf.write(mat_file, mat_file.name)

    return draft_dir, materials_dir or draft_dir / "materials", zip_path


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------


def test_happy_path(tmp_path: Path) -> None:
    """Minimal valid draft: all required files, passed validation."""
    draft_dir, materials_dir, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "source_video": ("source_video.mp4", 50000),
            "dubbed_audio": ("dubbed_audio.wav", 100000),
        },
    )

    request = JianyingDraftRequest(
        project_id="proj_001",
        project_title="Test Project",
        source_video_path=str(draft_dir / "materials" / "source_video.mp4"),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["schema_version"] == "jianying_compatibility_report_v1"
    assert report["project_id"] == "proj_001"
    assert report["engine"]["name"] == "pyJianYingDraft"
    assert report["validation_status"] == "passed"
    assert report["issues"] == []
    assert report["draft"]["platform_app_version"] == "6.5.0"
    assert report["draft"]["draft_id"] == "draft_id_12345"
    assert len(report["tracks"]) == 3  # video, audio, text
    assert report["draft_zip_size_bytes"] > 0


# ---------------------------------------------------------------------------
# Scenario 2: Missing source_video material
# ---------------------------------------------------------------------------


def test_missing_source_video_material(tmp_path: Path) -> None:
    """Draft generated but source_video.mp4 not in materials.

    This occurs when source_video_path exists on disk but the writer failed to
    copy it into materials/. The validator detects this discrepancy.
    """
    draft_dir, materials_dir, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "dubbed_audio": ("dubbed_audio.wav", 100000),
        },
    )

    # Create a source video file outside materials/ to simulate the writer
    # having a path but failing to copy it
    source_video_file = tmp_path / "source_video.mp4"
    import hashlib
    data = hashlib.sha256(b"source").digest() * 2000  # ~64KB
    source_video_file.write_bytes(data)

    request = JianyingDraftRequest(
        project_id="proj_002",
        project_title="Test Project",
        source_video_path=str(source_video_file),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "needs_review"
    issues = [i for i in report["issues"] if i["code"] == "missing_video_material"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 3: Missing ambient_audio material
# ---------------------------------------------------------------------------


def test_missing_ambient_audio_material(tmp_path: Path) -> None:
    """Request had ambient_audio_path, but no ambient_audio in materials.

    This occurs when ambient_audio_path exists on disk but the writer failed
    to copy it into materials/.
    """
    draft_dir, materials_dir, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "source_video": ("source_video.mp4", 50000),
            "dubbed_audio": ("dubbed_audio.wav", 100000),
        },
    )

    # Create an ambient audio file to simulate writer having a path but failing to copy
    ambient_audio_file = tmp_path / "ambient_audio.wav"
    import hashlib
    data = hashlib.sha256(b"ambient").digest() * 1000  # ~32KB
    ambient_audio_file.write_bytes(data)

    request = JianyingDraftRequest(
        project_id="proj_003",
        project_title="Test Project",
        source_video_path=str(draft_dir / "materials" / "source_video.mp4"),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
        ambient_audio_path=str(ambient_audio_file),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "needs_review"
    issues = [i for i in report["issues"] if i["code"] == "missing_ambient_material"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 4: Empty text track (0 segments)
# ---------------------------------------------------------------------------


def test_empty_text_track(tmp_path: Path) -> None:
    """Text track exists but has 0 segments."""
    draft_dir, materials_dir, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "source_video": ("source_video.mp4", 12345),
            "dubbed_audio": ("dubbed_audio.wav", 67890),
        },
        text_track_segments=0,
    )

    request = JianyingDraftRequest(
        project_id="proj_004",
        project_title="Test Project",
        source_video_path=str(draft_dir / "materials" / "source_video.mp4"),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "needs_review"
    issues = [i for i in report["issues"] if i["code"] == "empty_text_track"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 5: Malformed draft_content.json
# ---------------------------------------------------------------------------


def test_malformed_draft_content_json(tmp_path: Path) -> None:
    """draft_content.json contains invalid JSON."""
    draft_dir = tmp_path / "synthetic_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    materials_dir = draft_dir / "materials"
    materials_dir.mkdir()

    # Write invalid JSON
    (draft_dir / "draft_content.json").write_text('{"invalid": json}', encoding="utf-8")
    (draft_dir / "draft_meta_info.json").write_text(
        json.dumps({"draft_name": "test"}),
        encoding="utf-8",
    )

    import zipfile
    zip_path = tmp_path / "jianying_draft_test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("draft_content.json", '{"invalid": json}')

    request = JianyingDraftRequest(
        project_id="proj_005",
        project_title="Test Project",
        source_video_path="/fake/source.mp4",
        dubbed_audio_path="/fake/audio.wav",
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "failed"
    issues = [i for i in report["issues"] if i["code"] == "malformed_draft_content"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 6: Missing zip file
# ---------------------------------------------------------------------------


def test_zip_missing(tmp_path: Path) -> None:
    """Zip file does not exist."""
    draft_dir, _, _ = _make_synthetic_draft(tmp_path)

    request = JianyingDraftRequest(
        project_id="proj_006",
        project_title="Test Project",
        source_video_path="/fake/source.mp4",
        dubbed_audio_path="/fake/audio.wav",
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)
    zip_path = tmp_path / "nonexistent.zip"

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "failed"
    issues = [i for i in report["issues"] if i["code"] == "zip_missing"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 7: Zip too small (sanity check)
# ---------------------------------------------------------------------------


def test_zip_too_small(tmp_path: Path) -> None:
    """Zip file exists but is too small (< 1KB)."""
    draft_dir, _, _ = _make_synthetic_draft(tmp_path)

    zip_path = tmp_path / "tiny.zip"
    zip_path.write_bytes(b"x" * 100)

    request = JianyingDraftRequest(
        project_id="proj_007",
        project_title="Test Project",
        source_video_path="/fake/source.mp4",
        dubbed_audio_path="/fake/audio.wav",
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "failed"
    issues = [i for i in report["issues"] if i["code"] == "zip_too_small"]
    assert len(issues) == 1


# ---------------------------------------------------------------------------
# Scenario 8: Engine version detection
# ---------------------------------------------------------------------------


def test_engine_version_detected(tmp_path: Path) -> None:
    """Engine name is always pyJianYingDraft; version may be None or string."""
    draft_dir, _, zip_path = _make_synthetic_draft(tmp_path)

    request = JianyingDraftRequest(
        project_id="proj_008",
        project_title="Test Project",
        source_video_path="/fake/source.mp4",
        dubbed_audio_path="/fake/audio.wav",
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["engine"]["name"] == "pyJianYingDraft"
    # version is either a string or null
    assert report["engine"]["version"] is None or isinstance(report["engine"]["version"], str)


# ---------------------------------------------------------------------------
# Scenario 9: No ambient in request → not flagged
# ---------------------------------------------------------------------------


def test_no_ambient_not_flagged(tmp_path: Path) -> None:
    """Request has no ambient_audio_path; missing ambient is not reported."""
    draft_dir, _, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "source_video": ("source_video.mp4", 12345),
            "dubbed_audio": ("dubbed_audio.wav", 67890),
        },
    )

    request = JianyingDraftRequest(
        project_id="proj_009",
        project_title="Test Project",
        source_video_path=str(draft_dir / "materials" / "source_video.mp4"),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
        ambient_audio_path=None,
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["validation_status"] == "passed"
    assert report["issues"] == []


# ---------------------------------------------------------------------------
# Scenario 10: Material size_bytes accurate
# ---------------------------------------------------------------------------


def test_material_size_bytes_accurate(tmp_path: Path) -> None:
    """For existing materials, size_bytes matches actual file size."""
    draft_dir, _, zip_path = _make_synthetic_draft(
        tmp_path,
        materials={
            "source_video": ("source_video.mp4", 50000),
            "dubbed_audio": ("dubbed_audio.wav", 100000),
        },
    )

    request = JianyingDraftRequest(
        project_id="proj_010",
        project_title="Test Project",
        source_video_path=str(draft_dir / "materials" / "source_video.mp4"),
        dubbed_audio_path=str(draft_dir / "materials" / "dubbed_audio.wav"),
        subtitle_path="/fake/path.srt",
        output_dir=str(tmp_path),
    )

    output_root = tmp_path / "jianying"
    output_root.mkdir(exist_ok=True)

    report_path = write_compatibility_report(
        request=request,
        draft_dir=draft_dir,
        draft_zip_path=zip_path,
        output_root=output_root,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    materials = {m["key"]: m for m in report["materials"]}
    if materials["source_video"]["found"]:
        assert materials["source_video"]["size_bytes"] == 50000
    if materials["dubbed_audio"]["found"]:
        assert materials["dubbed_audio"]["size_bytes"] == 100000

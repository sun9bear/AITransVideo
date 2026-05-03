"""Tests for JianyingDraftWriter + _PyJianYingDraftAdapter (Task J2).

Scenarios that do NOT require pyJianYingDraft are placed before the
pytest.importorskip guard and will run on any env (even clean envs without
the optional dependency).

Scenarios 2-10 require pyJianYingDraft and are automatically skipped if the
library is not installed.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.2
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import wave
import zipfile
from pathlib import Path
from typing import Generator
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Scenarios that do NOT need pyJianYingDraft installed
# ---------------------------------------------------------------------------


def test_jianying_engine_unavailable_raised_when_import_fails():
    """_PyJianYingDraftAdapter raises JianyingEngineUnavailable when
    pyJianYingDraft cannot be imported.  Does NOT require the library."""
    # Patch sys.modules to simulate ImportError for pyJianYingDraft.
    # We must also ensure the module itself is freshly imported so the
    # lazy-import path is exercised.
    with mock.patch.dict(sys.modules, {"pyJianYingDraft": None}):
        # Force re-import of the writer module so the lazy path is clean.
        mod_name = "modules.output.jianying.jianying_draft_writer"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
            JianyingEngineUnavailable,
            _PyJianYingDraftAdapter,
        )

        with pytest.raises(JianyingEngineUnavailable, match="pyJianYingDraft"):
            _PyJianYingDraftAdapter(width=1920, height=1080)


def test_jianying_engine_unavailable_is_exception():
    """JianyingEngineUnavailable is an Exception subclass."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        JianyingEngineUnavailable,
    )

    assert issubclass(JianyingEngineUnavailable, Exception)


def test_project_id_sanitization_removes_special_chars():
    """_sanitize_draft_name replaces /, \\, : with underscores."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _sanitize_draft_name,
    )

    assert _sanitize_draft_name("job/abc:123\\sub") == "job_abc_123_sub"
    assert _sanitize_draft_name("plain_name") == "plain_name"
    assert _sanitize_draft_name("job/abc:123") == "job_abc_123"


# ---------------------------------------------------------------------------
# K11: _make_material_paths_absolute helper (no pyJianYingDraft needed)
# ---------------------------------------------------------------------------


def _write_draft_content(path: str, videos=None, audios=None) -> None:
    """Write a minimal draft_content.json at *path* for helper tests."""
    data = {
        "materials": {
            "videos": videos or [],
            "audios": audios or [],
        }
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_make_material_paths_absolute_windows_style(tmp_path):
    """_make_material_paths_absolute rewrites paths with backslashes for Windows input (K11)."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed_audio.wav", "id": "a1"}],
    )

    user_draft_root = r"F:\剪映缓存\草稿\JianyingPro Drafts"
    draft_name = "my_draft"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, draft_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    audios = data["materials"]["audios"]
    assert len(audios) == 1
    expected = r"F:\剪映缓存\草稿\JianyingPro Drafts\jianying_draft_my_draft\materials\dubbed_audio.wav"
    assert audios[0]["path"] == expected, f"got: {audios[0]['path']!r}"
    # No backslash in expected should appear as forward slash
    assert "/" not in audios[0]["path"], "Windows path must use backslashes only"


def test_make_material_paths_absolute_unix_style(tmp_path):
    """_make_material_paths_absolute rewrites paths with forward-slashes for Unix input (K11)."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed_audio.wav", "id": "a1"}],
    )

    user_draft_root = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
    draft_name = "my_draft"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, draft_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    audios = data["materials"]["audios"]
    assert len(audios) == 1
    expected = (
        "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
        "/jianying_draft_my_draft/materials/dubbed_audio.wav"
    )
    assert audios[0]["path"] == expected, f"got: {audios[0]['path']!r}"
    assert "\\" not in audios[0]["path"], "Unix path must not contain backslashes"


def test_make_material_paths_absolute_rewrites_video_media_path(tmp_path):
    """_make_material_paths_absolute also rewrites media_path on video materials (K11)."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        videos=[{
            "path": "materials/source_video.mp4",
            "media_path": "materials/source_video.mp4",
            "id": "v1",
        }],
    )

    user_draft_root = r"F:\JianyingPro Drafts"
    draft_name = "job_abc"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, draft_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    videos = data["materials"]["videos"]
    assert len(videos) == 1
    expected = r"F:\JianyingPro Drafts\jianying_draft_job_abc\materials\source_video.mp4"
    assert videos[0]["path"] == expected
    assert videos[0]["media_path"] == expected, (
        "media_path must also be rewritten to match path"
    )


def test_make_material_paths_absolute_trailing_separator_stripped(tmp_path):
    """user_draft_root with trailing separator is handled correctly (K11)."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed.wav", "id": "a1"}],
    )

    # Trailing backslash should not produce double backslash
    user_draft_root = r"F:\JianyingPro Drafts" + "\\"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, "draft_name"
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    path_val = data["materials"]["audios"][0]["path"]
    assert "\\\\" not in path_val, f"double backslash found in: {path_val!r}"
    assert path_val.startswith(r"F:\JianyingPro Drafts\\".rstrip("\\"))


def test_make_material_paths_absolute_both_videos_and_audios(tmp_path):
    """_make_material_paths_absolute rewrites both videos and audios in one pass (K11)."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        videos=[{"path": "materials/source_video.mp4", "media_path": "materials/source_video.mp4", "id": "v1"}],
        audios=[
            {"path": "materials/dubbed_audio.wav", "id": "a1"},
            {"path": "materials/ambient_audio.wav", "id": "a2"},
        ],
    )

    user_draft_root = r"D:\Drafts"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, "proj_001"
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    all_paths = (
        [v["path"] for v in data["materials"]["videos"]]
        + [a["path"] for a in data["materials"]["audios"]]
    )
    for p in all_paths:
        assert p.startswith(r"D:\Drafts\jianying_draft_proj_001\materials" + "\\"), (
            f"expected absolute path under D:\\Drafts\\jianying_draft_proj_001\\materials\\, got: {p!r}"
        )


# ---------------------------------------------------------------------------
# Guard: importing jianying_draft_writer itself must not fail without library
# ---------------------------------------------------------------------------


def test_import_writer_module_succeeds_without_pyjianying():
    """Importing jianying_draft_writer must not raise even if pyJianYingDraft
    is absent (lazy import).  We simulate absence via sys.modules patching."""
    with mock.patch.dict(sys.modules, {"pyJianYingDraft": None}):
        mod_name = "modules.output.jianying.jianying_draft_writer"
        saved = sys.modules.pop(mod_name, None)
        try:
            import importlib  # noqa: PLC0415

            mod = importlib.import_module(mod_name)
            # Module-level names should be present
            assert hasattr(mod, "JianyingDraftWriter")
            assert hasattr(mod, "JianyingEngineUnavailable")
        finally:
            if saved is not None:
                sys.modules[mod_name] = saved
            elif mod_name in sys.modules:
                del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Helpers used only by scenarios requiring pyJianYingDraft
# ---------------------------------------------------------------------------

# Gate: skip remaining tests if pyJianYingDraft is not installed.
pyjianying_pkg = pytest.importorskip("pyJianYingDraft")


def _make_wav(path: str, duration_s: float = 2.0, freq_hz: float = 440.0) -> None:
    """Write a minimal PCM WAV file at *path* using stdlib only."""
    sample_rate = 44100
    n_samples = int(sample_rate * duration_s)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            # Simple square wave
            value = 16000 if (i // 50) % 2 == 0 else -16000
            wf.writeframes(struct.pack("<h", value))


def _make_srt(path: str) -> None:
    """Write a minimal 2-cue SRT file at *path*."""
    content = (
        "1\n"
        "00:00:01,000 --> 00:00:03,444\n"
        "今天我们来看第一个问题。\n"
        "\n"
        "2\n"
        "00:00:03,444 --> 00:00:06,000\n"
        "这个问题涉及 LLM 推理成本。\n"
    )
    Path(path).write_text(content, encoding="utf-8")


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Generator[Path, None, None]:
    """Provide a fresh temp directory for each test."""
    yield tmp_path


@pytest.fixture()
def basic_request(tmp_workspace: Path):
    """A JianyingDraftRequest with real WAV and SRT files."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    return JianyingDraftRequest(
        project_id="test_proj_001",
        project_title="Test Project",
        source_video_path=str(tmp_workspace / "nonexistent_video.mp4"),  # intentionally missing
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )


# ---------------------------------------------------------------------------
# Scenario 2 — end-to-end with real WAV
# ---------------------------------------------------------------------------


def test_end_to_end_with_real_wav(tmp_workspace: Path):
    """Write a draft with a real WAV + SRT, check all expected artifacts."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_proj_e2e",
        project_title="E2E Test",
        source_video_path=str(tmp_workspace / "missing_video.mp4"),  # skipped gracefully
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    # draft dir exists
    assert os.path.isdir(result.draft_dir), f"draft_dir missing: {result.draft_dir}"

    # core JSON files
    assert os.path.isfile(result.draft_content_path), "draft_content.json missing"
    assert os.path.isfile(result.draft_meta_info_path), "draft_meta_info.json missing"

    # materials subdir contains dubbed audio copy
    materials_dir = os.path.join(result.draft_dir, "materials")
    dubbed_copy = os.path.join(materials_dir, "dubbed_audio.wav")
    assert os.path.isfile(dubbed_copy), "dubbed_audio copy not in materials/"

    # zip exists and is non-trivial
    assert os.path.isfile(result.draft_zip_path), "zip missing"
    assert os.path.getsize(result.draft_zip_path) > 1024, "zip too small"

    # result fields
    assert result.validation_status == "ok"
    assert result.manifest_path is None
    assert result.compatibility_report_path == ""


# ---------------------------------------------------------------------------
# Scenario 3 — missing source video -> video track skipped
# ---------------------------------------------------------------------------


def test_missing_video_skipped_gracefully(tmp_workspace: Path):
    """When source_video_path doesn't exist, video track is skipped, draft succeeds."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_no_video",
        project_title="No Video",
        source_video_path=str(tmp_workspace / "no_such_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    assert result.validation_status == "ok"

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    video_tracks = [t for t in content.get("tracks", []) if t.get("type") == "video"]
    assert len(video_tracks) == 0, "expected 0 video tracks when video missing"

    materials_videos = content.get("materials", {}).get("videos", [])
    assert len(materials_videos) == 0, "expected 0 video materials when video missing"


# ---------------------------------------------------------------------------
# Scenario 4 — missing dubbed_audio -> audio track skipped, draft still generated
# ---------------------------------------------------------------------------


def test_missing_dubbed_audio_skipped_gracefully(tmp_workspace: Path):
    """When dubbed_audio_path doesn't exist, audio track is skipped, draft still succeeds."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    srt = str(tmp_workspace / "subtitles.srt")
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_no_audio",
        project_title="No Audio",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=str(tmp_workspace / "no_such_audio.wav"),
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    assert result.validation_status == "ok"
    # draft JSON should still exist (text-only)
    assert os.path.isfile(result.draft_content_path)


# ---------------------------------------------------------------------------
# Scenario 5 — missing subtitle -> FileNotFoundError
# ---------------------------------------------------------------------------


def test_missing_subtitle_raises_file_not_found(tmp_workspace: Path):
    """When subtitle_path doesn't exist, writer raises FileNotFoundError."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    _make_wav(dubbed)

    req = JianyingDraftRequest(
        project_id="test_no_srt",
        project_title="No SRT",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=str(tmp_workspace / "no_such_subtitle.srt"),
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    with pytest.raises(FileNotFoundError, match=r"subtitle"):
        writer.write(req)


# ---------------------------------------------------------------------------
# Scenario 6 — materials paths are relative in draft_content.json
# ---------------------------------------------------------------------------


def test_material_paths_are_relative(basic_request):
    """After write(), audio material path in draft_content.json starts with 'materials/'."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    writer = JianyingDraftWriter()
    result = writer.write(basic_request)

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    audios = content.get("materials", {}).get("audios", [])
    assert len(audios) > 0, "no audio materials in draft_content.json"
    for audio in audios:
        path_val = audio.get("path", "")
        assert path_val.startswith("materials/"), (
            f"audio material path should start with 'materials/', got: {path_val!r}"
        )
        assert not os.path.isabs(path_val), (
            f"audio material path must be relative, got absolute: {path_val!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 7 — ambient audio added with lower volume
# ---------------------------------------------------------------------------


def test_ambient_audio_uses_lower_volume(tmp_workspace: Path):
    """Ambient audio segment uses volume=0.3 in draft_content.json."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    ambient = str(tmp_workspace / "ambient.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_wav(ambient, duration_s=6.0, freq_hz=220.0)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_ambient",
        project_title="Ambient Test",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
        ambient_audio_path=ambient,
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))

    # Find the ambient track by track name
    ambient_segments = []
    for track in content.get("tracks", []):
        if track.get("name") == "ambient":
            ambient_segments = track.get("segments", [])
            break

    assert len(ambient_segments) > 0, "no ambient track in draft_content.json"
    seg = ambient_segments[0]
    assert seg.get("volume") == pytest.approx(0.3, abs=0.001), (
        f"ambient segment volume should be 0.3, got {seg.get('volume')}"
    )


# ---------------------------------------------------------------------------
# Scenario 8 — custom width/height respected in canvas_config
# ---------------------------------------------------------------------------


def test_custom_width_height_in_canvas_config(tmp_workspace: Path):
    """Custom width/height from request appears in draft_content.json canvas_config."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_wh",
        project_title="Custom WH",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
        width=3840,
        height=2160,
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    canvas = content.get("canvas_config", {})
    assert canvas.get("width") == 3840, f"expected width=3840, got {canvas.get('width')}"
    assert canvas.get("height") == 2160, f"expected height=2160, got {canvas.get('height')}"


# ---------------------------------------------------------------------------
# Scenario 9 — project_id with special chars is sanitized in path
# ---------------------------------------------------------------------------


def test_project_id_special_chars_sanitized_in_path(tmp_workspace: Path):
    """project_id containing '/', ':', '\\' is sanitized; draft dir uses safe name."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="job/abc:123",
        project_title="Special Chars",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    # draft_dir name must not contain raw / or :
    draft_folder_name = os.path.basename(result.draft_dir)
    assert "/" not in draft_folder_name
    assert ":" not in draft_folder_name
    assert "\\" not in draft_folder_name

    # The folder should actually exist
    assert os.path.isdir(result.draft_dir)


# ---------------------------------------------------------------------------
# Scenario 10 — idempotent re-run overwrites without error
# ---------------------------------------------------------------------------


def test_idempotent_rerun_overwrites_without_error(tmp_workspace: Path):
    """Calling write() twice with the same request succeeds both times."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_idempotent",
        project_title="Idempotent",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result1 = writer.write(req)
    result2 = writer.write(req)

    # Both runs succeed
    assert result1.validation_status == "ok"
    assert result2.validation_status == "ok"

    # Draft still exists and is valid after second run
    assert os.path.isfile(result2.draft_content_path)
    assert os.path.isfile(result2.draft_zip_path)


# ---------------------------------------------------------------------------
# K11: write() absolute vs relative path mode (require pyJianYingDraft)
# ---------------------------------------------------------------------------


def test_write_with_user_draft_root_produces_absolute_paths(tmp_workspace: Path):
    """When user_draft_root is set, write() embeds absolute material paths (K11)."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    # Use a Unix-style root so the test works cross-platform without real Windows paths
    user_root = "/home/testuser/JianyingDrafts"
    req = JianyingDraftRequest(
        project_id="test_abs_paths",
        project_title="Absolute Path Test",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
        user_draft_root=user_root,
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    assert result.validation_status == "ok"

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    audios = content.get("materials", {}).get("audios", [])
    assert len(audios) > 0, "no audio materials in draft_content.json"
    for audio in audios:
        path_val = audio.get("path", "")
        assert path_val.startswith(user_root), (
            f"expected path starting with user_root {user_root!r}, got {path_val!r}"
        )
        assert os.path.basename(path_val).endswith(".wav"), (
            f"expected .wav filename in path, got {path_val!r}"
        )
        # Must not be a simple relative path
        assert not path_val.startswith("materials/"), (
            f"path should be absolute, not relative, got {path_val!r}"
        )


def test_write_without_user_draft_root_produces_relative_paths(tmp_workspace: Path):
    """When user_draft_root is None, write() uses relative material paths (back-compat, K11)."""
    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="test_rel_paths",
        project_title="Relative Path Test",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
        # user_draft_root omitted — defaults to None
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    assert result.validation_status == "ok"

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    audios = content.get("materials", {}).get("audios", [])
    assert len(audios) > 0, "no audio materials in draft_content.json"
    for audio in audios:
        path_val = audio.get("path", "")
        assert path_val.startswith("materials/"), (
            f"expected relative path starting with 'materials/', got {path_val!r}"
        )
        assert not os.path.isabs(path_val), (
            f"path must be relative, not absolute, got {path_val!r}"
        )

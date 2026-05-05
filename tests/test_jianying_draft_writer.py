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
# Friendly zip-naming helpers (2026-05-04)
# ---------------------------------------------------------------------------


def test_sanitize_zip_basename_preserves_chinese_and_spaces():
    """CJK + spaces are kept; Windows-illegal chars stripped; trailing
    whitespace/dots trimmed."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _sanitize_zip_basename,
    )

    # Pure CJK is preserved verbatim.
    assert _sanitize_zip_basename("如何在6到12个月内彻底重塑自我") == "如何在6到12个月内彻底重塑自我"

    # ASCII spaces inside the name are kept (UX choice).
    assert _sanitize_zip_basename("Buffett interview 2026") == "Buffett interview 2026"

    # Windows-illegal chars stripped, surrounding text kept.
    assert _sanitize_zip_basename('A: "Quote" / B') == "A Quote  B"
    assert _sanitize_zip_basename("name<>|?*") == "name"

    # Leading/trailing whitespace and dots trimmed.
    assert _sanitize_zip_basename("  spaced  ") == "spaced"
    assert _sanitize_zip_basename("trailing.dots...") == "trailing.dots"
    assert _sanitize_zip_basename("...lead.dots") == "lead.dots"


def test_sanitize_zip_basename_caps_at_80_chars_and_trims_trailing():
    """Names longer than 80 chars are truncated; trailing space/dot is
    re-stripped after truncation so we don't leave an unsafe ending."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _sanitize_zip_basename,
    )

    long_name = "A" * 200
    out = _sanitize_zip_basename(long_name)
    assert len(out) == 80
    assert out == "A" * 80

    # Truncation lands on a trailing space — trailing-space trim runs again.
    in_trick = "X" * 79 + " " + "Y" * 5
    out = _sanitize_zip_basename(in_trick)
    assert len(out) == 79
    assert out == "X" * 79  # trailing space stripped


def test_sanitize_zip_basename_returns_empty_on_unusable_input():
    """Empty input + input that's only illegal chars → empty string."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _sanitize_zip_basename,
    )

    assert _sanitize_zip_basename("") == ""
    assert _sanitize_zip_basename(None or "") == ""  # defensive: None coerced upstream
    assert _sanitize_zip_basename("///") == ""
    assert _sanitize_zip_basename("<>|?*") == ""
    assert _sanitize_zip_basename("   ") == ""
    assert _sanitize_zip_basename("....") == ""


def test_resolve_zip_basename_priority_uses_project_title_first():
    """Sanitized project_title wins over project_id."""
    import datetime as _dt  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_basename,
    )

    out = _resolve_zip_basename(
        project_title="如何在6到12个月内彻底重塑自我",
        project_id="job_2593995420f546c3bcdcbef126f0b202",
        today_utc=_dt.date(2026, 5, 4),
    )
    assert out == "如何在6到12个月内彻底重塑自我_2026-05-04"


def test_resolve_zip_basename_falls_back_to_project_id_when_title_empty():
    """Empty / pathological title → project_id basename."""
    import datetime as _dt  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_basename,
    )

    for empty_title in ("", None, "   ", "<>|?*"):
        out = _resolve_zip_basename(
            project_title=empty_title,
            project_id="job_abc123",
            today_utc=_dt.date(2026, 5, 4),
        )
        assert out == "job_abc123_2026-05-04", f"title={empty_title!r} produced {out!r}"


def test_resolve_zip_basename_falls_back_to_draft_when_all_empty():
    """Defensive: even if project_id sanitizes to empty (truly degenerate),
    the result is a usable literal — never empty."""
    import datetime as _dt  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_basename,
    )

    out = _resolve_zip_basename(
        project_title="",
        project_id="<>|?*",
        today_utc=_dt.date(2026, 5, 4),
    )
    assert out == "draft_2026-05-04"


def test_resolve_zip_basename_default_today_uses_utc():
    """When today_utc is omitted, the helper stamps current UTC date."""
    import datetime as _dt  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_basename,
    )

    before = _dt.datetime.now(_dt.timezone.utc).date()
    out = _resolve_zip_basename(project_title="hello", project_id="job")
    after = _dt.datetime.now(_dt.timezone.utc).date()
    # Date stamp is one of {before, after} — handles midnight crossing.
    assert out in (
        f"hello_{before.strftime('%Y-%m-%d')}",
        f"hello_{after.strftime('%Y-%m-%d')}",
    )


def test_resolve_zip_path_with_collision_returns_plain_when_unique(tmp_path):
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_path_with_collision,
    )

    out = _resolve_zip_path_with_collision(str(tmp_path), "title_2026-05-04")
    assert out == str(tmp_path / "title_2026-05-04.zip")


def test_resolve_zip_path_with_collision_appends_counter(tmp_path):
    """Existing zip → _2; that taken too → _3; etc."""
    from modules.output.jianying.jianying_draft_writer import (  # noqa: PLC0415
        _resolve_zip_path_with_collision,
    )

    base = "title_2026-05-04"
    (tmp_path / f"{base}.zip").write_bytes(b"")
    out2 = _resolve_zip_path_with_collision(str(tmp_path), base)
    assert out2 == str(tmp_path / f"{base}_2.zip")

    (tmp_path / f"{base}_2.zip").write_bytes(b"")
    out3 = _resolve_zip_path_with_collision(str(tmp_path), base)
    assert out3 == str(tmp_path / f"{base}_3.zip")


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
    """_make_material_paths_absolute rewrites paths with backslashes for Windows input.

    Post-2026-05-04 contract: third arg is the actual unzip-target folder
    name (== zip stem), no implicit ``jianying_draft_`` prefix.
    """
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed_audio.wav", "id": "a1"}],
    )

    user_draft_root = r"F:\剪映缓存\草稿\JianyingPro Drafts"
    unzip_folder_name = "如何在6到12个月内彻底重塑自我_2026-05-04"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, unzip_folder_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    audios = data["materials"]["audios"]
    assert len(audios) == 1
    expected = (
        r"F:\剪映缓存\草稿\JianyingPro Drafts"
        + "\\如何在6到12个月内彻底重塑自我_2026-05-04\\materials\\dubbed_audio.wav"
    )
    assert audios[0]["path"] == expected, f"got: {audios[0]['path']!r}"
    # No backslash in expected should appear as forward slash
    assert "/" not in audios[0]["path"], "Windows path must use backslashes only"


def test_make_material_paths_absolute_unix_style(tmp_path):
    """_make_material_paths_absolute rewrites paths with forward-slashes for Unix input."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed_audio.wav", "id": "a1"}],
    )

    user_draft_root = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
    unzip_folder_name = "My Project_2026-05-04"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, unzip_folder_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    audios = data["materials"]["audios"]
    assert len(audios) == 1
    expected = (
        "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
        "/My Project_2026-05-04/materials/dubbed_audio.wav"
    )
    assert audios[0]["path"] == expected, f"got: {audios[0]['path']!r}"
    assert "\\" not in audios[0]["path"], "Unix path must not contain backslashes"


def test_make_material_paths_absolute_rewrites_video_media_path(tmp_path):
    """_make_material_paths_absolute also rewrites media_path on video materials."""
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
    unzip_folder_name = "Buffett Interview_2026-05-04"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, unzip_folder_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    videos = data["materials"]["videos"]
    assert len(videos) == 1
    expected = r"F:\JianyingPro Drafts\Buffett Interview_2026-05-04\materials\source_video.mp4"
    assert videos[0]["path"] == expected
    assert videos[0]["media_path"] == expected, (
        "media_path must also be rewritten to match path"
    )


def test_make_material_paths_absolute_trailing_separator_stripped(tmp_path):
    """user_draft_root with trailing separator is handled correctly."""
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    content_path = str(tmp_path / "draft_content.json")
    _write_draft_content(
        content_path,
        audios=[{"path": "materials/dubbed.wav", "id": "a1"}],
    )

    # Trailing backslash should not produce double backslash
    user_draft_root = r"F:\JianyingPro Drafts" + "\\"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, "Title_2026-05-04"
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    path_val = data["materials"]["audios"][0]["path"]
    assert "\\\\" not in path_val, f"double backslash found in: {path_val!r}"
    assert path_val.startswith(r"F:\JianyingPro Drafts\Title_2026-05-04")


def test_make_material_paths_absolute_both_videos_and_audios(tmp_path):
    """_make_material_paths_absolute rewrites both videos and audios in one pass."""
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
    unzip_folder_name = "Project Name_2026-05-04"
    JianyingDraftWriter._make_material_paths_absolute(
        content_path, user_draft_root, unzip_folder_name
    )

    data = json.loads(Path(content_path).read_text(encoding="utf-8"))
    all_paths = (
        [v["path"] for v in data["materials"]["videos"]]
        + [a["path"] for a in data["materials"]["audios"]]
    )
    for p in all_paths:
        assert p.startswith(r"D:\Drafts\Project Name_2026-05-04\materials" + "\\"), (
            f"expected absolute path under D:\\Drafts\\Project Name_2026-05-04\\materials\\, got: {p!r}"
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


# ---------------------------------------------------------------------------
# Friendly zip naming end-to-end (2026-05-04)
# ---------------------------------------------------------------------------


def test_write_zip_filename_uses_project_title_and_date(tmp_workspace: Path):
    """Zip filename is ``{project_title}_{YYYY-MM-DD}.zip`` end-to-end.

    Pre-2026-05-04 the filename was ``jianying_draft_{project_id}.zip``;
    we renamed to surface the user's display name (passed through
    ``project_title``) and the generation date for friendliness.
    """
    import datetime as _dt  # noqa: PLC0415

    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="job_abcdef0123",
        project_title="如何在6到12个月内彻底重塑自我",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)

    assert result.validation_status == "ok"

    today_str = _dt.datetime.now(_dt.timezone.utc).date().strftime("%Y-%m-%d")
    expected_basename = f"如何在6到12个月内彻底重塑自我_{today_str}"

    zip_basename = os.path.basename(result.draft_zip_path)
    # Allow same-day collision suffixes (_2/_3) but the prefix must match.
    assert zip_basename.startswith(expected_basename), (
        f"zip filename should start with {expected_basename!r}, got {zip_basename!r}"
    )
    assert zip_basename.endswith(".zip")
    assert os.path.isfile(result.draft_zip_path)

    # Old prefix MUST NOT appear in the zip filename — that would be a
    # rollback regression.
    assert not zip_basename.startswith("jianying_draft_"), (
        f"zip filename should not use the legacy 'jianying_draft_' prefix, got {zip_basename!r}"
    )


def test_write_zip_filename_falls_back_to_project_id_when_title_empty(tmp_workspace: Path):
    """Empty ``project_title`` → basename uses sanitized project_id (defensive)."""
    import datetime as _dt  # noqa: PLC0415

    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    req = JianyingDraftRequest(
        project_id="job_fallback_test",
        project_title="",  # empty → fallback path
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)
    assert result.validation_status == "ok"

    today_str = _dt.datetime.now(_dt.timezone.utc).date().strftime("%Y-%m-%d")
    zip_basename = os.path.basename(result.draft_zip_path)
    assert zip_basename.startswith(f"job_fallback_test_{today_str}"), (
        f"expected fallback basename starting with job_fallback_test_{today_str}, "
        f"got {zip_basename!r}"
    )


def test_write_absolute_paths_use_friendly_unzip_folder_name(tmp_workspace: Path):
    """When user_draft_root is set, absolute paths in draft_content.json must
    point under ``{user_draft_root}/{friendly_basename}/materials/...``
    (NOT under the legacy ``jianying_draft_*`` prefix). This is the
    K11 invariant restated for the 2026-05-04 rename.
    """
    import datetime as _dt  # noqa: PLC0415

    from modules.output.jianying.jianying_draft_models import JianyingDraftRequest  # noqa: PLC0415
    from modules.output.jianying.jianying_draft_writer import JianyingDraftWriter  # noqa: PLC0415

    dubbed = str(tmp_workspace / "dubbed.wav")
    srt = str(tmp_workspace / "subtitles.srt")
    _make_wav(dubbed)
    _make_srt(srt)

    user_root = "/home/test/JianyingDrafts"
    req = JianyingDraftRequest(
        project_id="job_xyz",
        project_title="My Friendly Title",
        source_video_path=str(tmp_workspace / "no_video.mp4"),
        dubbed_audio_path=dubbed,
        subtitle_path=srt,
        output_dir=str(tmp_workspace / "output"),
        user_draft_root=user_root,
    )

    writer = JianyingDraftWriter()
    result = writer.write(req)
    assert result.validation_status == "ok"

    today_str = _dt.datetime.now(_dt.timezone.utc).date().strftime("%Y-%m-%d")
    expected_unzip_folder = f"My Friendly Title_{today_str}"
    expected_path_prefix = f"{user_root}/{expected_unzip_folder}/materials/"

    content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    audios = content.get("materials", {}).get("audios", [])
    assert len(audios) > 0
    for audio in audios:
        path_val = audio.get("path", "")
        assert path_val.startswith(expected_path_prefix), (
            f"expected path under {expected_path_prefix!r}, got {path_val!r}"
        )
        # And NOT under the legacy prefix.
        assert "jianying_draft_" not in path_val, (
            f"legacy prefix leaked into absolute path: {path_val!r}"
        )

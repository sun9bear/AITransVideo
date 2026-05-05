"""Tests for ``services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles``.

Phase D-2 of 2026-05-04-subtitle-audio-sync-plan.

Helper called at deliverable time (Jianying draft / materials_pack with
subtitles) to ensure ``output/subtitle_cues.json`` and the related SRT
files are whisper-aligned. Idempotent + cache-aware:

  - Already whisper-aligned + fingerprint matches → no-op
  - Already whisper-aligned + fingerprint mismatches (audio changed
    underneath) → regenerate
  - Proportional cues + admin enables whisper → regenerate
  - Proportional cues + admin disabled → no-op (proportional stays)

Returns a small status dict for caller logging / event emission.
"""
from __future__ import annotations

import hashlib
import json
import sys
import wave
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Fixture builder: minimal project_dir with editor/segments.json + per-segment
# WAV files + an existing proportional subtitle_cues.json
# ---------------------------------------------------------------------------


def _write_silence_wav(path: Path, duration_ms: int = 1000):
    """Tiny mono PCM-16 WAV — content varies by ``duration_ms`` so each
    segment gets a distinct content hash."""
    sample_rate = 16000
    n_frames = sample_rate * duration_ms // 1000
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)


def _build_minimal_project(tmp_path: Path, *, n_segments: int = 2,
                           cues_source: str = "semantic_block_v2") -> Path:
    """Build a project_dir resembling what publish stage produces:
      editor/segments.json (full DubbingSegment-style records)
      tts/segment_*_aligned.wav (per-segment audio)
      output/subtitles_zh.srt (Chinese SRT)
      output/subtitle_cues.json (cue list with ``source`` per cue)
      output/subtitle_quality_report.json
    """
    project_dir = tmp_path / "project"

    # editor/segments.json
    segs = []
    tts_dir = project_dir / "tts"
    tts_dir.mkdir(parents=True)
    for i in range(1, n_segments + 1):
        wav = tts_dir / f"segment_{i:03d}_aligned.wav"
        _write_silence_wav(wav, duration_ms=1000 + i * 100)
        segs.append({
            "segment_id": str(i),
            "speaker_id": "A",
            "display_name": "A",
            "voice_id": "v",
            "start_ms": (i - 1) * 1000,
            "end_ms": i * 1000,
            "target_duration_ms": 1000,
            "source_text": f"src{i}",
            "cn_text": f"文本{i}",
            "tts_input_cn_text": f"文本{i}",
            "actual_duration_ms": 1000,
            "alignment_method": "direct",
            "tts_audio_path": str(wav),
            "aligned_audio_path": str(wav),
            "dubbing_mode": "dub",
        })
    (project_dir / "editor").mkdir(parents=True, exist_ok=True)
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(segs, ensure_ascii=False), encoding="utf-8",
    )

    # output/ stage produced by publish
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cues = []
    for i in range(1, n_segments + 1):
        cues.append({
            "cue_id": f"segment_{i:03d}_cue_01",
            "block_id": f"segment_{i:03d}",
            "speaker_id": "A",
            "speaker_name": "A",
            "text": f"文本{i}",
            "en_text": f"src{i}",
            "start_ms": (i - 1) * 1000,
            "end_ms": i * 1000,
            "source": cues_source,  # proportional or whisper-aligned
            "needs_review": False,
            "review_reason": None,
        })
    cues_payload = {
        "schema_version": "subtitle_cues_v2",
        "project_id": "test-project",
        "cues": cues,
    }
    (output_dir / "subtitle_cues.json").write_text(
        json.dumps(cues_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "subtitle_quality_report.json").write_text(
        json.dumps({"validation_status": "passed", "issues": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    # SRT files: simple placeholder content matching cue count
    srt_lines = []
    for i, c in enumerate(cues, start=1):
        srt_lines.append(f"{i}\n00:00:0{i-1},000 --> 00:00:0{i},000\n{c['text']}\n")
    srt = "\n".join(srt_lines)
    (output_dir / "subtitles_zh.srt").write_text(srt, encoding="utf-8")
    (output_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
    (output_dir / "subtitles_en.srt").write_text(srt, encoding="utf-8")
    (output_dir / "subtitles_bilingual.srt").write_text(srt, encoding="utf-8")
    return project_dir


# ---------------------------------------------------------------------------
# already-aligned fast path: fingerprint match → no-op
# ---------------------------------------------------------------------------


def test_returns_already_aligned_when_cues_are_whisper_with_matching_fingerprint(
    tmp_path, monkeypatch,
):
    """Cues already carry whisper source AND fingerprint matches the
    current aligned WAV bytes → no work, no whisper invocation."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    # Add fingerprint matching current WAV bytes
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    cues_payload["alignment_fingerprint"] = _expected_fingerprint(project_dir)
    cues_path.write_text(json.dumps(cues_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    # Patch whisper to fail loudly if invoked
    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        side_effect=AssertionError("whisper should not be invoked for cache hit"),
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "already_aligned"
    assert status["whisper_invoked"] is False


# ---------------------------------------------------------------------------
# admin gate closed: no-op even if cues are proportional
# ---------------------------------------------------------------------------


def test_no_op_when_admin_disables_whisper(tmp_path, monkeypatch):
    """Admin policy off → never invoke whisper, never modify SRTs.
    Existing proportional cues remain in place."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": False}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(tmp_path, n_segments=2)
    cues_path = project_dir / "output" / "subtitle_cues.json"
    pre_mtime = cues_path.stat().st_mtime
    pre_content = cues_path.read_text(encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        side_effect=AssertionError("whisper should not be invoked when admin off"),
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "skipped_admin_disabled"
    assert status["whisper_invoked"] is False
    # File untouched
    assert cues_path.read_text(encoding="utf-8") == pre_content


def test_no_op_when_env_capability_off(tmp_path, monkeypatch):
    """Env capability off (ops kill switch) → no-op, regardless of admin."""
    monkeypatch.delenv("AVT_WHISPER_ALIGN_ENABLED", raising=False)
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(tmp_path, n_segments=2)

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        side_effect=AssertionError("whisper should not be invoked when env off"),
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "skipped_admin_disabled"
    assert status["whisper_invoked"] is False


# ---------------------------------------------------------------------------
# regenerate path: gates open + cues proportional → call whisper, rewrite SRTs
# ---------------------------------------------------------------------------


def test_regenerates_cues_and_srts_when_gates_open_and_proportional(
    tmp_path, monkeypatch,
):
    """Both gates open + existing cues are proportional → run whisper,
    rewrite cue JSON + 4 SRT files + quality report. Stamp fingerprint
    so subsequent calls hit the fast path."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(tmp_path, n_segments=2)

    fake_words = [
        {"start_ms": 100, "end_ms": 800, "text": "文本"},
    ]

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        return_value=fake_words,
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "regenerated"
    assert status["whisper_invoked"] is True
    assert status["blocks_processed"] == 2

    # Cues should now be whisper-aligned
    cues_path = project_dir / "output" / "subtitle_cues.json"
    payload = json.loads(cues_path.read_text(encoding="utf-8"))
    assert any("whisper" in c["source"].lower() for c in payload["cues"])
    # Fingerprint stamped
    assert "alignment_fingerprint" in payload

    # All 4 SRT files updated
    for n in ("subtitles_zh.srt", "subtitles.srt",
              "subtitles_en.srt", "subtitles_bilingual.srt"):
        assert (project_dir / "output" / n).is_file()


# ---------------------------------------------------------------------------
# fingerprint mismatch: aligned WAV bytes changed since last whisper run
# ---------------------------------------------------------------------------


def test_regenerates_when_fingerprint_mismatches_current_audio(
    tmp_path, monkeypatch,
):
    """Old whisper-aligned cues + fingerprint references different audio
    bytes (e.g. user edited a segment and re-TTS'd) → re-run whisper.
    Cache will hit for unchanged segments; only the changed one needs
    new transcription."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    # Stamp a stale fingerprint
    cues_path = project_dir / "output" / "subtitle_cues.json"
    payload = json.loads(cues_path.read_text(encoding="utf-8"))
    payload["alignment_fingerprint"] = "stale_fingerprint_no_longer_matches"
    cues_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        return_value=[{"start_ms": 0, "end_ms": 800, "text": "文本"}],
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "regenerated"
    # New fingerprint stamped
    new_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    assert new_payload["alignment_fingerprint"] != "stale_fingerprint_no_longer_matches"


# ---------------------------------------------------------------------------
# missing inputs: proper failure modes
# ---------------------------------------------------------------------------


def test_returns_skipped_no_segments_when_editor_segments_missing(
    tmp_path, monkeypatch,
):
    """No editor/segments.json on disk → can't regenerate, return
    skipped status (don't crash)."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = tmp_path / "project"
    (project_dir / "output").mkdir(parents=True)

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    status = ensure_whisper_aligned_subtitles(project_dir)
    assert status["action"] == "skipped_no_segments"
    assert status["whisper_invoked"] is False


def test_returns_skipped_no_cues_when_subtitle_cues_missing(
    tmp_path, monkeypatch,
):
    """editor/segments.json present but output/subtitle_cues.json missing
    (publish never ran for this project) → still regenerate from scratch.
    The helper isn't required to skip — the absence of cues is just an
    edge case that should produce fresh whisper-aligned cues."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(tmp_path, n_segments=2)
    # Remove the existing cues file to simulate "never published"
    (project_dir / "output" / "subtitle_cues.json").unlink()

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        return_value=[{"start_ms": 0, "end_ms": 800, "text": "文本"}],
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)
    assert status["action"] == "regenerated"


# ---------------------------------------------------------------------------
# helper: compute the fingerprint the way the production helper does, so
# tests can pre-seed a "matching" fingerprint
# ---------------------------------------------------------------------------


def _expected_fingerprint(project_dir: Path) -> str:
    """Mirror of services.subtitles.ensure_whisper_alignment._compute_alignment_fingerprint."""
    from services.subtitles.ensure_whisper_alignment import (
        _compute_alignment_fingerprint,
    )
    segs = json.loads(
        (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    )
    return _compute_alignment_fingerprint(segs)

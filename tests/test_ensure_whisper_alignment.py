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
    current aligned WAV bytes AND stamped model matches admin model
    → no work, no whisper invocation."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_model": "small",
        }),
        encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    # Add fingerprint matching current WAV bytes + stamped model.
    # CodeX P1 follow-up #2 (2026-05-05) made the fast path require both;
    # missing alignment_model treats the payload as model-unknown and
    # forces regeneration (one-time rebuild for legacy payloads).
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    cues_payload["alignment_fingerprint"] = _expected_fingerprint(project_dir)
    cues_payload["alignment_model"] = "small"
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


# ---------------------------------------------------------------------------
# CodeX P1 (2026-05-05): _content_hash_for_wav must NOT trust the
# whisper-cache sidecar's content_hash without verifying current WAV bytes.
#
# Regression: a project that successfully ran whisper alignment has
# {wav_path}.whisper_<model>_<lang>.json sidecars. If a user then re-TTSs
# a segment (overwrites the WAV bytes at the same path), the sidecar
# stays behind with the OLD hash. _content_hash_for_wav reads sidecar.
# content_hash and returns it without checking the WAV — so the
# alignment fingerprint is computed against stale bytes and matches the
# previously-stamped fingerprint in subtitle_cues.json. ensure_helper
# returns "already_aligned" → whisper does NOT re-run → SRT keeps the
# old timing while the new audio plays differently.
#
# Fix: hash the actual WAV bytes. The C5 cache sidecar is meant for the
# whisper subprocess wrapper (where the hash is verified before reuse);
# it is NOT a trustworthy fingerprint source for downstream consumers.
# ---------------------------------------------------------------------------


def test_regenerates_when_wav_overwritten_under_stale_sidecar(
    tmp_path, monkeypatch,
):
    """Setup mimics a real edit-commit/re-TTS round:
      1. project ran ensure once → cues are whisper-aligned + fingerprint
         stamped, sidecars exist next to each WAV with content_hash=H1.
      2. user re-TTS'd one segment, overwriting its WAV bytes → current
         hash for that WAV is H2 ≠ H1.
      3. user clicks "生成剪映草稿" → ensure runs again.

    Expected behavior: ensure detects the audio change and regenerates.
    The sidecar's stale H1 must NOT be trusted as the content hash —
    it would make the fingerprint match the old stamped value, falsely
    triggering the already_aligned fast path.
    """
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )

    # (1) Compute the "old" fingerprint and stamp it into subtitle_cues.json
    # at this point the WAV bytes still match what the sidecars will say.
    old_fp = _expected_fingerprint(project_dir)
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    cues_payload["alignment_fingerprint"] = old_fp
    cues_path.write_text(json.dumps(cues_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # Pre-seed the C5 sidecar files with the OLD hash. This is what the
    # whisper subprocess wrapper writes after a successful run.
    tts_dir = project_dir / "tts"
    for wav_path in sorted(tts_dir.glob("segment_*_aligned.wav")):
        old_hash = hashlib.sha256(wav_path.read_bytes()).hexdigest()
        sidecar = wav_path.with_name(f"{wav_path.name}.whisper_small_zh.json")
        sidecar.write_text(json.dumps({
            "version": 1,
            "content_hash": old_hash,
            "model": "small",
            "language": "zh",
            "words": [{"start_ms": 0, "end_ms": 100, "text": "stale"}],
        }), encoding="utf-8")

    # (2) Now overwrite ONE WAV's bytes (simulating re-TTS for that
    # segment). The sidecar stays — it carries the stale H1.
    target_wav = tts_dir / "segment_001_aligned.wav"
    new_bytes = target_wav.read_bytes() + b"_NEW_TTS_BYTES_"
    target_wav.write_bytes(new_bytes)

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    # Patch the cue_pipeline whisper call so we can detect "did regenerate
    # actually run?" without spawning a real subprocess.
    fake_run_calls: list[str] = []

    def _fake_whisper_cached(wav_path, *args, **kwargs):
        fake_run_calls.append(str(wav_path))
        return [{"start_ms": 0, "end_ms": 100, "text": "fresh"}]

    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        _fake_whisper_cached,
    )

    status = ensure_whisper_aligned_subtitles(project_dir)

    # Whisper MUST have re-run because the WAV bytes changed.
    assert status["action"] == "regenerated", (
        f"Expected 'regenerated' (new WAV bytes), got {status['action']!r}. "
        "Helper trusted the stale sidecar hash and incorrectly took the "
        "already_aligned fast path."
    )
    assert status["whisper_invoked"] is True
    # And the cue_pipeline did invoke whisper for at least the modified
    # segment (others may cache-hit at the subprocess level — that's fine).
    assert len(fake_run_calls) >= 1


# ---------------------------------------------------------------------------
# CodeX P1 follow-up #2 (2026-05-05): already_aligned fast path must
# respect admin model + skip_cache, not just the audio fingerprint.
#
# Regression: the fast path checked
#   - all cues carry whisper source
#   - stamped audio fingerprint matches current WAV bytes
# but ignored:
#   - the model field (admin switching small → medium needs a fresh
#     transcript even though audio is unchanged)
#   - skip_cache=true (admin saying "force fresh" was being silently
#     swallowed by the fast path)
# Result on materials_pack: SRT file in zip kept old-model timing
# despite admin having flipped to a larger model. On Jianying: top-level
# fingerprint already invalidates (model is in _whisper_policy_snapshot),
# but the inner ensure call still returned already_aligned and produced
# the stale SRT, which then went into the rebuilt zip.
# ---------------------------------------------------------------------------


def test_ensure_regenerates_when_admin_model_changes(tmp_path, monkeypatch):
    """First run with model=small produces whisper-aligned cues stamped
    with that model; admin then switches to medium. ensure must NOT
    return ``already_aligned`` — the transcripts were produced by a
    different model and should be regenerated.

    Regression for: admin can change ``whisper_alignment_model`` but
    fast-path gate doesn't notice → stale SRT in deliverable zip."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_model": "small",
        }),
        encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    # Stamp the audio fingerprint as if this run was produced under
    # model=small — the audio is unchanged.
    cues_payload["alignment_fingerprint"] = _expected_fingerprint(project_dir)
    cues_payload["alignment_model"] = "small"
    cues_path.write_text(json.dumps(cues_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # Now admin switches to medium.
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_model": "medium",
        }),
        encoding="utf-8",
    )

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    fake_run_calls: list[str] = []

    def _fake_whisper_cached(wav_path, *args, **kwargs):
        fake_run_calls.append(str(wav_path))
        return [{"start_ms": 0, "end_ms": 100, "text": "fresh-medium"}]

    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        _fake_whisper_cached,
    )

    status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "regenerated", (
        f"Model switch (small→medium) must trigger regeneration, "
        f"got action={status['action']!r}. The fast path is ignoring "
        f"the admin model field."
    )
    assert status["whisper_invoked"] is True
    assert len(fake_run_calls) >= 1


def test_ensure_skip_cache_bypasses_already_aligned_fast_path(
    tmp_path, monkeypatch,
):
    """When admin sets ``whisper_alignment_skip_cache=true``, the
    already_aligned fast path must be bypassed — even if cues are
    whisper-aligned and audio fingerprint matches. This is the admin's
    explicit "force fresh transcription" lever; if the fast path
    swallows it, the lever has zero effect."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_skip_cache": True,  # ← the lever
        }),
        encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    cues_payload["alignment_fingerprint"] = _expected_fingerprint(project_dir)
    cues_payload["alignment_model"] = "small"
    cues_path.write_text(json.dumps(cues_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    fake_run_calls: list[str] = []

    def _fake_whisper_cached(wav_path, *args, **kwargs):
        fake_run_calls.append(str(wav_path))
        return [{"start_ms": 0, "end_ms": 100, "text": "fresh"}]

    monkeypatch.setattr(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        _fake_whisper_cached,
    )

    status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "regenerated", (
        f"skip_cache=true must bypass the already_aligned fast path; "
        f"got action={status['action']!r}. The lever is being ignored."
    )
    assert status["whisper_invoked"] is True


def test_ensure_already_aligned_when_model_matches_and_no_skip_cache(
    tmp_path, monkeypatch,
):
    """Sanity / no-regression: when stamped model matches admin model
    AND skip_cache is false AND audio fingerprint matches → fast path
    fires (no whisper invocation, action=already_aligned). This is the
    intended common case: same admin policy, same audio bytes, idempotent
    repeat trigger."""
    monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_model": "small",
            "whisper_alignment_skip_cache": False,
        }),
        encoding="utf-8",
    )

    project_dir = _build_minimal_project(
        tmp_path, n_segments=2,
        cues_source="semantic_block_v2_whisper_aligned",
    )
    cues_path = project_dir / "output" / "subtitle_cues.json"
    cues_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    cues_payload["alignment_fingerprint"] = _expected_fingerprint(project_dir)
    cues_payload["alignment_model"] = "small"
    cues_path.write_text(json.dumps(cues_payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        ensure_whisper_aligned_subtitles,
    )

    with patch(
        "modules.subtitles.cue_pipeline._run_whisper_cached",
        side_effect=AssertionError("whisper should not be invoked on fast-path hit"),
    ):
        status = ensure_whisper_aligned_subtitles(project_dir)

    assert status["action"] == "already_aligned"
    assert status["whisper_invoked"] is False


def test_content_hash_for_wav_returns_actual_bytes_not_sidecar(tmp_path):
    """Direct unit test on the helper: when WAV bytes differ from the
    sidecar's stored content_hash, the helper MUST return the hash of
    the current bytes, not the sidecar value."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"new-audio-bytes")
    actual_hash = hashlib.sha256(b"new-audio-bytes").hexdigest()

    # Plant a stale sidecar with a wrong hash
    sidecar = wav.with_name(f"{wav.name}.whisper_small_zh.json")
    sidecar.write_text(json.dumps({
        "version": 1,
        "content_hash": "deadbeef" * 8,  # plausibly-shaped but wrong
        "model": "small",
        "language": "zh",
        "words": [],
    }), encoding="utf-8")

    from services.subtitles.ensure_whisper_alignment import (
        _content_hash_for_wav,
    )

    result = _content_hash_for_wav(str(wav))
    assert result == actual_hash, (
        f"_content_hash_for_wav returned {result!r}; expected the actual "
        f"WAV bytes' hash {actual_hash!r}. The sidecar's stored hash "
        f"({'deadbeef' * 8!r}) must NOT be trusted without verifying "
        "current bytes."
    )

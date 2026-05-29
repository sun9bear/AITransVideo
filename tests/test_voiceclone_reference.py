"""Phase 1 (plan 2026-05-29 free-tier): per-speaker reference clip extraction."""

import services.tts.voiceclone_reference as vr
from services.tts.voiceclone_reference import pick_reference_window


def test_pick_reference_window_uses_longest_then_caps_to_max():
    # speaker_a: 0-2s, 5-12s (7s longest) -> take longest, cap to max(5s)
    segs = [
        {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 2000},
        {"speaker_id": "speaker_a", "start_ms": 5000, "end_ms": 12000},
    ]
    win = pick_reference_window(segs, "speaker_a", min_s=3.0, max_s=5.0)
    assert win is not None
    start_ms, end_ms = win
    assert start_ms == 5000
    assert (end_ms - start_ms) == 5000  # capped to max_s


def test_pick_reference_window_returns_none_if_all_too_short():
    segs = [{"speaker_id": "s1", "start_ms": 0, "end_ms": 1500}]
    assert pick_reference_window(segs, "s1", min_s=3.0, max_s=5.0) is None


def test_extract_speaker_references_writes_per_speaker_skips_short(tmp_path, monkeypatch):
    segs = [
        {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 6000},
        {"speaker_id": "speaker_b", "start_ms": 7000, "end_ms": 8000},  # 1s too short -> skipped
    ]
    speech = tmp_path / "speech.wav"
    speech.write_bytes(b"\x00" * 100)
    out = tmp_path / "refs"

    def fake_run(cmd, **kw):
        # emulate ffmpeg writing the output file (last positional arg)
        from pathlib import Path as P
        P(cmd[-1]).write_bytes(b"\x00" * 500)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    refs = vr.extract_speaker_references(segs, speech, out, min_s=3.0, max_s=5.0)
    assert set(refs.keys()) == {"speaker_a"}
    assert refs["speaker_a"].exists()


def test_stamp_segment_references_sets_path_by_speaker(tmp_path, monkeypatch):
    """Phase 2a: stamp each segment's voiceclone_reference_path from the
    per-speaker extraction result (by speaker_id); unmatched speakers stay
    None so the TTS dispatch falls back to the base MiMo preset."""
    from types import SimpleNamespace

    def _seg(sid, spk):
        return SimpleNamespace(
            segment_id=sid, speaker_id=spk, start_ms=0, end_ms=6000,
            voiceclone_reference_path=None,
        )

    segs = [_seg(1, "speaker_a"), _seg(2, "speaker_a"), _seg(3, "speaker_b")]
    ref_a = tmp_path / "speaker_a.wav"
    ref_a.write_bytes(b"\x00" * 100)
    # Only speaker_a gets a usable reference; speaker_b is skipped by extraction.
    monkeypatch.setattr(
        vr, "extract_speaker_references", lambda *a, **k: {"speaker_a": ref_a}
    )

    stamped = vr.stamp_segment_references(segs, tmp_path / "speech.wav", tmp_path / "refs")

    assert stamped == 2  # both speaker_a segments
    assert segs[0].voiceclone_reference_path == str(ref_a)
    assert segs[1].voiceclone_reference_path == str(ref_a)
    assert segs[2].voiceclone_reference_path is None  # speaker_b unmatched → base preset

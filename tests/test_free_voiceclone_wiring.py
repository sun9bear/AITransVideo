"""Phase 2a Task 2 (Chunk A): MiMo voiceclone wiring into TTSGenerator.

Mock-only — never calls the real MiMo API. Verifies that a segment carrying a
``voiceclone_reference_path`` routes through ``_generate_one_mimo_voiceclone``
(reusing the Phase 1 ``synthesize_voiceclone`` primitive with that reference),
and that a segment without one defaults to None (→ caller dispatches to the
base MiMo preset).
"""
import services.tts.tts_generator as tg
import services.tts.mimo_tts_provider as mp
from services.gemini.translator import DubbingSegment


def _seg(**kw):
    base = dict(
        segment_id=3, speaker_id="speaker_a", display_name="", voice_id="v",
        start_ms=0, end_ms=1000, target_duration_ms=1000,
        source_text="hello", cn_text="你好世界",
    )
    base.update(kw)
    return DubbingSegment(**base)


def test_voiceclone_method_uses_segment_reference(monkeypatch, tmp_path):
    ref = tmp_path / "speaker_a.wav"
    ref.write_bytes(b"\x00" * 2000)
    captured = {}

    def fake_vc(text, *, reference_audio, **kw):
        captured["text"] = text
        captured["ref"] = reference_audio
        return b"RIFF" + b"\x00" * 400

    monkeypatch.setattr(mp, "synthesize_voiceclone", fake_vc)
    monkeypatch.setattr(tg, "_ffprobe_duration_ms", lambda p: 1234)

    seg = _seg(voiceclone_reference_path=str(ref))
    gen = tg.TTSGenerator.__new__(tg.TTSGenerator)  # no __init__ needed for this path
    result = gen._generate_one_mimo_voiceclone(seg, "你好世界", tmp_path)

    assert captured["ref"] == str(ref)
    assert captured["text"] == "你好世界"
    assert result.segment_id == 3
    assert result.duration_ms == 1234


def test_segment_defaults_to_no_reference():
    """No stamp → voiceclone_reference_path is None, so the dispatch in
    _generate_one routes to the base MiMo preset (not voiceclone)."""
    assert _seg().voiceclone_reference_path is None


# ── _generate_one dispatch: free/non-free/no-ref three branches (CodeX P2) ──

def _dispatch_gen(voice_strategy):
    gen = tg.TTSGenerator.__new__(tg.TTSGenerator)  # bypass __init__
    gen._voice_strategy = voice_strategy
    return gen


def _spy_branches(monkeypatch, gen):
    calls = []

    def _vc(segment, tts_text, output_root):
        calls.append("voiceclone")
        return tg.TTSResult(segment_id=segment.segment_id, audio_path="x", duration_ms=1, voice_id="v")

    def _base(segment, tts_text, output_root):
        calls.append("base")
        return tg.TTSResult(segment_id=segment.segment_id, audio_path="x", duration_ms=1, voice_id="v")

    monkeypatch.setattr(gen, "_generate_one_mimo_voiceclone", _vc)
    monkeypatch.setattr(gen, "_generate_one_mimo", _base)
    monkeypatch.setattr(gen, "_record_tts_usage", lambda *a, **k: None)
    return calls


def test_dispatch_free_with_reference_uses_voiceclone(monkeypatch, tmp_path):
    gen = _dispatch_gen("free_voiceclone")
    calls = _spy_branches(monkeypatch, gen)
    seg = _seg(tts_provider="mimo", voiceclone_reference_path="/tmp/ref.wav")
    gen._generate_one(seg, str(tmp_path), provider="mimo")
    assert calls == ["voiceclone"]


def test_dispatch_nonfree_with_reference_uses_base(monkeypatch, tmp_path):
    """Defense in depth: a non-free job (voice_strategy != free_voiceclone) must
    NOT clone even if a reference stray-stamped onto the segment."""
    gen = _dispatch_gen("preset_mapping")
    calls = _spy_branches(monkeypatch, gen)
    seg = _seg(tts_provider="mimo", voiceclone_reference_path="/tmp/ref.wav")
    gen._generate_one(seg, str(tmp_path), provider="mimo")
    assert calls == ["base"]


def test_dispatch_free_without_reference_uses_base(monkeypatch, tmp_path):
    gen = _dispatch_gen("free_voiceclone")
    calls = _spy_branches(monkeypatch, gen)
    seg = _seg(tts_provider="mimo")  # no reference stamped
    gen._generate_one(seg, str(tmp_path), provider="mimo")
    assert calls == ["base"]

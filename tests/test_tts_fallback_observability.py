"""Tests for T7: TTS fallback is observable end-to-end.

When primary TTS provider fails and the fallback provider succeeds (e.g.
MiniMax exhausted → CosyVoice takes over), the substitution must be
traceable in three places:
  1. Structured log line `tts_fallback_triggered`
  2. TTSResult.fallback_used_provider (None when primary succeeds)
  3. DubbingSegment.fallback_used_provider (mirrors result, for manifest)

These tests lock down those contracts.
"""
from __future__ import annotations

from dataclasses import asdict

import pytest

from services.tts.tts_generator import TTSResult


class TestTTSResultFallbackField:
    def test_default_is_none(self):
        """Primary-success path: fallback_used_provider must stay None."""
        r = TTSResult(
            segment_id=1, audio_path="x.wav", duration_ms=1000, voice_id="v1",
        )
        assert r.fallback_used_provider is None

    def test_can_be_set_to_provider_name(self):
        r = TTSResult(
            segment_id=1, audio_path="x.wav", duration_ms=1000, voice_id="v1",
            fallback_used_provider="cosyvoice",
        )
        assert r.fallback_used_provider == "cosyvoice"

    def test_asdict_includes_field(self):
        """Regression guard: dataclass serialization surfaces the field so
        downstream code that uses asdict() (or manual dict construction that
        enumerates fields) picks it up."""
        r = TTSResult(
            segment_id=1, audio_path="x.wav", duration_ms=1000, voice_id="v1",
            fallback_used_provider="cosyvoice",
        )
        d = asdict(r)
        assert d["fallback_used_provider"] == "cosyvoice"


class TestDubbingSegmentFallbackField:
    def test_default_is_none(self):
        from services.gemini.translator import DubbingSegment
        s = DubbingSegment(
            segment_id=1, speaker_id="A", display_name="Alice",
            voice_id="v1", start_ms=0, end_ms=1000, target_duration_ms=1000,
            source_text="hi", cn_text="你好",
        )
        assert s.fallback_used_provider is None

    def test_can_be_set(self):
        from services.gemini.translator import DubbingSegment
        s = DubbingSegment(
            segment_id=1, speaker_id="A", display_name="Alice",
            voice_id="v1", start_ms=0, end_ms=1000, target_duration_ms=1000,
            source_text="hi", cn_text="你好",
            fallback_used_provider="cosyvoice",
        )
        assert s.fallback_used_provider == "cosyvoice"


class TestFallbackTriggerSite:
    """Verify the fallback code path in tts_generator actually sets the flag
    and emits the structured log line.

    We don't mock the full TTS stack — we inspect the source code shape to
    guarantee the key lines exist. This is a regression guard: if someone
    removes the logger.warning or the result.fallback_used_provider =
    assignment during refactor, these checks fail and the observability
    contract is broken.
    """

    def test_fallback_trigger_sets_result_field(self):
        """The fallback code path must assign fallback_used_provider onto
        the result returned from _generate_one."""
        import inspect
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        # Look for the specific pattern: result.fallback_used_provider = fallback
        assert "result.fallback_used_provider = fallback" in src, (
            "Fallback code path must tag TTSResult with fallback_used_provider; "
            "otherwise downstream can't audit voice substitutions."
        )

    def test_fallback_trigger_emits_structured_log(self):
        """Structured log line is the operator-visible trace."""
        import inspect
        from services.tts import tts_generator
        src = inspect.getsource(tts_generator)
        assert "tts_fallback_triggered" in src, (
            "Expected structured log event `tts_fallback_triggered` at the "
            "MiniMax→CosyVoice (etc.) fallback site."
        )


def test_segment_copy_propagates_fallback_field():
    """When tts_generator finishes processing a segment, it must copy the
    fallback flag from TTSResult onto the DubbingSegment. Otherwise the
    manifest construction in process.py (which reads from segment) can't
    surface it.

    Checks the code shape rather than simulating full TTS — we verify the
    key assignment `segment.fallback_used_provider = result.fallback_used_provider`
    exists in the post-generation field-copy block.
    """
    import inspect
    from services.tts import tts_generator
    src = inspect.getsource(tts_generator)
    assert "segment.fallback_used_provider = result.fallback_used_provider" in src, (
        "segment must inherit fallback_used_provider from result so the "
        "final manifest (process.py:~3920) can emit it."
    )


def test_manifest_emits_fallback_field():
    """The segment manifest dict in process.py must include fallback_used_provider."""
    import inspect
    from pipeline import process as pipeline_process
    src = inspect.getsource(pipeline_process)
    assert '"fallback_used_provider": segment.fallback_used_provider' in src, (
        "Segment manifest must write fallback_used_provider so users can "
        "audit which segments used a different TTS provider than their "
        "primary selection."
    )


def test_tts_usage_records_resolved_model_not_global_default():
    from services.tts.tts_generator import TTSConfig, TTSGenerator

    class Meter:
        def __init__(self):
            self.events = []

        def record_tts(self, **kwargs):
            self.events.append(kwargs)

    meter = Meter()
    generator = TTSGenerator(TTSConfig(api_key="test", model="speech-2.8-turbo"))
    generator.set_usage_meter(meter)
    result = TTSResult(
        segment_id=1,
        audio_path="x.wav",
        duration_ms=1000,
        voice_id="v1",
        billed_chars=20,
    )

    generator._record_tts_usage(
        result,
        bucket="first_tts",
        provider="minimax",
        model="speech-2.8-hd",
        text="测试",
    )

    assert meter.events[0]["model"] == "speech-2.8-hd"

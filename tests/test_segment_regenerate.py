"""Tests for the real SegmentTTSCaller factory.

The caller is the only production bridge between the post-edit user
surface and paid TTS providers. These tests pin down:

- Retry semantics: bounded retries against the SAME provider (no auto
  provider fallback — guards the paid-API policy from CLAUDE.md).
- Provider pass-through: the segment's ``tts_provider`` reaches
  ``TTSGenerator._generate_one`` verbatim.
- segment_id coercion: editor_baseline normalises to str; caller must
  map editing ids to ints before constructing DubbingSegment (legacy
  TTSGenerator filename contract).
- Lazy TTSGenerator instantiation: factory call itself must not try to
  load TTS credentials — only the first user click does.

TTSGenerator is mocked at ``_generate_one`` level so we don't hit the
real network. TTSConfig is also mocked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from services.gemini.translator import DubbingSegment
from services.tts.tts_generator import TTSGenerationError, TTSResult


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeTTSConfig:
    """Minimal stand-in for TTSConfig. segment_regenerate never inspects
    fields; it only passes the object straight into TTSGenerator.__init__.
    By patching TTSGenerator as a whole we bypass real validation."""
    api_key: str = "fake"


class _FakeGenerator:
    """Stand-in for TTSGenerator. Records every ``_generate_one`` call so
    tests can assert on provider / retry count / segment contents."""

    def __init__(self, config: object) -> None:
        self.config = config
        self.calls: list[dict[str, Any]] = []
        # Script controls per-call behaviour. Set by tests.
        self.next_results: list[Any] = []

    def _generate_one(
        self,
        segment: DubbingSegment,
        output_dir: str,
        *,
        provider: str | None = None,
        usage_bucket: str | None = None,
    ) -> TTSResult:
        self.calls.append({
            "segment_id": segment.segment_id,
            "cn_text": segment.cn_text,
            "voice_id": segment.voice_id,
            "tts_provider": segment.tts_provider,
            "provider_arg": provider,
            "usage_bucket": usage_bucket,
            "output_dir": output_dir,
        })
        if not self.next_results:
            raise AssertionError(
                "test ran out of scripted results for _generate_one"
            )
        scripted = self.next_results.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        # scripted is a path (str) — write a fake wav there and return
        # a TTSResult pointing at it.
        wav_path = Path(output_dir) / "fake.wav"
        wav_path.write_bytes(b"RIFFfakefake")
        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(wav_path),
            duration_ms=1234,
            voice_id=segment.voice_id,
            selected_voice=segment.voice_id,
            match_confidence="high",
            billed_chars=10,
        )


@pytest.fixture
def patch_ttsgen(monkeypatch: pytest.MonkeyPatch) -> _FakeGenerator:
    """Replace TTSGenerator and load_tts_config in the segment_regenerate
    module namespace. Return the fake generator instance that every
    caller invocation will share (matches production's module-scope
    instance caching)."""
    from services.tts import segment_regenerate as mod

    fake_gen: dict[str, _FakeGenerator] = {}

    def _fake_ctor(cfg: object) -> _FakeGenerator:
        # Reuse the same instance across factory lazy-init and test handle
        # access so state set via handle.fake.next_results is visible to
        # the caller.
        if "instance" not in fake_gen:
            fake_gen["instance"] = _FakeGenerator(cfg)
        return fake_gen["instance"]

    monkeypatch.setattr(mod, "TTSGenerator", _fake_ctor)
    monkeypatch.setattr(mod, "load_tts_config", lambda: _FakeTTSConfig())

    # No-op sleep so retry tests don't actually wait 1s/2s/4s.
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    class _Handle:
        @property
        def fake(self) -> _FakeGenerator:
            # Triggers lazy init on first access so tests can prime
            # next_results before invoking the caller.
            if "instance" not in fake_gen:
                fake_gen["instance"] = _FakeGenerator(_FakeTTSConfig())
            return fake_gen["instance"]

    return _Handle()  # type: ignore[return-value]


def _minimal_segment(
    segment_id: object = 1,
    *,
    tts_provider: str = "minimax",
    voice_id: str = "voice_A",
    cn_text: str = "你好",
) -> dict[str, Any]:
    """Minimal segment dict carrying every required DubbingSegment field
    plus the test-specific overrides."""
    return {
        "segment_id": segment_id,
        "speaker_id": "speaker_a",
        "display_name": "讲述者",
        "voice_id": voice_id,
        "start_ms": 0,
        "end_ms": 1000,
        "target_duration_ms": 1000,
        "source_text": "hello",
        "cn_text": cn_text,
        "tts_provider": tts_provider,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_caller_writes_wav_and_calls_generate_one_with_segment_provider(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    patch_ttsgen.fake.next_results = ["ok"]

    segment = _minimal_segment(tts_provider="volcengine", voice_id="vc_42")
    output = tmp_path / "draft" / "1.wav"

    caller(segment, output)

    assert output.is_file(), "caller must write the wav to the given output_path"
    assert output.read_bytes().startswith(b"RIFF"), "draft wav should be the fake payload"

    gen = patch_ttsgen.fake
    assert len(gen.calls) == 1
    assert gen.calls[0]["tts_provider"] == "volcengine"
    # _generate_one's explicit provider kwarg is passed; it must match
    # the segment's own tts_provider (no substitution at this layer).
    assert gen.calls[0]["provider_arg"] == "volcengine"
    assert gen.calls[0]["voice_id"] == "vc_42"
    assert gen.calls[0]["segment_id"] == 1


def test_caller_coerces_str_segment_id_to_int_for_dubbing_segment(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    """editor_baseline normalises segment_id to str, DubbingSegment
    dataclass wants int. Caller must bridge the two."""
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    patch_ttsgen.fake.next_results = ["ok"]
    caller(
        _minimal_segment(segment_id="42"),
        tmp_path / "42.wav",
    )

    assert patch_ttsgen.fake.calls[0]["segment_id"] == 42


def test_caller_maps_split_segment_id_to_stable_numeric_surrogate(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    """Post-edit split ids look like 11_b, while TTSGenerator still uses
    segment_id with integer formatting for its temporary wav name."""
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    patch_ttsgen.fake.next_results = ["ok"]
    output = tmp_path / "11_b.wav"

    caller(
        _minimal_segment(segment_id="11_b"),
        output,
    )

    assert output.is_file()
    assert patch_ttsgen.fake.calls[0]["segment_id"] == 11002


def test_caller_maps_generic_editing_segment_id_to_stable_surrogate(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    patch_ttsgen.fake.next_results = ["ok", "ok"]

    caller(_minimal_segment(segment_id="seg_001"), tmp_path / "seg_001.wav")
    caller(_minimal_segment(segment_id="seg_001"), tmp_path / "seg_001_second.wav")

    first = patch_ttsgen.fake.calls[0]["segment_id"]
    second = patch_ttsgen.fake.calls[1]["segment_id"]
    assert isinstance(first, int)
    assert first == second


def test_caller_rejects_missing_segment_id(tmp_path: Path, patch_ttsgen: Any) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    seg = _minimal_segment()
    seg.pop("segment_id")

    with pytest.raises(ValueError, match="segment_id"):
        caller(seg, tmp_path / "x.wav")


def test_caller_rejects_empty_segment_id(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    with pytest.raises(ValueError, match="non-empty"):
        caller(
            _minimal_segment(segment_id=""),
            tmp_path / "x.wav",
        )


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_caller_retries_transient_tts_error_then_succeeds(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller()
    gen = patch_ttsgen.fake  # also primes it
    # First two attempts raise, third succeeds.
    gen.next_results = [
        TTSGenerationError("429 rate limit"),
        TTSGenerationError("transient network blip"),
        "ok",
    ]

    output = tmp_path / "retry.wav"
    caller(_minimal_segment(), output)

    assert output.is_file()
    # 3 attempts = 2 failures + 1 success.
    assert len(gen.calls) == 3
    # All three calls kept the SAME provider — the contract guarantees
    # we never silently switch providers under retry.
    providers = {call["provider_arg"] for call in gen.calls}
    assert providers == {"minimax"}


def test_caller_exhausts_retries_and_raises_with_provider_in_message(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller(max_retries=2)  # 3 total attempts
    gen = patch_ttsgen.fake
    gen.next_results = [
        TTSGenerationError("fail #1"),
        TTSGenerationError("fail #2"),
        TTSGenerationError("fail #3"),
    ]

    seg = _minimal_segment(tts_provider="cosyvoice")
    with pytest.raises(RuntimeError) as exc_info:
        caller(seg, tmp_path / "out.wav")

    msg = str(exc_info.value)
    assert "cosyvoice" in msg, f"error message must mention the provider: {msg}"
    assert "3 attempts" in msg, f"error message must mention total attempts: {msg}"
    assert len(gen.calls) == 3


def test_caller_does_not_fall_back_to_a_different_provider(
    tmp_path: Path, patch_ttsgen: Any,
) -> None:
    """Regression pin for the paid-API policy: pipeline's
    _generate_one_with_backoff auto-substitutes providers (MiniMax →
    CosyVoice) on exhaustion. The interactive caller MUST NOT — billing
    and audio character would silently shift under the user's feet."""
    from services.tts.segment_regenerate import build_real_segment_tts_caller

    caller = build_real_segment_tts_caller(max_retries=2)
    gen = patch_ttsgen.fake
    gen.next_results = [
        TTSGenerationError("primary fail 1"),
        TTSGenerationError("primary fail 2"),
        TTSGenerationError("primary fail 3"),
    ]

    seg = _minimal_segment(tts_provider="minimax")
    with pytest.raises(RuntimeError):
        caller(seg, tmp_path / "out.wav")

    # Every attempt must have used the SAME provider.
    providers_seen = [call["provider_arg"] for call in gen.calls]
    assert providers_seen == ["minimax", "minimax", "minimax"], (
        f"expected all attempts on 'minimax', got {providers_seen}"
    )


# ---------------------------------------------------------------------------
# Lazy init
# ---------------------------------------------------------------------------


def test_factory_does_not_instantiate_generator_until_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the factory at Job API startup must not touch credentials.
    If it did, a missing AUTODUB_TTS_API_KEY would block the Job API from
    booting instead of surfacing to the specific user clicking 重合成."""
    from services.tts import segment_regenerate as mod

    # If TTSGenerator or load_tts_config are called during factory setup,
    # these patches would blow up — the test fails if factory is eager.
    def _boom_ctor(cfg: object) -> None:
        raise AssertionError("TTSGenerator should not be constructed until first call")

    def _boom_config() -> None:
        raise AssertionError("load_tts_config should not run during factory setup")

    monkeypatch.setattr(mod, "TTSGenerator", _boom_ctor)
    monkeypatch.setattr(mod, "load_tts_config", _boom_config)

    # Factory call: must succeed without touching either.
    caller = mod.build_real_segment_tts_caller()
    assert caller is not None

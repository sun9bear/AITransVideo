from pathlib import Path
import shutil
import subprocess

from pydub import AudioSegment
from pydub.generators import Sine
import pytest

from services.alignment.aligner import AlignmentError, PostTTSBudgetTracker, SegmentAligner
import services.alignment.aligner as aligner_module
from services.tts.tts_generator import TTSResult
from services.gemini.translator import DubbingSegment


def _export_tone_wav(path: Path, *, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Sine(440).to_audio_segment(duration=duration_ms).export(path, format="wav")
    return path


def _build_segment(
    *,
    segment_id: int,
    audio_path: Path,
    start_ms: int = 0,
    end_ms: int = 1_000,
    actual_duration_ms: int | None = None,
) -> DubbingSegment:
    measured_duration_ms = len(AudioSegment.from_wav(audio_path))
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_demo_001",
        start_ms=start_ms,
        end_ms=end_ms,
        target_duration_ms=end_ms - start_ms,
        source_text="Demo source text.",
        cn_text="demo tts cn text",
        tts_audio_path=str(audio_path.resolve(strict=False)),
        actual_duration_ms=measured_duration_ms if actual_duration_ms is None else actual_duration_ms,
    )


def _error_ratio(actual_ms: int, target_ms: int) -> float:
    return abs(actual_ms - target_ms) / target_ms


def _assert_jianying_wav_format(path: Path) -> None:
    audio = AudioSegment.from_wav(path)
    assert audio.frame_rate == 44_100
    assert audio.channels == 2
    assert audio.sample_width == 2


def test_aligner_uses_direct_copy_when_diff_is_within_ideal_threshold(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "direct.wav", duration_ms=1_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=1_020)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    output_path = Path(aligned.aligned_audio_path)
    assert aligned.alignment_method == "direct"
    assert output_path.exists()
    assert len(AudioSegment.from_wav(output_path)) == 1_000
    assert output_path.name == "segment_001_aligned.wav"
    _assert_jianying_wav_format(output_path)


def test_aligner_avoids_direct_copy_for_long_overflow_even_within_ideal_ratio(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "overflow_dsp.wav", duration_ms=66_800)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=64_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    output_path = Path(aligned.aligned_audio_path)
    actual_ms = len(AudioSegment.from_wav(output_path))
    assert aligned.alignment_method == "dsp"
    assert output_path.exists()
    assert _error_ratio(actual_ms, 64_000) <= 0.05


def test_aligner_uses_dsp_when_diff_is_within_dsp_threshold(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "dsp.wav", duration_ms=17_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=14_900)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    output_path = Path(aligned.aligned_audio_path)
    actual_ms = len(AudioSegment.from_wav(output_path))
    assert aligned.alignment_method == "dsp"
    assert output_path.exists()
    assert _error_ratio(actual_ms, 14_900) <= 0.05
    _assert_jianying_wav_format(output_path)


def test_aligner_uses_force_dsp_and_marks_review_when_diff_exceeds_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "force.wav", duration_ms=10_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=6_000)
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 6_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert Path(aligned.aligned_audio_path).exists()


def test_aligner_short_force_dsp_backchannel_does_not_require_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short_force.wav", duration_ms=1_200)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=500)
    segment.cn_text = "嗯，是的。"
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 500)

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    assert aligned.alignment_method == "capped_dsp_overflow"
    assert aligned.needs_review is False
    assert segment.needs_review is False
    assert segment.force_dsp_severity == "low"
    assert segment.force_dsp_review_suppressed is True
    assert segment.force_dsp_review_reason == "short_backchannel"


def test_aligner_caps_short_force_dsp_target_for_listenability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "tiny_force.wav", duration_ms=2_400)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=500)
    segment.cn_text = "嗯，好。"
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    captured: dict[str, int] = {}

    def fake_dsp(
        self: SegmentAligner,
        _input_path: str,
        target_duration_ms: int,
        _output_path: str,
        **_kwargs: object,
    ) -> tuple[str, None]:
        # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None).
        captured["target_duration_ms"] = target_duration_ms
        return str(output_path), None

    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", fake_dsp)
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 1_371)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "capped_dsp_overflow"
    assert captured["target_duration_ms"] == 1_371
    assert aligned.actual_duration_ms == 1_371


def test_aligner_short_force_dsp_long_text_still_requires_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short_force_long_text.wav", duration_ms=1_200)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=500)
    segment.cn_text = "这是一个明显不是短反馈的完整句子，仍然需要人工检查，不能自动降噪。"
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 500)

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    # Long content (>SHORT_LISTENABLE_DSP_MAX_SPOKEN_CHARS=28) deliberately
    # bypasses the listenable-cap path: timeline alignment beats listenability
    # for real content, so the segment goes to uncapped force_dsp. The chars
    # threshold is the same constant that gates "medium" severity, so crossing
    # it also bumps severity to "high" via the long_or_contentful_segment
    # branch in _classify_force_dsp_review — review is still required.
    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert segment.needs_review is True
    assert segment.force_dsp_severity == "high"
    assert segment.force_dsp_review_suppressed is False


def test_aligner_two_second_force_dsp_backchannel_is_review_denoised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "two_second_short_force.wav", duration_ms=4_200)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=1_800)
    segment.cn_text = "ok yes"
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 1_800)

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    assert aligned.alignment_method == "capped_dsp_overflow"
    assert aligned.needs_review is False
    assert segment.force_dsp_severity == "low"
    assert segment.force_dsp_review_suppressed is True


def test_aligner_two_to_five_second_force_dsp_keeps_review_with_medium_severity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "medium_short_force.wav", duration_ms=5_500)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=3_000)
    segment.cn_text = "short medium"
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 3_000)

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert segment.force_dsp_severity == "medium"
    assert segment.force_dsp_review_suppressed is False


def test_aligner_marks_pre_tts_contradiction_after_first_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short_after_pre_rewrite.wav", duration_ms=16_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=20_000)
    segment.pre_tts_rewrite_direction = "overshoot"
    segment.pre_tts_estimate_ms = 26_666
    segment.pre_tts_target_ms = 20_000
    segment.pre_tts_pre_chars = 120
    segment.pre_tts_post_chars = 80
    output_path = tmp_path / "aligned" / "segment_001_aligned.wav"
    output_path.parent.mkdir(parents=True)
    shutil.copyfile(input_path, output_path)
    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # mocks return None for FitResult since these tests don't exercise audit fields.
    monkeypatch.setattr(SegmentAligner, "_dsp_stretch", lambda self, *_args, **_kwargs: (str(output_path), None))
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 16_000)

    SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert segment.first_pass_duration_ms == 16_000
    assert segment.pre_tts_post_tts_first_pass_ms == 16_000
    assert segment.pre_tts_contradiction is True
    assert segment.pre_tts_harmful_contradiction is True


def test_aligner_marks_direct_pre_tts_contradiction_as_not_harmful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "direct_after_pre_rewrite.wav", duration_ms=18_800)
    segment = _build_segment(
        segment_id=1,
        audio_path=input_path,
        end_ms=20_000,
        actual_duration_ms=18_800,
    )
    segment.pre_tts_rewrite_direction = "overshoot"
    segment.pre_tts_estimate_ms = 26_666
    segment.pre_tts_target_ms = 20_000
    segment.pre_tts_pre_chars = 120
    segment.pre_tts_post_chars = 88
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda _path: 18_800)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "direct"
    assert segment.pre_tts_contradiction is True
    assert segment.pre_tts_harmful_contradiction is False


def test_aligner_supports_slowdown_when_tts_is_shorter_than_target(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "slow.wav", duration_ms=17_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=19_500)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    output_path = Path(aligned.aligned_audio_path)
    actual_ms = len(AudioSegment.from_wav(output_path))
    assert aligned.alignment_method == "dsp"
    assert _error_ratio(actual_ms, 19_500) <= 0.05


def test_aligner_aligns_multiple_segments_with_mixed_methods(tmp_path: Path) -> None:
    direct_path = _export_tone_wav(tmp_path / "input" / "direct.wav", duration_ms=1_000)
    dsp_path = _export_tone_wav(tmp_path / "input" / "dsp.wav", duration_ms=17_000)
    force_path = _export_tone_wav(tmp_path / "input" / "force.wav", duration_ms=10_000)
    segments = [
        _build_segment(segment_id=1, audio_path=direct_path, end_ms=1_020),
        _build_segment(segment_id=2, audio_path=dsp_path, start_ms=1_020, end_ms=15_920),
        _build_segment(segment_id=3, audio_path=force_path, start_ms=15_920, end_ms=21_920),
    ]

    aligned_segments = SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    assert len(aligned_segments) == 3
    assert [segment.alignment_method for segment in aligned_segments] == ["direct", "dsp", "force_dsp"]


def test_aligner_surfaces_missing_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "missing_ffmpeg.wav", duration_ms=17_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=14_900)

    def missing_ffmpeg(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda path: 17_000)
    monkeypatch.setattr(aligner_module.subprocess, "run", missing_ffmpeg)

    with pytest.raises(AlignmentError, match="Please install ffmpeg"):
        SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))


def test_aligner_caps_extreme_underflow_dsp_and_pads_silence(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "extreme.wav", duration_ms=1_000)
    aligner = SegmentAligner()

    # 2026-05-08 P2-17a-0: _dsp_stretch returns (path, FitResult | None);
    # the FitResult is no longer cached on self for thread safety.
    output_path, fit_result = aligner._dsp_stretch(
        str(input_path),
        2_500,
        str(tmp_path / "aligned" / "segment_001_aligned.wav"),
    )

    assert fit_result is not None
    assert Path(output_path).exists()
    assert len(AudioSegment.from_wav(output_path)) == pytest.approx(2_500, abs=10)
    assert fit_result.speed_ratio_used == pytest.approx(
        aligner_module.UNDERFLOW_LISTENABLE_DSP_MIN_SPEED_RATIO
    )
    assert fit_result.silence_padded_ms >= 800
    assert AudioSegment.from_wav(output_path)[-700:].dBFS < -60


def test_aligner_marks_capped_underflow_when_force_dsp_would_extreme_slowmo(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short_underflow.wav", duration_ms=1_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=5_000)
    segment.cn_text = "还剩十秒。"

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    assert aligned.alignment_method == "capped_dsp_underflow"
    assert aligned.needs_review is True
    assert segment.force_dsp_severity == "high"
    assert segment.dsp_speed_ratio_used == pytest.approx(
        aligner_module.UNDERFLOW_LISTENABLE_DSP_MIN_SPEED_RATIO
    )
    assert segment.dsp_silence_padded_ms >= 3_000
    assert len(AudioSegment.from_wav(aligned.aligned_audio_path)) == pytest.approx(5_000, abs=10)


def test_aligner_clears_dsp_audit_between_segments_when_next_segment_is_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_input = _export_tone_wav(tmp_path / "input" / "underflow.wav", duration_ms=1_000)
    second_input = _export_tone_wav(tmp_path / "input" / "direct.wav", duration_ms=1_000)
    first_segment = _build_segment(segment_id=1, audio_path=first_input, end_ms=5_000)
    second_segment = _build_segment(segment_id=2, audio_path=second_input, end_ms=1_020)
    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda path: len(AudioSegment.from_wav(path)))
    aligner = SegmentAligner(rewriter=None, tts_generator=None)

    first_aligned = aligner._align_one(first_segment, str(tmp_path / "aligned"))
    second_aligned = aligner._align_one(second_segment, str(tmp_path / "aligned"))

    assert first_aligned.alignment_method == "capped_dsp_underflow"
    assert first_segment.dsp_silence_padded_ms > 0
    assert second_aligned.alignment_method == "direct"
    assert second_segment.dsp_silence_padded_ms == 0
    assert second_segment.dsp_speed_ratio_used == pytest.approx(1.0)


def test_aligner_uses_direct_copy_when_absolute_diff_is_small_for_short_segment(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short.wav", duration_ms=800)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=2_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "direct"
    assert aligned.needs_review is False
    assert Path(aligned.aligned_audio_path).exists()


def test_aligner_keeps_force_dsp_for_long_segments_with_large_absolute_diff(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "long.wav", duration_ms=10_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=15_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert Path(aligned.aligned_audio_path).exists()


def test_aligner_uses_direct_copy_when_absolute_diff_equals_threshold(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "boundary.wav", duration_ms=3_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=5_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "direct"
    assert aligned.needs_review is False


def test_aligner_uses_rewrite_direct_when_rewrite_brings_duration_within_ideal_range(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "rewrite_direct.wav", duration_ms=13_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=10_000)

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            assert speaker_id == "speaker_a"
            assert cn_text == "demo tts cn text"
            assert actual_duration_ms == 13_000
            assert target_duration_ms == 10_000
            assert source_text == "Demo source text."
            return "rewrite direct text"

    class FakeTTSGenerator:
        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            audio_path = _export_tone_wav(Path(output_dir) / "segment_001_speaker_a.wav", duration_ms=9_500)
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=9_500,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "rewrite_direct"
    assert aligned.needs_review is False
    assert segment.cn_text == "rewrite direct text"
    assert segment.rewrite_count == 1


def test_aligner_uses_rewrite_dsp_when_rewrite_brings_duration_within_dsp_range(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "rewrite_dsp.wav", duration_ms=25_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=20_000)

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            assert speaker_id == "speaker_a"
            del cn_text, target_duration_ms
            assert actual_duration_ms == 25_000
            assert source_text == "Demo source text."
            return "rewrite dsp text"

    class FakeTTSGenerator:
        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            audio_path = _export_tone_wav(Path(output_dir) / "segment_001_speaker_a.wav", duration_ms=22_500)
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=22_500,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "rewrite_dsp"
    assert aligned.needs_review is False
    assert segment.cn_text == "rewrite dsp text"
    assert segment.rewrite_count == 1


def test_aligner_falls_back_to_force_dsp_after_max_rewrites(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "rewrite_force.wav", duration_ms=13_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=10_000)
    rewrite_calls: list[int] = []

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            del cn_text, actual_duration_ms, target_duration_ms, source_text, speaker_id
            rewrite_calls.append(1)
            return f"rewrite attempt {len(rewrite_calls)}"

    class FakeTTSGenerator:
        def __init__(self) -> None:
            self._durations = iter([12_400, 12_300])

        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            duration_ms = next(self._durations)
            audio_path = _export_tone_wav(
                Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                duration_ms=duration_ms,
            )
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
        max_rewrites=2,
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert segment.rewrite_count == 2
    assert len(rewrite_calls) == 2


def test_aligner_restores_best_rewrite_candidate_before_force_dsp_when_later_attempt_is_worse(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "rewrite_best.wav", duration_ms=13_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=10_000)

    class FakeRewriter:
        def __init__(self) -> None:
            self._texts = iter(["rewrite attempt 1", "rewrite attempt 2"])

        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            del cn_text, actual_duration_ms, target_duration_ms, source_text, speaker_id
            return next(self._texts)

    class FakeTTSGenerator:
        def __init__(self) -> None:
            self._results = iter(
                [
                    ("segment_001_rewrite_1.wav", 12_050),
                    ("segment_001_rewrite_2.wav", 13_500),
                ]
            )

        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            file_name, duration_ms = next(self._results)
            audio_path = _export_tone_wav(Path(output_dir) / file_name, duration_ms=duration_ms)
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
        max_rewrites=2,
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert segment.rewrite_count == 2
    assert segment.cn_text == "rewrite attempt 1"
    assert segment.tts_audio_path is not None
    assert Path(segment.tts_audio_path).name == "segment_001_rewrite_1.wav"


def test_aligner_falls_back_to_force_dsp_when_rewriter_is_missing(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "no_rewriter.wav", duration_ms=13_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=10_000)

    aligned = SegmentAligner(rewriter=None, tts_generator=None)._align_one(
        segment,
        str(tmp_path / "aligned"),
    )

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True


def test_aligner_skips_rewrite_for_short_targets(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "short_target.wav", duration_ms=7_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=4_000)

    class FailingRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            raise AssertionError("rewriter should not be called for short targets")

    aligned = SegmentAligner(
        rewriter=FailingRewriter(),
        tts_generator=object(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert segment.rewrite_count == 0


def test_aligner_skips_rewrite_for_extreme_ratio(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "extreme_ratio.wav", duration_ms=10_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=6_000)

    class FailingRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            raise AssertionError("rewriter should not be called for extreme ratios")

    aligned = SegmentAligner(
        rewriter=FailingRewriter(),
        tts_generator=object(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert segment.rewrite_count == 0


def test_aligner_attempts_rewrite_for_long_severe_underflow_segments(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "long_underflow.wav", duration_ms=20_000)
    segment = _build_segment(
        segment_id=1,
        audio_path=input_path,
        end_ms=40_000,
        actual_duration_ms=20_000,
    )
    observed: dict[str, object] = {}

    class FakeRewriter:
        def rewrite_for_duration_with_profile(
            self,
            cn_text: str,
            *,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
            preferred_min_ratio: float | None = None,
            preferred_max_ratio: float | None = None,
        ) -> str:
            observed["args"] = {
                "cn_text": cn_text,
                "actual_duration_ms": actual_duration_ms,
                "target_duration_ms": target_duration_ms,
                "source_text": source_text,
                "speaker_id": speaker_id,
                "preferred_min_ratio": preferred_min_ratio,
                "preferred_max_ratio": preferred_max_ratio,
            }
            return "expanded rewrite"

    class FakeTTSGenerator:
        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            audio_path = _export_tone_wav(
                Path(output_dir) / "segment_001_speaker_a.wav",
                duration_ms=37_000,
            )
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=37_000,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "rewrite_dsp"
    assert aligned.needs_review is False
    assert segment.cn_text == "expanded rewrite"
    assert segment.rewrite_count == 1
    assert observed["args"] == {
        "cn_text": "demo tts cn text",
        "actual_duration_ms": 20_000,
        "target_duration_ms": 40_000,
        "source_text": "Demo source text.",
        "speaker_id": "speaker_a",
        "preferred_min_ratio": 0.88,
        "preferred_max_ratio": 1.08,
    }


def test_aligner_directional_scoring_penalizes_shrink_undershoot_more_than_slight_overshoot() -> None:
    aligner = SegmentAligner()

    overshort_score = aligner._score_rewrite_candidate(
        actual_duration_ms=17_800,
        target_duration_ms=20_000,
        direction="shrink",
        attempt_index=0,
    )
    slight_overshoot_score = aligner._score_rewrite_candidate(
        actual_duration_ms=22_200,
        target_duration_ms=20_000,
        direction="shrink",
        attempt_index=0,
    )

    assert slight_overshoot_score < overshort_score


def test_aligner_requires_second_rewrite_when_shrink_overshoots_past_lower_bound(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "shrink_followup.wav", duration_ms=26_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=20_000, actual_duration_ms=26_000)
    observed_calls: list[tuple[int, int]] = []

    class FakeRewriter:
        def __init__(self) -> None:
            self._texts = iter(["rewrite attempt 1", "rewrite attempt 2"])

        def rewrite_for_duration_with_profile(
            self,
            cn_text: str,
            *,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
            preferred_min_ratio: float | None = None,
            preferred_max_ratio: float | None = None,
        ) -> str:
            del cn_text, source_text, speaker_id, preferred_min_ratio, preferred_max_ratio
            observed_calls.append((actual_duration_ms, target_duration_ms))
            return next(self._texts)

    class FakeTTSGenerator:
        def __init__(self) -> None:
            self._durations = iter([17_800, 19_600])

        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            duration_ms = next(self._durations)
            audio_path = _export_tone_wav(
                Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                duration_ms=duration_ms,
            )
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "rewrite_direct"
    assert aligned.needs_review is False
    assert segment.rewrite_count == 2
    assert observed_calls == [(26_000, 20_000), (17_800, 20_000)]


def test_aligner_requires_second_rewrite_when_expand_overshoots_upper_bound(
    tmp_path: Path,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "expand_followup.wav", duration_ms=12_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=20_000, actual_duration_ms=12_000)
    observed_calls: list[tuple[int, int]] = []

    class FakeRewriter:
        def __init__(self) -> None:
            self._texts = iter(["rewrite attempt 1", "rewrite attempt 2"])

        def rewrite_for_duration_with_profile(
            self,
            cn_text: str,
            *,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
            preferred_min_ratio: float | None = None,
            preferred_max_ratio: float | None = None,
        ) -> str:
            del cn_text, source_text, speaker_id, preferred_min_ratio, preferred_max_ratio
            observed_calls.append((actual_duration_ms, target_duration_ms))
            return next(self._texts)

    class FakeTTSGenerator:
        def __init__(self) -> None:
            self._durations = iter([22_000, 20_400])

        def _generate_one(self, segment: DubbingSegment, output_dir: str) -> TTSResult:
            duration_ms = next(self._durations)
            audio_path = _export_tone_wav(
                Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                duration_ms=duration_ms,
            )
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=duration_ms,
                voice_id=segment.voice_id,
            )

    aligned = SegmentAligner(
        rewriter=FakeRewriter(),
        tts_generator=FakeTTSGenerator(),
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "rewrite_direct"
    assert aligned.needs_review is False
    assert segment.rewrite_count == 2
    assert observed_calls == [(12_000, 20_000), (22_000, 20_000)]


def test_aligner_skips_rewrite_when_post_tts_budget_is_exhausted(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "budget.wav", duration_ms=25_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=20_000)
    budget_tracker = PostTTSBudgetTracker(max_extra_tts_per_root=3)
    assert budget_tracker.try_consume_for_segment(segment, 3) is True

    class FailingRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs) -> str:
            raise AssertionError("rewriter should not be called when post-TTS budget is exhausted")

    aligned = SegmentAligner(
        rewriter=FailingRewriter(),
        tts_generator=object(),
        post_tts_budget_tracker=budget_tracker,
    )._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert segment.rewrite_count == 0

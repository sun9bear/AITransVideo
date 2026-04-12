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


def test_aligner_uses_force_dsp_and_marks_review_when_diff_exceeds_threshold(tmp_path: Path) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "force.wav", duration_ms=10_000)
    segment = _build_segment(segment_id=1, audio_path=input_path, end_ms=6_000)

    aligned = SegmentAligner()._align_one(segment, str(tmp_path / "aligned"))

    assert aligned.alignment_method == "force_dsp"
    assert aligned.needs_review is True
    assert Path(aligned.aligned_audio_path).exists()


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


def test_aligner_builds_chained_atempo_filter_for_extreme_ratio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = _export_tone_wav(tmp_path / "input" / "extreme.wav", duration_ms=1_000)
    captured_commands: list[list[str]] = []

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check
        captured_commands.append(command)
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(aligner_module, "_measure_wav_duration_ms", lambda path: 1_000)
    monkeypatch.setattr(aligner_module.subprocess, "run", fake_run)

    output_path = SegmentAligner()._dsp_stretch(
        str(input_path),
        2_500,
        str(tmp_path / "aligned" / "segment_001_aligned.wav"),
    )

    assert Path(output_path).exists()
    filter_index = captured_commands[0].index("-filter:a") + 1
    assert captured_commands[0][filter_index] == "atempo=0.5,atempo=0.8"


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

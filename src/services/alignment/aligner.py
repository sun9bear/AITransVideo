from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from pydub import AudioSegment

from modules.output.project_output import AlignedSegment
from services.gemini.translator import DubbingSegment

FIRST_REWRITE_TARGET_RATIO_WINDOWS = {
    "shrink": (0.95, 1.12),
    "expand": (0.88, 1.08),
}
LATER_REWRITE_TARGET_RATIO_WINDOWS = {
    "shrink": (0.97, 1.07),
    "expand": (0.93, 1.03),
}
SEVERE_EXPAND_REWRITE_MIN_TARGET_MS = 20_000
SEVERE_EXPAND_REWRITE_MAX_RATIO = 0.55
DEFAULT_MAX_POST_TTS_ADJUSTMENTS_PER_SEGMENT = 3


class AlignmentError(Exception):
    pass


class PostTTSBudgetTracker:
    def __init__(
        self,
        max_extra_tts_per_root: int = DEFAULT_MAX_POST_TTS_ADJUSTMENTS_PER_SEGMENT,
    ) -> None:
        self.max_extra_tts_per_root = int(max_extra_tts_per_root)
        self._usage_by_root: dict[int, int] = {}
        self._segment_roots: dict[int, int] = {}

    def root_id_for_segment(self, segment: DubbingSegment) -> int:
        segment_id = int(segment.segment_id)
        return self._segment_roots.get(segment_id, segment_id)

    def register_child_segments(
        self,
        *,
        parent_segment: DubbingSegment,
        child_segments: list[DubbingSegment],
    ) -> int:
        root_id = self.root_id_for_segment(parent_segment)
        for child_segment in child_segments:
            self._segment_roots[int(child_segment.segment_id)] = root_id
        return root_id

    def remaining_for_segment(self, segment: DubbingSegment) -> int:
        root_id = self.root_id_for_segment(segment)
        used = self._usage_by_root.get(root_id, 0)
        return max(0, self.max_extra_tts_per_root - used)

    def try_consume_for_segment(self, segment: DubbingSegment, amount: int = 1) -> bool:
        root_id = self.root_id_for_segment(segment)
        used = self._usage_by_root.get(root_id, 0)
        normalized_amount = max(0, int(amount))
        if used + normalized_amount > self.max_extra_tts_per_root:
            return False
        self._usage_by_root[root_id] = used + normalized_amount
        return True


class SegmentAligner:
    def __init__(
        self,
        ideal_threshold: float = 0.05,
        dsp_threshold: float = 0.15,
        min_abs_diff_ms: int = 2_000,
        max_direct_overflow_ms: int = 500,
        rewriter: GeminiRewriter | None = None,
        tts_generator: TTSGenerator | None = None,
        max_rewrites: int = 2,
        min_rewrite_target_ms: int = 5_000,
        max_rewrite_ratio: float = 0.35,
        post_tts_budget_tracker: PostTTSBudgetTracker | None = None,
    ):
        self.ideal_threshold = ideal_threshold
        self.dsp_threshold = dsp_threshold
        self.min_abs_diff_ms = int(min_abs_diff_ms)
        self.max_direct_overflow_ms = int(max_direct_overflow_ms)
        self.rewriter = rewriter
        self.tts_generator = tts_generator
        self.max_rewrites = int(max_rewrites)
        self.min_rewrite_target_ms = int(min_rewrite_target_ms)
        self.max_rewrite_ratio = float(max_rewrite_ratio)
        self.post_tts_budget_tracker = post_tts_budget_tracker

    def align_all(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
    ) -> list[AlignedSegment]:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        return [self._align_one(segment, str(output_root)) for segment in segments]

    def _align_one(
        self,
        segment: DubbingSegment,
        output_dir: str,
    ) -> AlignedSegment:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / f"segment_{segment.segment_id:03d}_aligned.wav"

        target_duration_ms = int(segment.target_duration_ms)
        if target_duration_ms <= 0:
            raise AlignmentError("target_duration_ms must be positive for alignment.")

        current_actual_duration_ms = (
            int(segment.actual_duration_ms)
            if int(segment.actual_duration_ms) > 0
            else _measure_wav_duration_ms(_resolve_existing_audio_path(segment.tts_audio_path))
        )
        alignment_method = "force_dsp"
        needs_review = True
        aligned_duration_ms: int | None = None

        decision = self._evaluate_alignment(current_actual_duration_ms, target_duration_ms)
        if decision == "direct":
            input_path = _resolve_existing_audio_path(segment.tts_audio_path)
            aligned_audio_path = self._direct_copy(str(input_path), str(output_path))
            alignment_method = "direct"
            needs_review = False
        elif decision == "dsp":
            input_path = _resolve_existing_audio_path(segment.tts_audio_path)
            aligned_audio_path = self._dsp_stretch(str(input_path), target_duration_ms, str(output_path))
            alignment_method = "dsp"
            needs_review = False
        else:
            rewrite_outcome = self._attempt_rewrite_loop(
                segment=segment,
                output_path=str(output_path),
                current_actual_duration_ms=current_actual_duration_ms,
            )
            if rewrite_outcome is not None:
                aligned_audio_path, aligned_duration_ms, alignment_method, needs_review = rewrite_outcome
            else:
                input_path = _resolve_existing_audio_path(segment.tts_audio_path)
                aligned_audio_path = self._dsp_stretch(str(input_path), target_duration_ms, str(output_path))
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                alignment_method = "force_dsp"
                needs_review = True

        if aligned_duration_ms is None:
            aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))

        segment.aligned_audio_path = aligned_audio_path
        segment.actual_duration_ms = aligned_duration_ms
        segment.alignment_ratio = aligned_duration_ms / target_duration_ms if target_duration_ms > 0 else 0.0
        segment.alignment_method = alignment_method
        segment.needs_review = needs_review

        return AlignedSegment(
            segment_id=segment.segment_id,
            speaker_id=segment.speaker_id,
            display_name=segment.display_name,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            cn_text=segment.cn_text,
            aligned_audio_path=aligned_audio_path,
            actual_duration_ms=aligned_duration_ms,
            alignment_method=alignment_method,
            needs_review=needs_review,
        )

    def _attempt_rewrite_loop(
        self,
        *,
        segment: DubbingSegment,
        output_path: str,
        current_actual_duration_ms: int,
    ) -> tuple[str, int, str, bool] | None:
        target_duration_ms = int(segment.target_duration_ms)
        if not self._should_attempt_rewrite(current_actual_duration_ms, target_duration_ms):
            return None
        if self.rewriter is None or self.tts_generator is None:
            return None

        best_tts_cn_text = (segment.tts_cn_text or segment.cn_text).strip()
        best_tts_audio_path = segment.tts_audio_path
        best_actual_duration_ms = int(current_actual_duration_ms)
        best_alignment_ratio = (
            best_actual_duration_ms / target_duration_ms if target_duration_ms > 0 else 0.0
        )
        best_abs_diff_ms = abs(best_actual_duration_ms - target_duration_ms)
        attempted_rewrite = False
        best_direction = "shrink" if best_actual_duration_ms > target_duration_ms else "expand"
        best_score = self._score_rewrite_candidate(
            actual_duration_ms=best_actual_duration_ms,
            target_duration_ms=target_duration_ms,
            direction=best_direction,
            attempt_index=0,
        )

        for rewrite_attempt in range(self.max_rewrites):
            current_text = (segment.tts_cn_text or segment.cn_text).strip()
            rewrite_direction = "shrink" if current_actual_duration_ms > target_duration_ms else "expand"
            if not self._can_consume_post_tts_budget(segment):
                break
            preferred_min_ratio, preferred_max_ratio = self._get_rewrite_target_ratio_window(
                direction=rewrite_direction,
                attempt_index=rewrite_attempt,
            )
            rewritten_text = self._rewrite_segment_with_constraints(
                current_text=current_text,
                current_actual_duration_ms=current_actual_duration_ms,
                target_duration_ms=target_duration_ms,
                segment=segment,
                preferred_min_ratio=preferred_min_ratio,
                preferred_max_ratio=preferred_max_ratio,
            ).strip()
            if not rewritten_text or rewritten_text == current_text:
                break

            if not self._consume_post_tts_budget(segment):
                break
            attempted_rewrite = True
            segment.tts_cn_text = rewritten_text
            segment.rewrite_count += 1
            tts_result = self.tts_generator._generate_one(
                segment,
                str(Path(output_path).resolve(strict=False).parent),
            )
            segment.tts_audio_path = tts_result.audio_path
            segment.actual_duration_ms = tts_result.duration_ms
            if target_duration_ms > 0:
                segment.alignment_ratio = tts_result.duration_ms / target_duration_ms
            else:
                segment.alignment_ratio = 0.0

            new_actual_duration_ms = int(tts_result.duration_ms)
            new_score = self._score_rewrite_candidate(
                actual_duration_ms=new_actual_duration_ms,
                target_duration_ms=target_duration_ms,
                direction=rewrite_direction,
                attempt_index=rewrite_attempt,
            )
            if new_score < best_score:
                best_tts_cn_text = rewritten_text
                best_tts_audio_path = tts_result.audio_path
                best_actual_duration_ms = new_actual_duration_ms
                best_alignment_ratio = segment.alignment_ratio
                best_abs_diff_ms = abs(new_actual_duration_ms - target_duration_ms)
                best_score = new_score

            print(
                f"[S5] 重写第{rewrite_attempt + 1}次："
                f"{current_actual_duration_ms}ms -> {new_actual_duration_ms}ms"
                f"（目标{target_duration_ms}ms）"
            )
            decision = self._evaluate_alignment(new_actual_duration_ms, target_duration_ms)
            if self._should_force_followup_rewrite(
                actual_duration_ms=new_actual_duration_ms,
                target_duration_ms=target_duration_ms,
                direction=rewrite_direction,
                attempt_index=rewrite_attempt,
            ):
                current_actual_duration_ms = new_actual_duration_ms
                continue
            if decision == "direct":
                aligned_audio_path = self._direct_copy(tts_result.audio_path, output_path)
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_direct", False
            if decision == "dsp":
                aligned_audio_path = self._dsp_stretch(tts_result.audio_path, target_duration_ms, output_path)
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_dsp", False
            current_actual_duration_ms = new_actual_duration_ms

        if attempted_rewrite:
            segment.tts_cn_text = best_tts_cn_text
            segment.tts_audio_path = best_tts_audio_path
            segment.actual_duration_ms = best_actual_duration_ms
            segment.alignment_ratio = best_alignment_ratio
            best_decision = self._evaluate_alignment(best_actual_duration_ms, target_duration_ms)
            if best_decision == "direct":
                aligned_audio_path = self._direct_copy(best_tts_audio_path, output_path)
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_direct", False
            if best_decision == "dsp":
                aligned_audio_path = self._dsp_stretch(best_tts_audio_path, target_duration_ms, output_path)
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_dsp", False

        return None

    def _should_attempt_rewrite(self, actual_duration_ms: int, target_duration_ms: int) -> bool:
        if target_duration_ms < self.min_rewrite_target_ms:
            return False
        if target_duration_ms <= 0:
            return False
        diff_ratio = abs(actual_duration_ms - target_duration_ms) / target_duration_ms
        if diff_ratio <= self.dsp_threshold:
            return False
        if diff_ratio <= self.max_rewrite_ratio:
            return True
        if (
            actual_duration_ms < target_duration_ms
            and target_duration_ms >= SEVERE_EXPAND_REWRITE_MIN_TARGET_MS
        ):
            return diff_ratio <= SEVERE_EXPAND_REWRITE_MAX_RATIO
        return False

    def _evaluate_alignment(self, actual_duration_ms: int, target_duration_ms: int) -> str:
        diff_ratio = (actual_duration_ms - target_duration_ms) / target_duration_ms
        abs_diff_ms = abs(actual_duration_ms - target_duration_ms)
        if actual_duration_ms <= target_duration_ms and abs_diff_ms <= self.min_abs_diff_ms:
            return "direct"
        if (
            actual_duration_ms > target_duration_ms
            and abs_diff_ms <= self.max_direct_overflow_ms
            and abs(diff_ratio) <= self.ideal_threshold
        ):
            return "direct"
        if abs(diff_ratio) <= self.dsp_threshold:
            return "dsp"
        return "rewrite_or_force"

    def _rewrite_segment_with_constraints(
        self,
        *,
        current_text: str,
        current_actual_duration_ms: int,
        target_duration_ms: int,
        segment: DubbingSegment,
        preferred_min_ratio: float,
        preferred_max_ratio: float,
    ) -> str:
        rewrite_with_profile = getattr(self.rewriter, "rewrite_for_duration_with_profile", None)
        if callable(rewrite_with_profile):
            return rewrite_with_profile(
                current_text,
                actual_duration_ms=current_actual_duration_ms,
                target_duration_ms=target_duration_ms,
                source_text=segment.source_text,
                speaker_id=segment.speaker_id,
                preferred_min_ratio=preferred_min_ratio,
                preferred_max_ratio=preferred_max_ratio,
            )
        return self.rewriter.rewrite_for_duration(
            current_text,
            actual_duration_ms=current_actual_duration_ms,
            target_duration_ms=target_duration_ms,
            source_text=segment.source_text,
            speaker_id=segment.speaker_id,
        )

    def _get_rewrite_target_ratio_window(
        self,
        *,
        direction: str,
        attempt_index: int,
    ) -> tuple[float, float]:
        if attempt_index <= 0:
            return FIRST_REWRITE_TARGET_RATIO_WINDOWS[direction]
        return LATER_REWRITE_TARGET_RATIO_WINDOWS[direction]

    def _should_force_followup_rewrite(
        self,
        *,
        actual_duration_ms: int,
        target_duration_ms: int,
        direction: str,
        attempt_index: int,
    ) -> bool:
        if attempt_index >= self.max_rewrites - 1:
            return False
        lower_ratio, upper_ratio = self._get_rewrite_target_ratio_window(
            direction=direction,
            attempt_index=attempt_index,
        )
        actual_ratio = actual_duration_ms / target_duration_ms
        if direction == "shrink":
            return actual_ratio < lower_ratio
        return actual_ratio > upper_ratio

    def _score_rewrite_candidate(
        self,
        *,
        actual_duration_ms: int,
        target_duration_ms: int,
        direction: str,
        attempt_index: int,
    ) -> tuple[float, float, int]:
        lower_ratio, upper_ratio = self._get_rewrite_target_ratio_window(
            direction=direction,
            attempt_index=attempt_index,
        )
        actual_ratio = actual_duration_ms / target_duration_ms
        if actual_ratio < lower_ratio:
            distance_outside_window = lower_ratio - actual_ratio
        elif actual_ratio > upper_ratio:
            distance_outside_window = actual_ratio - upper_ratio
        else:
            distance_outside_window = 0.0
        abs_diff_ratio = abs(actual_ratio - 1.0)
        abs_diff_ms = abs(actual_duration_ms - target_duration_ms)
        return (distance_outside_window, abs_diff_ratio, abs_diff_ms)

    def _can_consume_post_tts_budget(self, segment: DubbingSegment) -> bool:
        if self.post_tts_budget_tracker is None:
            return True
        return self.post_tts_budget_tracker.remaining_for_segment(segment) > 0

    def _consume_post_tts_budget(self, segment: DubbingSegment) -> bool:
        if self.post_tts_budget_tracker is None:
            return True
        return self.post_tts_budget_tracker.try_consume_for_segment(segment, 1)

    def _dsp_stretch(
        self,
        input_path: str,
        target_duration_ms: int,
        output_path: str,
    ) -> str:
        input_audio_path = _resolve_existing_audio_path(input_path)
        if target_duration_ms <= 0:
            raise AlignmentError("target_duration_ms must be positive for DSP alignment.")

        actual_duration_ms = _measure_wav_duration_ms(input_audio_path)
        speed_ratio = actual_duration_ms / target_duration_ms
        filter_value = _build_atempo_filter(speed_ratio)
        output_audio_path = Path(output_path).resolve(strict=False)

        command = [
            "ffmpeg",
            "-i",
            str(input_audio_path),
            "-filter:a",
            filter_value,
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-y",
            str(output_audio_path),
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AlignmentError("ffmpeg was not found in PATH. Please install ffmpeg.") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise AlignmentError(
                "ffmpeg failed to stretch audio."
                + (f" stderr: {stderr}" if stderr else "")
            )
        if not output_audio_path.exists():
            raise AlignmentError("ffmpeg reported success but aligned output was not created.")
        return str(output_audio_path)

    def _direct_copy(self, input_path: str, output_path: str) -> str:
        input_audio_path = _resolve_existing_audio_path(input_path)
        output_audio_path = Path(output_path).resolve(strict=False)
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-i",
            str(input_audio_path),
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-y",
            str(output_audio_path),
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            shutil.copy2(input_audio_path, output_audio_path)
            return str(output_audio_path)

        if completed.returncode != 0 or not output_audio_path.exists():
            if output_audio_path.exists():
                output_audio_path.unlink()
            shutil.copy2(input_audio_path, output_audio_path)
        return str(output_audio_path)


def _resolve_existing_audio_path(path: str | None) -> Path:
    normalized_path = (path or "").strip()
    if not normalized_path:
        raise AlignmentError("Alignment requires a non-empty tts_audio_path.")
    resolved_path = Path(normalized_path).resolve(strict=False)
    if not resolved_path.exists():
        raise AlignmentError(f"Alignment input audio file not found: {resolved_path}")
    return resolved_path


def _measure_wav_duration_ms(path: str | Path) -> int:
    return len(AudioSegment.from_wav(path))


def _build_atempo_filter(speed_ratio: float) -> str:
    if speed_ratio <= 0:
        raise AlignmentError("speed_ratio must be positive.")

    remaining_ratio = float(speed_ratio)
    factors: list[float] = []

    while remaining_ratio < 0.5:
        factors.append(0.5)
        remaining_ratio /= 0.5
    while remaining_ratio > 2.0:
        factors.append(2.0)
        remaining_ratio /= 2.0

    factors.append(remaining_ratio)
    return ",".join(f"atempo={_format_atempo_factor(factor)}" for factor in factors)


def _format_atempo_factor(value: float) -> str:
    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted or "1"

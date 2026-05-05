from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from modules.output.project_output import AlignedSegment
from utils.audio_fit import FitPolicy, FitResult, fit_audio_to_slot
from utils.audio_utils import measure_duration_ms as _ffprobe_duration_ms
from utils.atomic_io import is_valid_output
from services.gemini.translator import DubbingSegment
from services.gemini.translator import DUBBING_MODE_DUB, DUBBING_MODE_KEEP_ORIGINAL
from services.gemini.translator import is_keep_original_dubbing_mode, normalize_dubbing_mode
from services.tts.duration_estimator import count_spoken_chars
from services.usage_meter import TTS_BUCKET_POST_TTS_RESYNTH


# Phase 2 force-DSP override — admin-controlled, read at every alignment call.
# 2026-05-05 Phase D-1: read logic factored into ``services.admin_settings`` so
# the Whisper-alignment reader and this one share one tested file-read path.
from services.admin_settings import read_admin_setting


def _is_force_dsp_alignment_enabled() -> bool:
    """Read admin_settings.force_dsp_alignment. Defaults to False on any read
    failure so the existing rewrite/dsp/direct decision tree stays in effect.
    """
    return bool(read_admin_setting("force_dsp_alignment", default=False))


def _snapshot_first_pass_text(segment: DubbingSegment) -> None:
    """Snapshot the segment's CURRENT cn_text into the audit fields.

    Called at every TTS pass entry into alignment (initial pass, post-TTS
    rewrite re-pass). Two fields, snapshotted at the same time but with
    different mutability:

    - ``first_pass_cn_text`` — set ONLY the first time. Preserved across
      subsequent post-TTS rewrites because downstream voice-speed-profile
      sampling pairs ``first_pass_duration_ms`` with this text and would
      poison the profile if paired with a rewritten version
      (see ``pipeline/process.py:7423-7425``).

    - ``tts_input_cn_text`` — ALWAYS overwritten with the current cn_text.
      Records "the text that produced the audio currently on disk".
      Re-stamped on every post-TTS rewrite. Never re-stamped at user-edit
      time (the editing endpoints don't call this), which is exactly how
      we detect text↔audio drift downstream.

    Empty / whitespace-only cn_text is treated defensively: don't poison
    a previously-set non-empty stamp with "". TTS never runs on empty
    text in normal flow, so this only affects pathological inputs.
    """
    current = (segment.cn_text or "").strip()
    if not current:
        return
    if not getattr(segment, "first_pass_cn_text", ""):
        segment.first_pass_cn_text = current
    segment.tts_input_cn_text = current

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
SHORT_FORCE_DSP_LOW_MAX_TARGET_MS = 2_000
SHORT_FORCE_DSP_LOW_MAX_SPOKEN_CHARS = 18
SHORT_FORCE_DSP_LOW_MAX_FIRST_PASS_MS = 4_500
SHORT_FORCE_DSP_LOW_MAX_FIRST_PASS_RATIO = 4.0
SHORT_FORCE_DSP_MEDIUM_MAX_TARGET_MS = 5_000
SHORT_FORCE_DSP_MEDIUM_MAX_SPOKEN_CHARS = 28
SHORT_LISTENABLE_DSP_MAX_TARGET_MS = 2_000
SHORT_LISTENABLE_DSP_MAX_FIRST_PASS_MS = 4_500
SHORT_LISTENABLE_DSP_MAX_SPOKEN_CHARS = 28
SHORT_LISTENABLE_DSP_MAX_SPEED_RATIO = 1.75
SHORT_LISTENABLE_DSP_POLICY = FitPolicy(
    atempo_max=SHORT_LISTENABLE_DSP_MAX_SPEED_RATIO,
)
UNDERFLOW_LISTENABLE_DSP_MIN_SPEED_RATIO = 0.67
CAPPED_UNDERFLOW_MIN_SILENCE_PAD_MS = 250
DEFAULT_ALIGNMENT_DSP_POLICY = FitPolicy(
    atempo_min=UNDERFLOW_LISTENABLE_DSP_MIN_SPEED_RATIO,
    atempo_max=100.0,
)
_FORCE_DSP_REVIEW_METHODS = {
    "force_dsp",
    "capped_dsp_overflow",
    "capped_dsp_underflow",
}


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
        self._last_dsp_fit_result: FitResult | None = None

    def align_all(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
    ) -> list[AlignedSegment]:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        results: list[AlignedSegment] = []
        total_segments = len(segments)
        for index, segment in enumerate(segments, start=1):
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                results.append(self._keep_original_result(segment))
                if total_segments > 0 and (index % 15 == 0 or index == total_segments):
                    print(f"[S5] 对齐进度: {index}/{total_segments} 段")
                continue
            output_path = output_root / f"segment_{segment.segment_id:03d}_aligned.wav"
            if is_valid_output(str(output_path)):
                print(f"[S5] 跳过已完成的对齐段 {index}/{total_segments}")
                duration_ms = _ffprobe_duration_ms(output_path)
                target_duration_ms = int(segment.target_duration_ms)
                segment.aligned_audio_path = str(output_path)
                segment.actual_duration_ms = duration_ms
                segment.alignment_ratio = duration_ms / target_duration_ms if target_duration_ms > 0 else 0.0
                results.append(AlignedSegment(
                    segment_id=segment.segment_id,
                    speaker_id=segment.speaker_id,
                    display_name=segment.display_name,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    cn_text=segment.cn_text,
                    en_text=getattr(segment, "en_text", ""),
                    aligned_audio_path=str(output_path),
                    actual_duration_ms=duration_ms,
                    alignment_method="checkpoint",
                    needs_review=False,
                    dubbing_mode=normalize_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)),
                ))
                if total_segments > 0 and (index % 15 == 0 or index == total_segments):
                    print(f"[S5] 对齐进度: {index}/{total_segments} 段")
                continue
            results.append(self._align_one(segment, str(output_root)))
            if total_segments > 0 and (index % 15 == 0 or index == total_segments):
                print(f"[S5] 对齐进度: {index}/{total_segments} 段")
        return results

    def _keep_original_result(self, segment: DubbingSegment) -> AlignedSegment:
        audio_path = segment.aligned_audio_path or segment.tts_audio_path
        if not audio_path:
            raise AlignmentError("keep_original segment is missing source audio slice.")
        resolved_audio_path = _resolve_existing_audio_path(audio_path)
        duration_ms = _measure_wav_duration_ms(resolved_audio_path)
        target_duration_ms = int(segment.target_duration_ms)
        segment.tts_audio_path = str(resolved_audio_path)
        segment.aligned_audio_path = str(resolved_audio_path)
        segment.actual_duration_ms = duration_ms
        segment.alignment_ratio = duration_ms / target_duration_ms if target_duration_ms > 0 else 1.0
        segment.alignment_method = DUBBING_MODE_KEEP_ORIGINAL
        segment.needs_review = False
        return AlignedSegment(
            segment_id=segment.segment_id,
            speaker_id=segment.speaker_id,
            display_name=segment.display_name,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            cn_text=segment.cn_text,
            en_text=getattr(segment, "en_text", ""),
            aligned_audio_path=str(resolved_audio_path),
            actual_duration_ms=duration_ms,
            alignment_method=DUBBING_MODE_KEEP_ORIGINAL,
            needs_review=False,
            dubbing_mode=DUBBING_MODE_KEEP_ORIGINAL,
        )

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
        # Phase 2 Task 0 — snapshot first-pass duration BEFORE any rewrite/DSP.
        # This is the raw TTS output as-is; any subsequent rewrite or DSP would
        # overwrite segment.actual_duration_ms below, so we save it here once.
        # `target_duration_ms > 0` is already guaranteed by the check above.
        segment.first_pass_duration_ms = current_actual_duration_ms
        # 2026-05-04 P0a — snapshot the text we just synthesized. Captures
        # tts_input_cn_text every pass; first_pass_cn_text only first pass.
        _snapshot_first_pass_text(segment)
        segment.first_pass_error_pct = (
            (current_actual_duration_ms - target_duration_ms) / target_duration_ms
        )
        pre_tts_direction = (getattr(segment, "pre_tts_rewrite_direction", "") or "").lower()
        if pre_tts_direction:
            segment.pre_tts_post_tts_first_pass_ms = current_actual_duration_ms
            error_pct = segment.first_pass_error_pct
            segment.pre_tts_contradiction = (
                (pre_tts_direction in {"overshoot", "shrink", "too_long"} and error_pct < -0.05)
                or (pre_tts_direction in {"undershoot", "expand", "too_short"} and error_pct > 0.05)
            )

        alignment_method = "force_dsp"
        needs_review = True
        aligned_duration_ms: int | None = None
        self._last_dsp_fit_result = None
        self._clear_dsp_fit_audit(segment)

        # Phase 2 force-DSP override — when admin enables `force_dsp_alignment`,
        # bypass the rewrite/direct/dsp decision entirely and always stretch
        # the raw TTS audio to the target duration. Trades quality for hard
        # time alignment. Useful when LLM length control is unreliable AND
        # the user prefers slight DSP artefact over rewrite churn.
        force_dsp_user = _is_force_dsp_alignment_enabled()
        if force_dsp_user:
            input_path = _resolve_existing_audio_path(segment.tts_audio_path)
            aligned_audio_path = self._dsp_stretch(
                str(input_path), target_duration_ms, str(output_path),
            )
            alignment_method = "force_dsp_user"
            needs_review = False
            aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
            # Skip the decision tree, common post-processing below will pick up
            # `aligned_duration_ms is not None` and write back to the segment.
            decision = None
        else:
            decision = self._evaluate_alignment(current_actual_duration_ms, target_duration_ms)
        if decision is None:
            pass  # already handled by force_dsp branch above
        elif decision == "direct":
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
                if self._should_use_listenable_short_dsp(
                    segment=segment,
                    actual_duration_ms=current_actual_duration_ms,
                    target_duration_ms=target_duration_ms,
                ):
                    listenable_target_ms = self._listenable_short_dsp_target_ms(
                        actual_duration_ms=current_actual_duration_ms,
                    )
                    aligned_audio_path = self._dsp_stretch(
                        str(input_path),
                        listenable_target_ms,
                        str(output_path),
                        policy=SHORT_LISTENABLE_DSP_POLICY,
                    )
                    alignment_method = "capped_dsp_overflow"
                else:
                    aligned_audio_path = self._dsp_stretch(
                        str(input_path),
                        target_duration_ms,
                        str(output_path),
                    )
                    alignment_method = "force_dsp"
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                needs_review = True

        if aligned_duration_ms is None:
            aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))

        self._apply_dsp_fit_audit(segment)
        if (
            alignment_method in {"force_dsp", "force_dsp_user"}
            and self._last_dsp_fit_was_capped_underflow()
        ):
            alignment_method = "capped_dsp_underflow"

        if alignment_method in _FORCE_DSP_REVIEW_METHODS:
            severity, suppress_review, review_reason = self._classify_force_dsp_review(
                segment=segment,
                actual_duration_ms=current_actual_duration_ms,
                target_duration_ms=target_duration_ms,
            )
            segment.force_dsp_severity = severity
            segment.force_dsp_review_reason = review_reason
            segment.force_dsp_review_suppressed = False
            if suppress_review and needs_review:
                needs_review = False
                segment.force_dsp_review_suppressed = True
        else:
            segment.force_dsp_severity = ""
            segment.force_dsp_review_suppressed = False
            segment.force_dsp_review_reason = ""

        segment.aligned_audio_path = aligned_audio_path
        segment.actual_duration_ms = aligned_duration_ms
        segment.alignment_ratio = aligned_duration_ms / target_duration_ms if target_duration_ms > 0 else 0.0
        segment.alignment_method = alignment_method
        segment.needs_review = needs_review
        segment.pre_tts_harmful_contradiction = (
            bool(getattr(segment, "pre_tts_contradiction", False))
            and self._is_pre_tts_contradiction_harmful(
                alignment_method=alignment_method,
                needs_review=needs_review,
            )
        )

        return AlignedSegment(
            segment_id=segment.segment_id,
            speaker_id=segment.speaker_id,
            display_name=segment.display_name,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            cn_text=segment.cn_text,
            en_text=getattr(segment, "en_text", ""),
            aligned_audio_path=aligned_audio_path,
            actual_duration_ms=aligned_duration_ms,
            alignment_method=alignment_method,
            needs_review=needs_review,
            dubbing_mode=normalize_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)),
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

        best_cn_text = segment.cn_text.strip()
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
            current_text = segment.cn_text.strip()
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
            segment.cn_text = rewritten_text
            segment.rewrite_count += 1
            try:
                tts_result = self.tts_generator._generate_one(
                    segment,
                    str(Path(output_path).resolve(strict=False).parent),
                    usage_bucket=TTS_BUCKET_POST_TTS_RESYNTH,
                )
            except TypeError as exc:
                if "usage_bucket" not in str(exc):
                    raise
                tts_result = self.tts_generator._generate_one(
                    segment,
                    str(Path(output_path).resolve(strict=False).parent),
                )
            segment.tts_audio_path = tts_result.audio_path
            segment.actual_duration_ms = tts_result.duration_ms
            # 2026-05-04 P0a — the audio at tts_audio_path now reflects
            # ``rewritten_text`` (segment.cn_text was just updated above).
            # Re-stamp tts_input_cn_text so any subsequent comparison knows
            # which text the audio comes from. first_pass_cn_text stays
            # immutable (the helper guards on "if not already set").
            _snapshot_first_pass_text(segment)
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
                best_cn_text = rewritten_text
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
            segment.cn_text = best_cn_text
            segment.tts_audio_path = best_tts_audio_path
            segment.actual_duration_ms = best_actual_duration_ms
            segment.alignment_ratio = best_alignment_ratio
            # 2026-05-04 P0a — best-candidate finalization re-applies cn_text
            # to whichever attempt scored best (may differ from last attempt).
            # Re-stamp tts_input_cn_text so it tracks the audio finally used.
            _snapshot_first_pass_text(segment)
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

    def _should_use_listenable_short_dsp(
        self,
        *,
        segment: DubbingSegment,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> bool:
        if target_duration_ms <= 0 or actual_duration_ms <= target_duration_ms:
            return False
        if target_duration_ms > SHORT_LISTENABLE_DSP_MAX_TARGET_MS:
            return False
        if actual_duration_ms > SHORT_LISTENABLE_DSP_MAX_FIRST_PASS_MS:
            return False
        if count_spoken_chars(segment.cn_text) > SHORT_LISTENABLE_DSP_MAX_SPOKEN_CHARS:
            return False
        return (
            actual_duration_ms / target_duration_ms
            > SHORT_LISTENABLE_DSP_MAX_SPEED_RATIO
        )

    @staticmethod
    def _listenable_short_dsp_target_ms(*, actual_duration_ms: int) -> int:
        return max(
            1,
            int(round(actual_duration_ms / SHORT_LISTENABLE_DSP_MAX_SPEED_RATIO)),
        )

    def _is_short_force_dsp_expected(
        self,
        *,
        segment: DubbingSegment,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> bool:
        _severity, suppress_review, _reason = self._classify_force_dsp_review(
            segment=segment,
            actual_duration_ms=actual_duration_ms,
            target_duration_ms=target_duration_ms,
        )
        return suppress_review

    def _classify_force_dsp_review(
        self,
        *,
        segment: DubbingSegment,
        actual_duration_ms: int,
        target_duration_ms: int,
    ) -> tuple[str, bool, str]:
        """Classify final force-DSP risk without another LLM rewrite.

        Only very short, low-information backchannels are auto-denoised from
        manual review. 2-5s force-DSP is still surfaced, but marked medium so
        downstream reports can separate it from genuinely long/high-risk drift.
        """
        if target_duration_ms <= 0 or actual_duration_ms <= 0:
            return "high", False, "invalid_duration"

        spoken_chars = count_spoken_chars(segment.cn_text)
        first_pass_ratio = actual_duration_ms / target_duration_ms

        if (
            target_duration_ms <= SHORT_FORCE_DSP_LOW_MAX_TARGET_MS
            and spoken_chars <= SHORT_FORCE_DSP_LOW_MAX_SPOKEN_CHARS
            and actual_duration_ms <= SHORT_FORCE_DSP_LOW_MAX_FIRST_PASS_MS
            and first_pass_ratio <= SHORT_FORCE_DSP_LOW_MAX_FIRST_PASS_RATIO
        ):
            return "low", True, "short_backchannel"

        if (
            target_duration_ms <= SHORT_FORCE_DSP_MEDIUM_MAX_TARGET_MS
            and spoken_chars <= SHORT_FORCE_DSP_MEDIUM_MAX_SPOKEN_CHARS
        ):
            return "medium", False, "short_segment"

        return "high", False, "long_or_contentful_segment"

    @staticmethod
    def _clear_dsp_fit_audit(segment: DubbingSegment) -> None:
        segment.dsp_speed_ratio_used = 1.0
        segment.dsp_silence_padded_ms = 0
        segment.dsp_truncated_ms = 0
        segment.dsp_initial_duration_ms = 0
        segment.dsp_trimmed_duration_ms = 0
        segment.dsp_stretched_duration_ms = 0

    def _apply_dsp_fit_audit(self, segment: DubbingSegment) -> None:
        fit_result = self._last_dsp_fit_result
        if fit_result is None:
            return
        segment.dsp_speed_ratio_used = float(fit_result.speed_ratio_used)
        segment.dsp_silence_padded_ms = int(fit_result.silence_padded_ms)
        segment.dsp_truncated_ms = int(fit_result.truncated_ms)
        segment.dsp_initial_duration_ms = int(fit_result.initial_duration_ms)
        segment.dsp_trimmed_duration_ms = int(fit_result.trimmed_duration_ms)
        segment.dsp_stretched_duration_ms = int(fit_result.stretched_duration_ms)

    def _last_dsp_fit_was_capped_underflow(self) -> bool:
        fit_result = self._last_dsp_fit_result
        if fit_result is None:
            return False
        if fit_result.initial_duration_ms >= fit_result.final_duration_ms:
            return False
        if fit_result.silence_padded_ms < CAPPED_UNDERFLOW_MIN_SILENCE_PAD_MS:
            return False
        return (
            fit_result.speed_ratio_used
            <= UNDERFLOW_LISTENABLE_DSP_MIN_SPEED_RATIO + 1e-6
        )

    @staticmethod
    def _is_pre_tts_contradiction_harmful(
        *,
        alignment_method: str,
        needs_review: bool,
    ) -> bool:
        if needs_review:
            return True
        return alignment_method not in {"direct", "dsp"}

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
        *,
        policy: FitPolicy | None = None,
    ) -> str:
        self._last_dsp_fit_result = None
        input_audio_path = _resolve_existing_audio_path(input_path)
        if target_duration_ms <= 0:
            raise AlignmentError("target_duration_ms must be positive for DSP alignment.")

        output_audio_path = Path(output_path).resolve(strict=False)
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        fit_source_path = input_audio_path
        if input_audio_path.resolve(strict=False) != output_audio_path:
            shutil.copy2(input_audio_path, output_audio_path)
            fit_source_path = output_audio_path

        fit_result = fit_audio_to_slot(
            fit_source_path,
            target_duration_ms,
            output_path=output_audio_path,
            policy=policy or DEFAULT_ALIGNMENT_DSP_POLICY,
        )
        self._last_dsp_fit_result = fit_result
        if fit_result is not None and output_audio_path.exists():
            return str(output_audio_path)

        actual_duration_ms = _measure_wav_duration_ms(input_audio_path)
        speed_ratio = actual_duration_ms / target_duration_ms
        filter_value = _build_atempo_filter(speed_ratio)

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
    return _ffprobe_duration_ms(path)


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

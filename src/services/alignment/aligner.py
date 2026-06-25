from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import threading


# 2026-05-09 P2-17a-1: alignment thread pool envs.
#
# AVT_ALIGN_MAX_WORKERS: top-level concurrency for the expensive
# branch of align_all (cache miss / non-keep-original segments). 1
# means strict serial fallback (dispatches to _align_all_serial(),
# the verbatim pre-17a-1 body). Defaults to 2; clamped to [1, 4]
# so a misconfigured 32 doesn't blow up disk / CPU / ffmpeg / paid
# providers. Invalid / 0 / negative values fall back to 1.
#
# AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY: covers rewriter (Gemini)
# + TTS _generate_one (paid TTS) + best-candidate _dsp_stretch as
# ONE critical section inside _attempt_rewrite_loop. Default 1 is
# a CORRECTNESS constraint, not cost-conservatism — see
# docs/audits/2026-05-08-tts-rewriter-translator-state-audit.md
# for the four mutable shared fields that require it. Raising
# above 1 requires upgrading those fields to ① local-return or
# ② lock-protected first.
_DEFAULT_ALIGN_MAX_WORKERS = 2
_ALIGN_MAX_WORKERS_CAP = 4
_DEFAULT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY = 1


def _read_align_max_workers() -> int:
    raw = os.environ.get("AVT_ALIGN_MAX_WORKERS", "")
    try:
        value = int(raw) if raw.strip() else _DEFAULT_ALIGN_MAX_WORKERS
    except ValueError:
        value = 1
    if value < 1:
        return 1
    if value > _ALIGN_MAX_WORKERS_CAP:
        return _ALIGN_MAX_WORKERS_CAP
    return value


def _read_align_paid_fallback_max_concurrency() -> int:
    raw = os.environ.get("AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY", "")
    try:
        value = int(raw) if raw.strip() else _DEFAULT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY
    except ValueError:
        return 1
    if value < 1:
        return 1
    return value

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
    """Per-root post-TTS rewrite/regeneration budget.

    Thread safety (P2-17a-0, 2026-05-08): all four public methods take
    ``self._lock`` (a ``threading.RLock``) so the read-modify-write in
    ``try_consume_for_segment`` is atomic and ``register_child_segments``
    can call ``root_id_for_segment`` without deadlock. Reentrancy matters
    because ``register_child_segments`` calls ``root_id_for_segment``
    while already holding the lock.

    The lock only protects the in-memory ledger. Callers must NOT hold
    this lock while issuing rewriter / TTS provider / ffmpeg work — those
    are slow IO and the alignment-level paid_fallback semaphore is the
    correct place to serialize external calls.
    """

    def __init__(
        self,
        max_extra_tts_per_root: int = DEFAULT_MAX_POST_TTS_ADJUSTMENTS_PER_SEGMENT,
    ) -> None:
        self.max_extra_tts_per_root = int(max_extra_tts_per_root)
        self._usage_by_root: dict[int, int] = {}
        self._segment_roots: dict[int, int] = {}
        self._lock = threading.RLock()

    def root_id_for_segment(self, segment: DubbingSegment) -> int:
        segment_id = int(segment.segment_id)
        with self._lock:
            return self._segment_roots.get(segment_id, segment_id)

    def register_child_segments(
        self,
        *,
        parent_segment: DubbingSegment,
        child_segments: list[DubbingSegment],
    ) -> int:
        with self._lock:
            root_id = self.root_id_for_segment(parent_segment)
            for child_segment in child_segments:
                self._segment_roots[int(child_segment.segment_id)] = root_id
            return root_id

    def remaining_for_segment(self, segment: DubbingSegment) -> int:
        with self._lock:
            root_id = self.root_id_for_segment(segment)
            used = self._usage_by_root.get(root_id, 0)
            return max(0, self.max_extra_tts_per_root - used)

    def try_consume_for_segment(self, segment: DubbingSegment, amount: int = 1) -> bool:
        with self._lock:
            root_id = self.root_id_for_segment(segment)
            used = self._usage_by_root.get(root_id, 0)
            normalized_amount = max(0, int(amount))
            if used + normalized_amount > self.max_extra_tts_per_root:
                return False
            self._usage_by_root[root_id] = used + normalized_amount
            return True

    def usage_summary(self) -> dict:
        """Public snapshot of consumption state for smart-mode aggregation.

        PR#3C-P3-d: returned dict has the shape::

            {
                "consumed_roots": {root_id: consumed_count, ...},
                "total_consumed": int,         # sum across all roots
                "cap": int,                    # max_extra_tts_per_root
                "exhausted_root_ids": [...],   # roots where consumed >= cap
            }

        Lock-protected copy of internal state — callers MUST NOT mutate
        the returned dicts; treat as read-only. Used by
        ``pipeline.process._aggregate_smart_retry_stats`` to build the
        smart quality_report ``retry_summary`` section, and by
        ``pipeline.process._emit_smart_budget_exhausted_events`` to
        emit one sidecar event per exhausted root.
        """
        with self._lock:
            consumed_roots = dict(self._usage_by_root)
            cap = int(self.max_extra_tts_per_root)
            total_consumed = sum(consumed_roots.values())
            exhausted_root_ids = [
                root_id for root_id, used in consumed_roots.items()
                if used >= cap
            ]
        return {
            "consumed_roots": consumed_roots,
            "total_consumed": total_consumed,
            "cap": cap,
            "exhausted_root_ids": exhausted_root_ids,
        }


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
        # 2026-05-08 P2-17a-0: dsp fit result removed from instance state.
        # Each _align_one call now keeps its own local FitResult and passes
        # it to _apply_dsp_fit_audit / _last_dsp_fit_was_capped_underflow
        # explicitly. Required for thread-safe parallel alignment in 17a-1.

    def align_all(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
    ) -> list[AlignedSegment]:
        """Align every segment to its target slot.

        Dispatch:
        - ``AVT_ALIGN_MAX_WORKERS=1`` (or invalid/0/negative): runs
          ``_align_all_serial`` — the verbatim pre-17a-1 body, preserved
          byte-for-byte so ops dashboards / log scrapers / bisect runs
          keep their existing signal. This is the rollback path.
        - ``AVT_ALIGN_MAX_WORKERS>=2``: pre-classifies segments into
          cheap (keep_original / cache hit) and expensive (cache miss);
          cheap stay synchronous (their work is dict ops + a single
          ffprobe), expensive go through a thread pool. Output paths
          are validated for uniqueness before the pool starts so a
          duplicate ``segment_id`` fails fast instead of producing
          nondeterministic last-write-wins overwrites.

        Order: returned list always matches input position regardless of
        worker completion order. Downstream callers in process.py
        (2807, 6064, 6224) rely on this — see audit
        docs/audits/2026-05-08-tts-rewriter-translator-state-audit.md
        for in-place mutation contract, and
        docs/plans/2026-05-08-p2-17-pipeline-parallelization-plan.md
        §3.1 for the grep evidence.

        Paid fallback: rewriter calls + TTS ``_generate_one`` +
        best-candidate ``_dsp_stretch`` are gated by a semaphore (default
        concurrency 1). This is a CORRECTNESS constraint per the audit
        doc — see ``_attempt_rewrite_loop`` for the comment block.
        """

        workers = _read_align_max_workers()
        if workers <= 1:
            # Hard rollback / debug path. _align_all_serial MUST stay a
            # verbatim copy of the pre-17a-1 align_all body; any
            # divergence breaks ops dashboards keyed on the three [S5]
            # log strings and bisect of the parallel rollout.
            return self._align_all_serial(segments, output_dir)

        return self._align_all_parallel(segments, output_dir, workers=workers)

    def _align_all_serial(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
    ) -> list[AlignedSegment]:
        """Pre-17a-1 ``align_all`` body, copied verbatim.

        DO NOT EDIT this function to share helpers with the parallel
        path. Its purpose is to be the byte-equivalent rollback target
        for ``AVT_ALIGN_MAX_WORKERS=1`` — same ``[S5] 对齐进度: i/N 段``
        cadence (every 15 + final), same ``[S5] 跳过已完成的对齐段 i/N``
        and ``[S5] 对齐缓存已过期，重新处理段 i/N`` strings. Ops dashboards
        and bisect runs depend on this exact format.
        """
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
            if is_valid_output(str(output_path)) and self._aligned_cache_is_fresh(
                segment,
                output_path,
            ):
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
                    en_text=segment.source_text,
                    aligned_audio_path=str(output_path),
                    actual_duration_ms=duration_ms,
                    alignment_method="checkpoint",
                    needs_review=False,
                    dubbing_mode=normalize_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)),
                ))
                if total_segments > 0 and (index % 15 == 0 or index == total_segments):
                    print(f"[S5] 对齐进度: {index}/{total_segments} 段")
                continue
            if is_valid_output(str(output_path)):
                print(
                    f"[S5] 对齐缓存已过期，重新处理段 {index}/{total_segments}",
                    flush=True,
                )
            results.append(self._align_one(segment, str(output_root)))
            if total_segments > 0 and (index % 15 == 0 or index == total_segments):
                print(f"[S5] 对齐进度: {index}/{total_segments} 段")
        return results

    def _align_all_parallel(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
        *,
        workers: int,
    ) -> list[AlignedSegment]:
        """Parallel path — used when ``AVT_ALIGN_MAX_WORKERS >= 2``.

        Three phases:
        1. Pre-classify: cheap branches (keep_original / cache hit) run
           inline so the thread pool only does work that actually moves
           the needle.
        2. Validate: each segment that needs alignment must map to a
           unique output path — fail fast on duplicate ``segment_id``
           rather than letting concurrent writes race.
        3. Submit + collect: expensive segments enter the pool; results
           are re-assembled in input order. First-error sets stop_event
           and cancels pending futures (in-flight ffmpeg / provider
           calls cannot truly be cancelled; the gate is best-effort to
           skip *new* paid work).
        """
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        total_segments = len(segments)
        # Phase 1: pre-classify into cheap vs expensive, AND collect every
        # non-keep-original output path for uniqueness validation. Cache
        # hits also point ``segment.aligned_audio_path`` at
        # ``segment_{id}_aligned.wav``; if a duplicate segment_id has one
        # cache-hit branch and one cache-miss branch, the cache-miss
        # worker would overwrite the file out from under the cache-hit
        # AlignedSegment. So the guard must consider both branches, not
        # just ``needs_align``.
        cheap_results: dict[int, AlignedSegment] = {}
        needs_align: list[tuple[int, DubbingSegment, Path]] = []
        non_keep_original_paths: list[tuple[int, str]] = []
        for idx, segment in enumerate(segments):
            if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
                # keep_original uses segment.tts_audio_path / aligned_audio_path,
                # NOT segment_{id}_aligned.wav, so its paths are disjoint by
                # design and don't participate in this guard.
                cheap_results[idx] = self._keep_original_result(segment)
                continue
            output_path = output_root / f"segment_{segment.segment_id:03d}_aligned.wav"
            non_keep_original_paths.append((idx, str(output_path)))
            if is_valid_output(str(output_path)) and self._aligned_cache_is_fresh(
                segment,
                output_path,
            ):
                cheap_results[idx] = self._build_cached_aligned_result(segment, output_path)
                continue
            if is_valid_output(str(output_path)):
                # Surface stale-cache log inline (same string + flush as serial path).
                print(
                    f"[S5] 对齐缓存已过期，重新处理段 {idx + 1}/{total_segments}",
                    flush=True,
                )
            needs_align.append((idx, segment, output_path))

        # Phase 2: output-path uniqueness check across ALL non-keep-original
        # segments (cache-hit + cache-miss both count). Duplicate segment_id
        # = last-write-wins under parallel; fail fast BEFORE the pool starts.
        unique_paths: set[str] = set()
        for _idx, key in non_keep_original_paths:
            if key in unique_paths:
                raise AlignmentError(
                    f"duplicate alignment output path '{key}' "
                    "(check that segment_id values are unique before align_all)"
                )
            unique_paths.add(key)

        # Phase 3: submit + collect.
        parallel_results: dict[int, AlignedSegment] = {}
        completed_count = len(cheap_results)
        progress_lock = threading.Lock()
        # Emit a single "progress 1" line for every cheap segment that
        # would have produced one in the serial path. Keep cadence on
        # the "every 15 / final" rule so dashboards see comparable noise.
        # We don't replay the serial-path "跳过已完成 i/N" line under
        # parallel because the i is no longer meaningful when cheap
        # segments are scattered between expensive ones in input order.
        if cheap_results:
            with progress_lock:
                if total_segments > 0 and completed_count == total_segments:
                    print(f"[S5] 对齐进度: {completed_count}/{total_segments} 段")

        if needs_align:
            effective_workers = max(1, min(workers, len(needs_align)))
            paid_fallback_max_concurrency = _read_align_paid_fallback_max_concurrency()
            paid_fallback_semaphore = threading.Semaphore(paid_fallback_max_concurrency)
            stop_event = threading.Event()

            with ThreadPoolExecutor(
                max_workers=effective_workers,
                thread_name_prefix="align",
            ) as pool:
                future_to_idx = {
                    pool.submit(
                        self._align_one,
                        seg,
                        str(output_root),
                        paid_fallback_semaphore=paid_fallback_semaphore,
                        stop_event=stop_event,
                    ): idx
                    for idx, seg, _path in needs_align
                }
                try:
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        parallel_results[idx] = future.result()  # re-raises first failure
                        with progress_lock:
                            completed_count += 1
                            if total_segments > 0 and (
                                completed_count % 15 == 0
                                or completed_count == total_segments
                            ):
                                print(f"[S5] 对齐进度: {completed_count}/{total_segments} 段")
                except BaseException:
                    # First failure: signal new paid work to skip and ask
                    # pending futures to cancel. Already-running ffmpeg /
                    # provider calls cannot be cancelled — the small
                    # default workers + paid_fallback=1 keep that
                    # exposure bounded.
                    stop_event.set()
                    for pending in future_to_idx:
                        pending.cancel()
                    raise

        # Re-assemble in input order regardless of completion order.
        return [
            cheap_results.get(idx) or parallel_results[idx]
            for idx in range(total_segments)
        ]

    def _build_cached_aligned_result(
        self,
        segment: DubbingSegment,
        output_path: Path,
    ) -> AlignedSegment:
        """Cache-hit branch shared by _align_all_serial and _align_all_parallel.

        Mirrors the in-place mutation + AlignedSegment construction the
        serial path does inline. _align_all_serial keeps its inline copy
        for log-fidelity; this helper is for the parallel path only so
        cheap branches run sync there too.
        """
        duration_ms = _ffprobe_duration_ms(output_path)
        target_duration_ms = int(segment.target_duration_ms)
        segment.aligned_audio_path = str(output_path)
        segment.actual_duration_ms = duration_ms
        segment.alignment_ratio = (
            duration_ms / target_duration_ms if target_duration_ms > 0 else 0.0
        )
        return AlignedSegment(
            segment_id=segment.segment_id,
            speaker_id=segment.speaker_id,
            display_name=segment.display_name,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            cn_text=segment.cn_text,
            en_text=segment.source_text,
            aligned_audio_path=str(output_path),
            actual_duration_ms=duration_ms,
            alignment_method="checkpoint",
            needs_review=False,
            dubbing_mode=normalize_dubbing_mode(
                getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
            ),
        )

    @staticmethod
    def _aligned_cache_is_fresh(segment: DubbingSegment, output_path: Path) -> bool:
        """Return whether an aligned wav can be reused for the segment.

        Text rewrites and TTS regeneration overwrite the raw TTS wav before
        alignment.  If that input is newer than the aligned wav, the path-based
        checkpoint is stale even though the aligned file is non-empty.
        """
        raw_path_value = (getattr(segment, "tts_audio_path", None) or "").strip()
        if not raw_path_value:
            return True
        raw_path = Path(raw_path_value).resolve(strict=False)
        if not raw_path.exists():
            return True
        try:
            return raw_path.stat().st_mtime <= output_path.stat().st_mtime
        except OSError:
            return False

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
            en_text=segment.source_text,
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
        *,
        paid_fallback_semaphore: threading.Semaphore | None = None,
        stop_event: threading.Event | None = None,
    ) -> AlignedSegment:
        # paid_fallback_semaphore + stop_event are non-None only when called
        # from _align_all_parallel. Both are forwarded into
        # _attempt_rewrite_loop where the actual paid-LLM / paid-TTS work
        # happens; the rest of _align_one is DSP / direct copy and does
        # not need gating.
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
        # Per-call local fit result. Threaded through every branch that may
        # call _dsp_stretch so audit/capped-underflow detection sees only
        # this segment's stretch, never a neighbor's.
        fit_result: FitResult | None = None
        self._clear_dsp_fit_audit(segment)

        # Phase 2 force-DSP override — when admin enables `force_dsp_alignment`,
        # bypass the rewrite/direct/dsp decision entirely and always stretch
        # the raw TTS audio to the target duration. Trades quality for hard
        # time alignment. Useful when LLM length control is unreliable AND
        # the user prefers slight DSP artefact over rewrite churn.
        force_dsp_user = _is_force_dsp_alignment_enabled()
        if force_dsp_user:
            input_path = _resolve_existing_audio_path(segment.tts_audio_path)
            aligned_audio_path, fit_result = self._dsp_stretch(
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
            aligned_audio_path, fit_result = self._dsp_stretch(
                str(input_path), target_duration_ms, str(output_path)
            )
            alignment_method = "dsp"
            needs_review = False
        else:
            rewrite_outcome = self._attempt_rewrite_loop(
                segment=segment,
                output_path=str(output_path),
                current_actual_duration_ms=current_actual_duration_ms,
                paid_fallback_semaphore=paid_fallback_semaphore,
                stop_event=stop_event,
            )
            if rewrite_outcome is not None:
                (
                    aligned_audio_path,
                    aligned_duration_ms,
                    alignment_method,
                    needs_review,
                    fit_result,
                ) = rewrite_outcome
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
                    aligned_audio_path, fit_result = self._dsp_stretch(
                        str(input_path),
                        listenable_target_ms,
                        str(output_path),
                        policy=SHORT_LISTENABLE_DSP_POLICY,
                    )
                    alignment_method = "capped_dsp_overflow"
                else:
                    aligned_audio_path, fit_result = self._dsp_stretch(
                        str(input_path),
                        target_duration_ms,
                        str(output_path),
                    )
                    alignment_method = "force_dsp"
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                needs_review = True

        if aligned_duration_ms is None:
            aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))

        self._apply_dsp_fit_audit(segment, fit_result)
        if (
            alignment_method in {"force_dsp", "force_dsp_user"}
            and self._last_dsp_fit_was_capped_underflow(fit_result)
        ):
            alignment_method = "capped_dsp_underflow"

        if alignment_method in _FORCE_DSP_REVIEW_METHODS:
            severity, suppress_review, review_reason = self._classify_force_dsp_review(
                segment=segment,
                actual_duration_ms=current_actual_duration_ms,
                target_duration_ms=target_duration_ms,
                alignment_method=alignment_method,
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
            en_text=segment.source_text,
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
        paid_fallback_semaphore: threading.Semaphore | None = None,
        stop_event: threading.Event | None = None,
    ) -> tuple[str, int, str, bool, FitResult | None] | None:
        target_duration_ms = int(segment.target_duration_ms)
        if not self._should_attempt_rewrite(current_actual_duration_ms, target_duration_ms):
            return None
        if self.rewriter is None or self.tts_generator is None:
            return None

        # P2-17a-1 paid fallback gate. The semaphore default concurrency
        # (AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1) is a CORRECTNESS
        # constraint, not cost-conservatism — see the audit:
        # docs/audits/2026-05-08-tts-rewriter-translator-state-audit.md
        # The four mutable shared fields require ③ paid_fallback=1
        # serial because:
        #   - GeminiTranslator._metering_usage_context (rewriter
        #     try/finally setattr; concurrent rewrite races corrupt
        #     usage phase attribution).
        #   - TTSGenerator._speaker_voice_cache (per-speaker auto-match
        #     cache; concurrent _generate_one writes can duplicate match
        #     work and put the cache in an inconsistent state).
        #   - TTSGenerator._active_job_record / _job_provider (set in
        #     generate_all but read by direct _generate_one path;
        #     concurrent calls may read stale state from prior calls).
        # Raising max concurrency above 1 requires upgrading each of
        # those to ① local-return semantics or ② an explicit lock; do
        # NOT just bump the env.
        #
        # Stop-event check happens BOTH before and after acquire so
        # that:
        #   - We skip new paid work if a sibling already failed.
        #   - We don't sit on the semaphore if cancellation arrived
        #     while we were waiting in queue.
        if stop_event is not None and stop_event.is_set():
            return None
        semaphore_cm: contextlib.AbstractContextManager
        if paid_fallback_semaphore is not None:
            semaphore_cm = paid_fallback_semaphore
        else:
            semaphore_cm = contextlib.nullcontext()
        with semaphore_cm:
            if stop_event is not None and stop_event.is_set():
                return None
            return self._attempt_rewrite_loop_unguarded(
                segment=segment,
                output_path=output_path,
                current_actual_duration_ms=current_actual_duration_ms,
            )

    def _attempt_rewrite_loop_unguarded(
        self,
        *,
        segment: DubbingSegment,
        output_path: str,
        current_actual_duration_ms: int,
    ) -> tuple[str, int, str, bool, FitResult | None] | None:
        """Original rewrite loop body, extracted so the semaphore wrap
        in _attempt_rewrite_loop stays a single ``with`` block. Tests
        that mock ``_dsp_stretch`` / ``rewriter`` / ``tts_generator``
        keep working unchanged because the body is identical.
        """
        target_duration_ms = int(segment.target_duration_ms)
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
                return aligned_audio_path, aligned_duration_ms, "rewrite_direct", False, None
            if decision == "dsp":
                aligned_audio_path, fit_result = self._dsp_stretch(
                    tts_result.audio_path, target_duration_ms, output_path
                )
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_dsp", False, fit_result
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
                return aligned_audio_path, aligned_duration_ms, "rewrite_direct", False, None
            if best_decision == "dsp":
                aligned_audio_path, fit_result = self._dsp_stretch(
                    best_tts_audio_path, target_duration_ms, output_path
                )
                aligned_duration_ms = _measure_wav_duration_ms(Path(aligned_audio_path))
                return aligned_audio_path, aligned_duration_ms, "rewrite_dsp", False, fit_result

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
        alignment_method: str = "",
    ) -> tuple[str, bool, str]:
        """Classify final force-DSP risk without another LLM rewrite.

        Only very short, low-information backchannels are auto-denoised from
        manual review. 2-5s force-DSP is still surfaced, but marked medium so
        downstream reports can separate it from genuinely long/high-risk drift.

        ``capped_dsp_underflow`` always classifies as high severity regardless
        of target/chars: that method only fires when the source TTS was too
        short for the slot AND the slowdown hit the 0.67x floor with ≥250ms of
        silence padding (see ``_last_dsp_fit_was_capped_underflow``). The cap
        firing is itself a strong audit signal — a short slot with little text
        can still produce 70%-silence output that needs human attention.
        """
        if target_duration_ms <= 0 or actual_duration_ms <= 0:
            return "high", False, "invalid_duration"

        if alignment_method == "capped_dsp_underflow":
            return "high", False, "capped_underflow"

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

    def _apply_dsp_fit_audit(
        self,
        segment: DubbingSegment,
        fit_result: FitResult | None,
    ) -> None:
        if fit_result is None:
            return
        segment.dsp_speed_ratio_used = float(fit_result.speed_ratio_used)
        segment.dsp_silence_padded_ms = int(fit_result.silence_padded_ms)
        segment.dsp_truncated_ms = int(fit_result.truncated_ms)
        segment.dsp_initial_duration_ms = int(fit_result.initial_duration_ms)
        segment.dsp_trimmed_duration_ms = int(fit_result.trimmed_duration_ms)
        segment.dsp_stretched_duration_ms = int(fit_result.stretched_duration_ms)

    def _last_dsp_fit_was_capped_underflow(
        self,
        fit_result: FitResult | None,
    ) -> bool:
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
    ) -> tuple[str, FitResult | None]:
        """Stretch ``input_path`` to ``target_duration_ms`` and return both
        the output path and the ``FitResult`` from ``fit_audio_to_slot``.

        Returning the ``FitResult`` per-call (rather than caching it on
        ``self``) keeps DSP audit metadata local to a single _align_one
        invocation; that isolation is what makes parallel alignment safe
        in 17a-1. ``None`` is returned for ``fit_result`` when the helper
        falls through to the legacy ffmpeg path (no FitResult available
        from that branch)."""

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
        if fit_result is not None and output_audio_path.exists():
            return str(output_audio_path), fit_result

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
        # Legacy ffmpeg fallback path: no FitResult is produced by raw atempo,
        # so callers that rely on dsp_* audit fields will see them stay zeroed.
        return str(output_audio_path), None

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

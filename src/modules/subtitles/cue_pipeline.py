"""High-level pipeline: SemanticBlock list → SubtitleCue list + ValidationReport.

This module is the bridge between core domain models (SemanticBlock / SubtitleLine)
and the subtitle-cue-generation-v2 abstractions (T1-T7).  It intentionally imports
from core.models — that coupling is acceptable here because cue_pipeline is the
designated integration layer (T9 / OutputDispatcher).

2026-05-04 Phase C addition: optional whisper-aligned cue boundaries.
When ``AVT_WHISPER_ALIGN_ENABLED=1`` and a block is in-sync (cn_text
matches tts_input_cn_text via Phase B normalize), the pipeline runs
faster-whisper on the block's aligned audio and uses the resulting
char-level timestamps to drive cue boundaries (vs the legacy
proportional layout). ANY failure path — flag off, drift, missing
audio, whisper subprocess error, DTW disjoint, build_cues_with_char_times
returns [] — falls back to ``build_cues_for_block`` so publish never
breaks because of whisper trouble. CodeX guardrails locked in.

2026-05-05 Phase D additions:
  - D-1: admin policy double-gate. ``admin_settings.json::
    whisper_alignment_enabled`` must also be true; either the env or
    the admin field can disable.
  - D-5: ``context``-aware trigger field. Default trigger
    ``"deliverable"`` skips publish (this module's default call site)
    and runs only at deliverable handlers (D-2 ensure helper). Admin
    can flip to ``"publish"`` (every task) or ``"manual"``
    (admin-button-only). Callers that know they are at a deliverable
    handoff pass ``context="deliverable"``.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §6, §10 Phase 1a
      docs/plans/2026-05-04-subtitle-audio-sync-plan.md Phase C Task C3 + Phase D
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from core.models import SemanticBlock, SubtitleLine
from modules.subtitles.cue_builder import (
    build_cues_for_block,
    build_cues_with_char_times,
)
from modules.subtitles.cue_models import SubtitleCue, normalize
from modules.subtitles.cue_validator import BlockSpec, ValidationReport, validate_cues

# 2026-05-04 P0c CodeX P1 follow-up: import the whisper-align integration
# at module top via aliases so tests have stable monkeypatch targets that
# survive `services.whisper_align` module re-import isolation tests.
# Previously these were lazy-imported inside _try_whisper_aligned_cues; the
# lazy import bound to the post-reimport (un-patched) module instance, so
# C3 tests that patched services.whisper_align.run_whisper_subprocess
# would silently spawn a real subprocess after the import-isolation test
# ran first and left a stale module reference behind.
#
# services.whisper_align itself does NOT import faster_whisper at module
# load — the dependency loads lazily inside the runner subprocess via
# `from faster_whisper import WhisperModel` in runner.main(). So
# importing the wrapper at cue_pipeline module top is safe even in
# environments without faster-whisper installed.
from services.whisper_align import run_whisper_subprocess_cached as _run_whisper_cached
from services.whisper_align.dtw import align_chars_to_words as _align_chars_to_words

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Whisper alignment feature flag + trigger gate + drift gate
# ---------------------------------------------------------------------------

# Recognised values of the ``context`` argument — each call site identifies
# itself so the trigger field's policy can be applied.
#
# - ``"publish"``     publish stage (every task; this is the default)
# - ``"deliverable"`` Jianying / materials_pack pre-pack (D-3 / D-4)
# - ``"manual"``      admin clicked "run whisper now" (D-6 endpoint)
_VALID_CONTEXTS = frozenset({"publish", "deliverable", "manual"})


def _trigger_permits(trigger: str, context: str) -> bool:
    """Decision matrix for whether ``trigger`` allows running at ``context``.

    Defines the user-preferred semantics introduced in D-5:

    | trigger \\ context | publish | deliverable | manual |
    |--------------------|---------|-------------|--------|
    | ``publish``        | ✓       | ✓           | ✓      |
    | ``deliverable``    | ✗       | ✓           | ✓      |
    | ``manual``         | ✗       | ✗           | ✓      |

    Rationale:
      - ``publish`` is "run for every task" — superset, allows everywhere.
      - ``deliverable`` is "only when user wants subtitles in the
        deliverable" (Jianying / materials_pack). Skips publish.
      - ``manual`` is "no auto-trigger anywhere" — admin-only.
      - A ``"manual"`` context (admin endpoint) bypasses the trigger
        check entirely; admin invocation is by definition intentional.

    Unknown context falls through to the strictest interpretation
    (publish) so unfamiliar callers don't accidentally widen access.
    """
    if context == "manual":
        return True
    if context == "deliverable":
        return trigger in ("publish", "deliverable")
    # context == "publish" or any unrecognised value → strictest
    return trigger == "publish"


def _resolve_whisper_settings(*, context: str = "publish"):
    """Single-pass evaluation of the three gates: env capability,
    admin ``enabled`` policy, and trigger-vs-context match. Returns the
    parsed ``WhisperAlignmentSettings`` if all three open, else ``None``.

    Folding the three gates into one function lets callers grab
    ``settings.model`` / ``settings.skip_cache`` for the subprocess
    call without re-reading admin_settings.json. Read fresh per call
    (no caching) so admin edits propagate without restart.
    """
    if os.environ.get("AVT_WHISPER_ALIGN_ENABLED", "") != "1":
        return None
    # Lazy import — keeps cue_pipeline's import time light, and avoids
    # a circular dep risk (admin_settings is in services/, cue_pipeline
    # is in modules/, services may eventually import from modules).
    from services.admin_settings import read_whisper_alignment_settings
    settings = read_whisper_alignment_settings()
    if not settings.enabled:
        return None
    if not _trigger_permits(settings.trigger, context):
        return None
    return settings


def _whisper_align_enabled(*, context: str = "publish") -> bool:
    """Three-gate: env capability AND admin policy AND trigger-vs-context.
    BOTH the env flag AND admin ``enabled`` AND the trigger field for
    this call site must permit.

    Phase D-1 (2026-05-05) introduced env+admin double-gate.
    Phase D-5 (2026-05-05) added the trigger-vs-context third gate.

    - ``AVT_WHISPER_ALIGN_ENABLED=1`` — ops capability switch. Set by
      docker-compose / .env. "Does this server know how to run whisper?
      (faster-whisper installed, model cached, OK to spawn subprocess?)"
      Ops can flip this OFF in an emergency to disable across all
      tenants instantly without touching admin UI or DB.

    - ``admin_settings.json::whisper_alignment_enabled`` — admin policy.
      "Should we run whisper for this deployment?" Admin can toggle in
      the backend UI without touching docker-compose.

    - ``admin_settings.json::whisper_alignment_trigger`` vs ``context`` —
      where in the pipeline this call sits. Default trigger
      ``"deliverable"`` only runs whisper at deliverable handlers
      (Jianying / materials_pack), saving ~5-15s per task at publish
      time. ``"publish"`` runs everywhere; ``"manual"`` is admin-only.

    Read fresh per call (no caching). Env evaluates first because it's
    cheaper (no file read).
    """
    return _resolve_whisper_settings(context=context) is not None


def _block_is_in_sync(block: SemanticBlock) -> bool:
    """Drift gate consistent with Phase B validator (CodeX guardrail #4).

    Empty ``tts_input_cn_text`` → treated as in-sync (legacy / pre-rollout
    block, Phase A's load-time backfill should have populated it).
    Otherwise: in-sync iff ``normalize(merged) == normalize(tts_input)`` —
    same comparison used in ``cue_validator.validate_cues`` so the C3
    decision matches what subtitle_quality_report.json says.
    """
    tts_input = (block.tts_input_cn_text or "").strip()
    if not tts_input:
        return True
    return normalize(tts_input) == normalize(block.merged_cn_text)


def _try_whisper_aligned_cues(
    block: SemanticBlock,
    *,
    en_text: str,
    block_start_ms: int,
    block_end_ms: int,
    min_display_ms: int,
    context: str = "publish",
) -> list[SubtitleCue] | None:
    """Try to build whisper-aligned cues for a block.

    Returns ``None`` if any precondition or step fails — caller falls
    back to the proportional path. Order of fallback gates:

    1. Feature flag off → None (no whisper invocation)
    2. Drift block → None
    3. aligned_audio_path is None / file missing → None
    4. Whisper subprocess raises (RuntimeError, TimeoutExpired, JSONError) → None
    5. DTW returns [] (disjoint / empty) → None
    6. build_cues_with_char_times returns [] (anomaly) → None

    ``context`` (D-5) tells the trigger gate where this call sits:
    "publish" (publish stage, default), "deliverable" (D-3/D-4 ensure
    helper), or "manual" (admin endpoint). The admin
    ``whisper_alignment_trigger`` field decides whether each context is
    permitted (see ``_trigger_permits``).

    Successful path returns a non-empty list of cues with
    ``source = "semantic_block_v2_whisper_aligned"``.
    """
    settings = _resolve_whisper_settings(context=context)
    if settings is None:
        return None

    if not _block_is_in_sync(block):
        return None

    audio_path = block.aligned_audio_path
    if not audio_path:
        return None
    audio_pathobj = Path(audio_path)
    if not audio_pathobj.is_file():
        logger.debug(
            "whisper-align: block %s aligned_audio_path %s not on disk; "
            "falling back", block.block_id, audio_path,
        )
        return None

    # Subprocess invocation (or cache hit) — any exception is fallback territory.
    # _run_whisper_cached is a module-level alias for
    # services.whisper_align.run_whisper_subprocess_cached. Tests
    # monkeypatch this name directly (cue_pipeline._run_whisper_cached)
    # so the patch survives even after the whisper_align module gets
    # re-imported by the import-isolation test.
    #
    # D-5: model + skip_cache come from admin settings. ``skip_cache=True``
    # forces a fresh subprocess run even on a cache hit (admin override).
    try:
        words = _run_whisper_cached(
            audio_path,
            language="zh",
            model=settings.model,
            skip_cache=settings.skip_cache,
        )
    except Exception as exc:  # noqa: BLE001 — whisper subprocess errors are
                              # diverse (RuntimeError, TimeoutExpired,
                              # JSONDecodeError, ImportError from runner...)
        logger.warning(
            "whisper-align: subprocess failed for block %s (%s); falling back",
            block.block_id, exc,
        )
        return None

    if not words:
        return None

    # CodeX P1 (2026-05-04): DTW + helper invocation must ALSO be inside
    # a fallback try. Even though both should be exception-free by
    # contract (helper returns [] on anomaly, DTW returns []), an
    # unforeseen bug must not crash publish. CodeX guardrail #5: any
    # whisper-path exception → fall back to proportional cues, never
    # propagate to build_subtitle_cues_for_blocks.
    try:
        char_times = _align_chars_to_words(block.merged_cn_text, words)
        if not char_times:
            return None

        cues = build_cues_with_char_times(
            block_id=block.block_id,
            speaker_id=block.speaker_id,
            speaker_name=block.speaker_name,
            cn_text=block.merged_cn_text,
            en_text=en_text,
            block_start_ms=block_start_ms,
            block_end_ms=block_end_ms,
            char_times=char_times,
            min_display_ms=min_display_ms,
        )
    except Exception as exc:  # noqa: BLE001 — see above; whisper-path safety
        logger.warning(
            "whisper-align: post-subprocess pipeline raised for block %s "
            "(%s); falling back", block.block_id, exc,
        )
        return None

    if not cues:
        return None
    return cues


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SubtitleCuePipelineResult:
    """Result of build_subtitle_cues_for_blocks."""

    cues: list[SubtitleCue]
    report: ValidationReport
    block_specs: list[BlockSpec]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_effective_duration(block: SemanticBlock) -> int:
    """Resolve the timeline-occupancy duration for a block.

    Used by SubtitleCueBuilder to compute block_end_ms = first_start_ms +
    effective_duration_ms. This must match how publish_backend lays
    segments on the dubbed audio timeline, otherwise subtitle cues drift
    out of sync with audio (or cause SegmentOverlap when two adjacent
    blocks' cue windows overlap).

    Priority (corrected 2026-05-03 after C2 hot-fix):
      1. block.last_end_ms - block.first_start_ms (>0)
         The original SRT segment's time window. This is what
         publish_backend uses as the segment's timeline slot — audio
         either fits with silence padding or is DSP-stretched to fill
         exactly this window. Cue timing must match.
      2. block.target_duration_ms (>0)
         Fallback if SRT window is unavailable. Note: target_duration_ms
         is the LLM rewrite TARGET (how long the rewritten Chinese should
         read), NOT timeline occupancy. Only used when SRT window is
         missing.
      3. block.actual_audio_duration_ms (>0)
         Final fallback if neither SRT window nor target are set. This
         is the raw TTS render duration before DSP — least accurate for
         timeline mapping but better than nothing.

    Returns the resolved integer duration (may be <= 0 for degenerate blocks).
    """
    srt_window = int(block.last_end_ms) - int(block.first_start_ms)
    if srt_window > 0:
        return srt_window
    if block.target_duration_ms > 0:
        return int(block.target_duration_ms)
    if block.actual_audio_duration_ms > 0:
        return int(block.actual_audio_duration_ms)
    return 0  # caller handles empty block


def _build_caption_map(subtitle_lines: list[SubtitleLine]) -> dict[int, str]:
    """Build index → en_text lookup from SubtitleLine list."""
    return {line.index: line.en_text for line in subtitle_lines}


def _derive_en_text(block: SemanticBlock, caption_map: dict[int, str]) -> str:
    """Join en_text from all SubtitleLines referenced by block.original_srt_indices.

    Missing indices contribute an empty string (not an error).
    Trailing/leading spaces from each part are stripped before joining.
    """
    parts = []
    for idx in block.original_srt_indices:
        en = caption_map.get(idx, "")
        if en:
            parts.append(en)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Cross-block overlap clamp (2026-05-08)
# ---------------------------------------------------------------------------

# Minimum gap inserted between adjacent cues when we have to clamp the
# later cue's start. 1ms is enough to satisfy pyJianYingDraft's strict
# SegmentOverlap check (which is "any overlap fails" without tolerance);
# larger values would unnecessarily eat into the later cue's duration.
_CUE_OVERLAP_GAP_MS = 1


def _clamp_cross_block_cue_overlaps(
    cues: list[SubtitleCue],
    min_display_ms: int,
) -> list[SubtitleCue]:
    """Walk cues in start_ms order; clamp any later cue that starts
    before its predecessor ends.

    Returns a NEW list with adjusted cues (mutating the originals
    would change shared block_specs / report-bound state in surprising
    ways). The pipeline result still preserves cue identity (cue_id /
    block_id / text / source) — only ``start_ms`` and possibly
    ``end_ms`` are nudged.

    Why we touch ``end_ms`` too: if clamping ``start_ms`` would crush
    the cue's display duration below ``min_display_ms``, we extend
    ``end_ms`` to keep the cue readable. Yes this can cascade — the
    extended cue may now overlap with the cue AFTER it. The loop is
    sequential precisely so the next iteration sees the updated end
    and clamps again. Worst case the whole tail of the timeline
    shifts by O(min_display_ms × overlap_count) milliseconds, which
    in practice is dominated by a few real-speech interruptions
    (typically 1-3 across a whole task).

    Empty / single-element input: returned as-is (nothing to clamp).
    """
    if len(cues) < 2:
        return list(cues)

    # Sort a SHALLOW copy by start_ms; ties broken by end_ms (longer
    # cue first so it owns the boundary). dataclass(slots=True)
    # instances are hashable-by-id; we rebuild by attribute below.
    sorted_cues = sorted(cues, key=lambda c: (c.start_ms, -c.end_ms))

    out: list[SubtitleCue] = []
    prev_end: int | None = None

    for c in sorted_cues:
        new_start = c.start_ms
        new_end = c.end_ms

        if prev_end is not None and new_start < prev_end:
            new_start = prev_end + _CUE_OVERLAP_GAP_MS
            # If the clamp crushed duration below min_display_ms,
            # push the end out so the cue stays readable. We accept
            # that this MIGHT now overlap the next cue — the next
            # iteration handles that cascade.
            if new_end - new_start < min_display_ms:
                new_end = new_start + min_display_ms
            logger.debug(
                "cue_pipeline: clamped cross-block overlap on cue %s "
                "(block %s): start %d→%d end %d→%d",
                c.cue_id, c.block_id, c.start_ms, new_start, c.end_ms, new_end,
            )

        if new_start != c.start_ms or new_end != c.end_ms:
            # Build an adjusted copy preserving every other field.
            out.append(
                SubtitleCue(
                    cue_id=c.cue_id,
                    block_id=c.block_id,
                    speaker_id=c.speaker_id,
                    speaker_name=c.speaker_name,
                    text=c.text,
                    en_text=c.en_text,
                    start_ms=new_start,
                    end_ms=new_end,
                    source=c.source,
                    needs_review=c.needs_review,
                    review_reason=c.review_reason,
                )
            )
        else:
            out.append(c)

        prev_end = new_end

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_subtitle_cues_for_blocks(
    blocks: list[SemanticBlock],
    subtitle_lines: list[SubtitleLine],
    *,
    min_display_ms: int = 500,
    context: str = "publish",
) -> SubtitleCuePipelineResult:
    """High-level pipeline: SemanticBlock list → SubtitleCue list + ValidationReport.

    Steps:
    1. Build a lookup of SubtitleLine by index for en_text derivation.
    2. For each block:
       a. Skip if merged_cn_text is empty/whitespace (no cues, no BlockSpec).
       b. Resolve effective audio duration — skip if <= 0 (degenerate).
       c. Derive en_text by joining SubtitleLine.en_text for each srt_index.
       d. Compute block_start_ms = first_start_ms, block_end_ms = first_start_ms + effective.
       e. Call build_cues_for_block → appended to running cues list.
       f. Build BlockSpec for the validator.
    3. Run validate_cues(block_specs, all_cues, min_display_ms).
    4. Return SubtitleCuePipelineResult(cues, report, block_specs).

    Blocks with empty merged_cn_text are excluded from block_specs, so the
    validator produces no text_mismatch for them.

    Blocks where effective duration resolves to <= 0 are logged at WARNING
    and silently skipped (no cues, no BlockSpec).

    No LLM, TTS, or external service calls are made here.

    Args:
        blocks:         List of SemanticBlock instances (typically aligned_blocks).
        subtitle_lines: List of SubtitleLine instances for en_text lookup.
        min_display_ms: Minimum display duration passed to validator (default 500).
        context:        Where this call sits in the pipeline. Drives the
            D-5 trigger-vs-context gate for whisper alignment. One of
            ``"publish"`` (publish stage, default), ``"deliverable"``
            (D-2 ensure helper for Jianying / materials_pack), or
            ``"manual"`` (admin "run whisper now" endpoint).

    Returns:
        SubtitleCuePipelineResult with all cues, the ValidationReport, and
        the BlockSpec list used during validation.
    """
    caption_map = _build_caption_map(subtitle_lines)

    all_cues: list[SubtitleCue] = []
    block_specs: list[BlockSpec] = []

    for block in blocks:
        # Skip empty / whitespace-only merged_cn_text
        if not block.merged_cn_text.strip():
            continue

        # Resolve effective duration
        effective_duration = _resolve_effective_duration(block)
        if effective_duration <= 0:
            logger.warning(
                "cue_pipeline: skipping block %r — effective_duration=%d <= 0",
                block.block_id,
                effective_duration,
            )
            continue

        block_start_ms = int(block.first_start_ms)
        block_end_ms = block_start_ms + effective_duration

        # Derive en_text from SubtitleLines referenced by this block
        en_text = _derive_en_text(block, caption_map)

        # 2026-05-04 Phase C: opportunistic whisper-aligned cues. Returns
        # None for any precondition failure / runtime error, so we always
        # have the proportional fallback below as the safety net. CodeX
        # guardrail #5: publish must never fail because of whisper trouble.
        cues: list[SubtitleCue] | None = _try_whisper_aligned_cues(
            block,
            en_text=en_text,
            block_start_ms=block_start_ms,
            block_end_ms=block_end_ms,
            min_display_ms=min_display_ms,
            context=context,
        )

        if cues is None:
            # Build cues via the proportional path (existing behavior).
            try:
                cues = build_cues_for_block(
                    block_id=block.block_id,
                    speaker_id=block.speaker_id,
                    speaker_name=block.speaker_name,
                    cn_text=block.merged_cn_text,
                    en_text=en_text,
                    block_start_ms=block_start_ms,
                    block_end_ms=block_end_ms,
                    min_display_ms=min_display_ms,
                )
            except ValueError:
                logger.warning(
                    "cue_pipeline: build_cues_for_block raised ValueError for block %r "
                    "(start=%d end=%d); skipping",
                    block.block_id,
                    block_start_ms,
                    block_end_ms,
                )
                continue

        if cues:
            all_cues.extend(cues)

        # Build BlockSpec for validator (even if cues is empty — segmenter may have
        # returned [] for whitespace-only cn_text, but merged_cn_text was non-empty;
        # the validator will correctly flag text_mismatch or empty_cue in that case).
        # 2026-05-04 P0b: pass tts_input_cn_text so the validator can detect
        # text↔audio drift when the user edited cn_text without re-running TTS.
        block_specs.append(
            BlockSpec(
                block_id=block.block_id,
                merged_cn_text=block.merged_cn_text,
                start_ms=block_start_ms,
                end_ms=block_end_ms,
                tts_input_cn_text=block.tts_input_cn_text,
            )
        )

    # 2026-05-08: clamp cross-block cue overlaps. Source segments can
    # legitimately overlap when speakers interrupt each other in real
    # audio (e.g. "对吧?" / "没错" interview banter). Audio is fine
    # because per-speaker tracks render in parallel, but the SUBTITLE
    # track is single-laned — overlapping cues trigger SegmentOverlap
    # in pyJianYingDraft / cause double-renders in any single-track
    # SRT consumer. The cue_validator's existing timing_overlap check
    # only catches WITHIN-block overlap; here we catch the cross-block
    # case the validator misses. Algorithm: sort by start_ms, walk
    # adjacent pairs, clamp later cue's start to earlier cue's end +
    # epsilon. If clamp would crush duration below min_display_ms,
    # also push the later cue's end out so duration is preserved
    # (rather than dropping the cue entirely — losing user-visible
    # text is worse than a small global timing nudge).
    all_cues = _clamp_cross_block_cue_overlaps(all_cues, min_display_ms)

    report = validate_cues(
        block_specs=block_specs,
        cues=all_cues,
        min_display_ms=min_display_ms,
    )

    return SubtitleCuePipelineResult(
        cues=all_cues,
        report=report,
        block_specs=block_specs,
    )

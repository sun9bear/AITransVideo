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

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §6, §10 Phase 1a
      docs/plans/2026-05-04-subtitle-audio-sync-plan.md Phase C Task C3
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
# Whisper alignment feature flag + drift gate
# ---------------------------------------------------------------------------


def _whisper_align_enabled() -> bool:
    """Read the feature flag fresh every call so deploy-time env changes
    take effect without re-importing the module. Default off (CodeX
    guardrail #1) — only ``AVT_WHISPER_ALIGN_ENABLED == "1"`` activates."""
    return os.environ.get("AVT_WHISPER_ALIGN_ENABLED", "") == "1"


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

    Successful path returns a non-empty list of cues with
    ``source = "semantic_block_v2_whisper_aligned"``.
    """
    if not _whisper_align_enabled():
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
    try:
        words = _run_whisper_cached(audio_path, language="zh")
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
# Public API
# ---------------------------------------------------------------------------


def build_subtitle_cues_for_blocks(
    blocks: list[SemanticBlock],
    subtitle_lines: list[SubtitleLine],
    *,
    min_display_ms: int = 500,
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

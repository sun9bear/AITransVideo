"""High-level pipeline: SemanticBlock list → SubtitleCue list + ValidationReport.

This module is the bridge between core domain models (SemanticBlock / SubtitleLine)
and the subtitle-cue-generation-v2 abstractions (T1-T7).  It intentionally imports
from core.models — that coupling is acceptable here because cue_pipeline is the
designated integration layer (T9 / OutputDispatcher).

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §6, §10 Phase 1a
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import SemanticBlock, SubtitleLine
from modules.subtitles.cue_builder import build_cues_for_block
from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.cue_validator import BlockSpec, ValidationReport, validate_cues

logger = logging.getLogger(__name__)


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

        # Build cues (may raise ValueError for truly bad inputs; propagate upward)
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

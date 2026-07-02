"""SubtitleCueValidator (T6) — validates canonical subtitle cues against
BlockSpec constraints and produces a structured ValidationReport.

Contract per plan §8:
    Hard errors  → validation_status "failed"
    Review items → validation_status "needs_review"  (if no errors)
    No issues    → validation_status "passed"

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §8
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from modules.subtitles.cue_models import SubtitleCue, normalize

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_REVIEW_REASONS: frozenset[str] = frozenset(
    {
        "long_unbreakable_text",
        "unknown_mixed_token",
        "text_audio_may_need_review",
    }
)


@dataclass(slots=True, frozen=True)
class BlockSpec:
    """Lightweight block descriptor consumed by the validator.

    Decouples the validator from SemanticBlock — callers resolve block fields
    before passing in, so the validator has no dependency on core.models.

    ``tts_input_cn_text`` (2026-05-04 P0b): the text that was actually fed
    to TTS for this block's audio. Compared against ``merged_cn_text`` to
    detect text↔audio drift after a user edited cn_text without
    regenerating TTS. Empty string is treated as "in-sync" (legacy /
    pre-rollout block) to avoid false positives.
    """

    block_id: str
    merged_cn_text: str
    start_ms: int
    end_ms: int
    tts_input_cn_text: str = ""


@dataclass(slots=True, frozen=True)
class ValidationIssue:
    """A single validation finding — either hard error or review tag."""

    block_id: str
    cue_id: str | None  # None for block-level issues (e.g. text_mismatch)
    code: str  # Issue code string; see plan §8.
    severity: str  # "error" or "review"
    message: str  # Human-readable description.


@dataclass(slots=True, frozen=True)
class BlockSummary:
    """Per-block aggregated stats for the quality report.

    ``text_audio_drift`` (2026-05-04 P0b): True when ``BlockSpec.merged_cn_text``
    differs from ``BlockSpec.tts_input_cn_text`` (post normalize). Emitted
    independently of cue-level issues so the quality report can surface
    drift state directly.
    """

    block_id: str
    cue_count: int
    text_mismatch: bool
    timing_overlap_count: int
    timing_out_of_block_count: int
    empty_cue_count: int
    long_unbreakable_count: int
    unknown_mixed_token_count: int
    short_display_duration_count: int
    text_audio_drift: bool = False


@dataclass(slots=True, frozen=True)
class ValidationReport:
    """Result of validating a set of (block_id → cues) groups."""

    validation_status: str  # "passed" | "needs_review" | "failed"
    issues: list[ValidationIssue]
    block_summaries: list[BlockSummary]


# ---------------------------------------------------------------------------
# Internal accumulator (mutable, not exported)
# ---------------------------------------------------------------------------


@dataclass
class _BlockAccumulator:
    """Mutable per-block working state used during validation."""

    block_id: str
    cue_count: int = 0
    text_mismatch: bool = False
    timing_overlap_count: int = 0
    timing_out_of_block_count: int = 0
    empty_cue_count: int = 0
    long_unbreakable_count: int = 0
    unknown_mixed_token_count: int = 0
    short_display_duration_count: int = 0
    text_audio_drift: bool = False

    def to_summary(self) -> BlockSummary:
        return BlockSummary(
            block_id=self.block_id,
            cue_count=self.cue_count,
            text_mismatch=self.text_mismatch,
            timing_overlap_count=self.timing_overlap_count,
            timing_out_of_block_count=self.timing_out_of_block_count,
            empty_cue_count=self.empty_cue_count,
            long_unbreakable_count=self.long_unbreakable_count,
            unknown_mixed_token_count=self.unknown_mixed_token_count,
            short_display_duration_count=self.short_display_duration_count,
            text_audio_drift=self.text_audio_drift,
        )


def _cue_texts_match_block(cue_texts: list[str], block_text: str) -> bool:
    """Whitespace-tolerant ONLY at cue boundaries (@codex review round-2 P2).

    Cue construction legitimately loses inter-span whitespace: segment_text()
    spans keep raw boundary whitespace, but SubtitleCue.__post_init__ strips
    each cue's text. For space-delimited targets (zh->en jobs) the space after
    sentence punctuation therefore vanishes when cue texts are rejoined —
    "content?" + "If so..." must equal block "content? If so...".

    But INTERNAL spaces are content: dropping every space (the first-round
    fix) made "NewYork is great." equal "New York is great.", masking a real
    corruption. So: normalize() both sides, then require the block text to be
    exactly the cue texts in order, with any run of whitespace — including
    none — between consecutive cues (CJK blocks have no boundary space; Latin
    blocks have one). Inside a cue, text must match verbatim post-normalize().
    """
    pattern = r"\s*".join(re.escape(normalize(t)) for t in cue_texts)
    return re.fullmatch(pattern, normalize(block_text)) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_cues(
    *,
    block_specs: list[BlockSpec],
    cues: list[SubtitleCue],
    min_display_ms: int = 500,
) -> ValidationReport:
    """Validate *cues* against *block_specs* and return a ValidationReport.

    Checks performed (plan §8):

    Hard errors (severity="error"):
        text_mismatch       — cue texts, in order, do not reproduce the block
                              text allowing optional whitespace at cue
                              boundaries only (see _cue_texts_match_block;
                              internal spaces are content and compare exactly)
        timing_overlap      — cue[i].end_ms > cue[i+1].start_ms within same block
        timing_out_of_block — cue's [start_ms, end_ms] not inside [block_start_ms, block_end_ms]
        empty_cue           — cue.text == "" after strip (SubtitleCue already strips in __post_init__)
        unknown_block       — cue.block_id doesn't match any BlockSpec.block_id

    Review items (severity="review"):
        long_unbreakable_text       — propagated from cue.review_reason
        unknown_mixed_token         — propagated from cue.review_reason
        text_audio_may_need_review  — propagated from cue.review_reason (caller-set, not inferred)
        short_display_duration      — cue duration < min_display_ms

    Status determination:
        any error   → "failed"
        only review → "needs_review"
        no issues   → "passed"

    BlockSummaries are returned in the same order as input block_specs.
    """
    # Build spec lookup
    spec_map: dict[str, BlockSpec] = {s.block_id: s for s in block_specs}

    # Group cues by block_id (preserving unknown block cues separately)
    block_cues: dict[str, list[SubtitleCue]] = {s.block_id: [] for s in block_specs}
    unknown_block_cues: list[SubtitleCue] = []

    for cue in cues:
        if cue.block_id in spec_map:
            block_cues[cue.block_id].append(cue)
        else:
            unknown_block_cues.append(cue)

    issues: list[ValidationIssue] = []

    # --- Unknown block errors ---
    # Collect unique unknown block_ids to emit one block-level error per unknown block.
    seen_unknown: set[str] = set()
    for cue in unknown_block_cues:
        if cue.block_id not in seen_unknown:
            seen_unknown.add(cue.block_id)
            issues.append(
                ValidationIssue(
                    block_id=cue.block_id,
                    cue_id=None,
                    code="unknown_block",
                    severity="error",
                    message=(f"Block '{cue.block_id}' has no matching BlockSpec."),
                )
            )

    # --- Per-block validation ---
    accumulators: dict[str, _BlockAccumulator] = {
        s.block_id: _BlockAccumulator(block_id=s.block_id) for s in block_specs
    }

    for block_id, spec in spec_map.items():
        acc = accumulators[block_id]
        raw_cues = block_cues[block_id]

        # Sort cues by start_ms for overlap check (do not mutate original list).
        sorted_cues = sorted(raw_cues, key=lambda c: c.start_ms)
        acc.cue_count = len(sorted_cues)

        # --- text_audio_drift (block-level, 2026-05-04 P0b) ---
        # If the audio's source text (tts_input_cn_text) differs from the
        # current cn_text (merged_cn_text), the user edited cn_text without
        # regenerating TTS — subtitles drawn from cn_text will not match
        # what the audio actually says. Emit as a "review" issue so callers
        # know to fall back to the safe proportional cue layout (Phase C
        # whisper alignment will use this flag to skip drift blocks).
        # Empty tts_input_cn_text is treated as in-sync (legacy / pre-rollout
        # block) — Phase A's load-time backfill should have filled it, but
        # defense-in-depth keeps cue pipeline robust across version skews.
        if spec.tts_input_cn_text:
            if normalize(spec.tts_input_cn_text) != normalize(spec.merged_cn_text):
                acc.text_audio_drift = True
                issues.append(
                    ValidationIssue(
                        block_id=block_id,
                        cue_id=None,
                        code="text_audio_drift",
                        severity="review",
                        message=(
                            f"Block '{block_id}': cn_text "
                            f"{normalize(spec.merged_cn_text)!r} differs "
                            f"from tts_input_cn_text "
                            f"{normalize(spec.tts_input_cn_text)!r} — "
                            "subtitle text may not match audio. "
                            "User edited text without re-generating TTS."
                        ),
                    )
                )

        # --- text_mismatch (block-level) ---
        joined = "".join(c.text for c in sorted_cues)
        if not _cue_texts_match_block([c.text for c in sorted_cues], spec.merged_cn_text):
            acc.text_mismatch = True
            issues.append(
                ValidationIssue(
                    block_id=block_id,
                    cue_id=None,
                    code="text_mismatch",
                    severity="error",
                    message=(
                        f"Block '{block_id}': cue text joined "
                        f"{normalize(joined)!r} != block text "
                        f"{normalize(spec.merged_cn_text)!r}."
                    ),
                )
            )

        # --- Per-cue checks ---
        for ix, cue in enumerate(sorted_cues):
            # empty_cue
            if cue.text.strip() == "":
                acc.empty_cue_count += 1
                issues.append(
                    ValidationIssue(
                        block_id=block_id,
                        cue_id=cue.cue_id,
                        code="empty_cue",
                        severity="error",
                        message=f"Cue '{cue.cue_id}' has empty text.",
                    )
                )

            # timing_out_of_block
            if cue.start_ms < spec.start_ms or cue.end_ms > spec.end_ms:
                acc.timing_out_of_block_count += 1
                issues.append(
                    ValidationIssue(
                        block_id=block_id,
                        cue_id=cue.cue_id,
                        code="timing_out_of_block",
                        severity="error",
                        message=(
                            f"Cue '{cue.cue_id}' [{cue.start_ms}, {cue.end_ms}] "
                            f"is outside block slot [{spec.start_ms}, {spec.end_ms}]."
                        ),
                    )
                )

            # timing_overlap (check against next cue in sorted order)
            if ix + 1 < len(sorted_cues):
                next_cue = sorted_cues[ix + 1]
                if cue.end_ms > next_cue.start_ms:
                    acc.timing_overlap_count += 1
                    issues.append(
                        ValidationIssue(
                            block_id=block_id,
                            cue_id=next_cue.cue_id,  # the LATER cue
                            code="timing_overlap",
                            severity="error",
                            message=(
                                f"Cue '{next_cue.cue_id}' starts at {next_cue.start_ms}ms "
                                f"before previous cue '{cue.cue_id}' ends at {cue.end_ms}ms."
                            ),
                        )
                    )

            # short_display_duration (review)
            duration = cue.end_ms - cue.start_ms
            if duration < min_display_ms:
                acc.short_display_duration_count += 1
                issues.append(
                    ValidationIssue(
                        block_id=block_id,
                        cue_id=cue.cue_id,
                        code="short_display_duration",
                        severity="review",
                        message=(f"Cue '{cue.cue_id}' duration {duration}ms is below minimum {min_display_ms}ms."),
                    )
                )

            # review_reason propagation (long_unbreakable_text, unknown_mixed_token,
            # text_audio_may_need_review)
            if cue.review_reason in _REVIEW_REASONS:
                if cue.review_reason == "long_unbreakable_text":
                    acc.long_unbreakable_count += 1
                elif cue.review_reason == "unknown_mixed_token":
                    acc.unknown_mixed_token_count += 1
                issues.append(
                    ValidationIssue(
                        block_id=block_id,
                        cue_id=cue.cue_id,
                        code=cue.review_reason,
                        severity="review",
                        message=(f"Cue '{cue.cue_id}' flagged for review: {cue.review_reason}."),
                    )
                )

    # --- Build summaries in block_specs input order ---
    block_summaries = [accumulators[s.block_id].to_summary() for s in block_specs]

    # --- Determine status ---
    has_error = any(i.severity == "error" for i in issues)
    has_review = any(i.severity == "review" for i in issues)

    if has_error:
        status = "failed"
    elif has_review:
        status = "needs_review"
    else:
        status = "passed"

    return ValidationReport(
        validation_status=status,
        issues=issues,
        block_summaries=block_summaries,
    )

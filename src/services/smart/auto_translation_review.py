"""Smart MVP §6.2.2 — auto translation review (PR#3A, business-logic only).

Pure-deterministic 6-check decision module. Given a translation review
payload + supporting stats, decide whether Smart can auto-approve OR
must hand off to Studio for human review.

The 6 checks (plan §6.2.2 + Codex F6):
  1. glossary_preservation_rate >= GLOSSARY_PRESERVATION_THRESHOLD
  2. speaker assignment consistent (no segment with conflicting
     speaker_id between translation and S2 voice selection)
  3. length budget overflow <= LENGTH_BUDGET_OVERFLOW_THRESHOLD
  4. final_spoken_text checksum matches subtitle_source_text
     (P2 sources both from segment.tts_input_cn_text per §4.7)
  5. uncertain_speaker_duration_share <= UNCERTAIN_SHARE_THRESHOLD
  6. clone_eligible_speakers / asr_speaker_count >= CLONE_ELIGIBLE_RATIO

Reason codes mirror smart_shadow_sim_simulator.py constants exactly so
offline shadow comparisons line up with production behaviour. See
scripts/smart_shadow_sim_simulator.py:181-203 for the reference impl.

This module:
  - is pure: no I/O, no provider, no review_state mutation
  - returns ``TranslationReviewDecision`` so caller (process.py
    integration in PR#3C) can act
  - sidecar emit happens via sidecar_emitter.emit_smart_decision —
    different writer, same payload

Acceptance tests: tests/test_smart_business_logic.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# Thresholds — mirror simulator constants. Plan §6.2.2 specifies the
# numeric values; pinning them here as module constants makes drift
# obvious in code review.
GLOSSARY_PRESERVATION_THRESHOLD = 0.80  # plan §6.2.2 step 1
LENGTH_BUDGET_OVERFLOW_THRESHOLD = 0.15  # plan §6.2.2 step 3 (15%)
UNCERTAIN_SHARE_THRESHOLD = 0.10  # plan §6.2.2 step 5 / simulator
CLONE_ELIGIBLE_RATIO_THRESHOLD = 0.50  # plan §6.2.2 step 6 / simulator


@dataclass(frozen=True)
class TranslationReviewDecision:
    """6-check verdict.

    ``auto_approved=True`` means the caller can write
    review_state.set_stage(TRANSLATION_REVIEW_STAGE, status=APPROVED) +
    fall through to TTS without human intervention. ``auto_approved=False``
    means the caller must trigger handoff with smart_state.reason set
    to ``reason_code``.
    """

    auto_approved: bool
    reason_code: str | None  # None when auto_approved=True
    failed_check: str | None  # human label for the first failed check
    metrics: dict[str, Any]  # raw values that drove the decision


def _check_glossary_preservation(
    translation_result: Mapping[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    glossary_total = float(translation_result.get("glossary_total_terms") or 0)
    glossary_preserved = float(translation_result.get("glossary_preserved_terms") or 0)
    if glossary_total == 0:
        # No glossary configured — vacuously passes (rate is undefined,
        # not 0). Caller upstream is expected to have recorded
        # glossary_source="none" so the audit trail is clear.
        return True, None, {"glossary_total_terms": 0, "glossary_preservation_rate": None}
    rate = glossary_preserved / glossary_total
    metrics = {
        "glossary_total_terms": int(glossary_total),
        "glossary_preserved_terms": int(glossary_preserved),
        "glossary_preservation_rate": rate,
    }
    if rate < GLOSSARY_PRESERVATION_THRESHOLD:
        return False, f"glossary_preservation_low_{rate:.2f}", metrics
    return True, None, metrics


def _check_speaker_assignment_consistent(
    translation_result: Mapping[str, Any],
    speaker_diff: Mapping[str, Any] | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Each translation segment's speaker_id must agree with the S2
    final speaker assignment (post Pass 1 corrections). A mismatch
    means S2 and translation disagree about who's talking — auto-
    approve unsafe.

    speaker_diff structure (mirrors S2 output): a mapping from
    segment_id → expected speaker_id (the S2-confirmed value). If
    None or empty, this check is vacuously passed (no diff data to
    contradict).
    """
    if not speaker_diff:
        return True, None, {"speaker_assignment_checked": False}
    segments = translation_result.get("segments") or []
    mismatches: list[dict[str, str]] = []
    for seg in segments:
        if not isinstance(seg, Mapping):
            continue
        seg_id = str(seg.get("segment_id") or seg.get("index") or "")
        translation_speaker = str(seg.get("speaker_id") or "")
        expected = speaker_diff.get(seg_id)
        if expected and translation_speaker and translation_speaker != expected:
            mismatches.append(
                {
                    "segment_id": seg_id,
                    "translation_speaker": translation_speaker,
                    "expected_speaker": str(expected),
                }
            )
    metrics = {
        "speaker_assignment_checked": True,
        "speaker_mismatch_count": len(mismatches),
        "speaker_mismatches": mismatches[:10],  # cap for sanity
    }
    if mismatches:
        return False, f"speaker_assignment_mismatch_{len(mismatches)}", metrics
    return True, None, metrics


def _check_length_budget(
    translation_result: Mapping[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Total translated CN char count vs source duration budget.
    Overflow rate beyond LENGTH_BUDGET_OVERFLOW_THRESHOLD after one
    rewrite pass means auto-approve unsafe."""
    overflow_rate = translation_result.get("length_overflow_rate")
    rewrite_attempted = bool(translation_result.get("rewrite_attempted") or False)
    metrics = {
        "length_overflow_rate": overflow_rate,
        "rewrite_attempted": rewrite_attempted,
    }
    if overflow_rate is None:
        # Upstream didn't compute — treat as pass (vacuous), but record so
        # the audit trail shows the gap.
        metrics["length_overflow_unknown"] = True
        return True, None, metrics
    if float(overflow_rate) > LENGTH_BUDGET_OVERFLOW_THRESHOLD:
        return (
            False,
            f"length_overflow_post_rewrite_{float(overflow_rate):.2%}",
            metrics,
        )
    return True, None, metrics


def _check_text_audio_checksum(
    translation_result: Mapping[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    """final_spoken_text vs subtitle_source_text checksum match per §4.7.
    For P2 the two are sourced from the same field (tts_input_cn_text)
    so a mismatch means a pipeline bug — definitely not auto-approvable."""
    expected = translation_result.get("subtitle_source_text_sha256")
    actual = translation_result.get("final_spoken_text_sha256")
    metrics = {
        "subtitle_source_text_sha256": expected,
        "final_spoken_text_sha256": actual,
    }
    if expected is None and actual is None:
        # Pre-§4.7 jobs without checksums — pass with note.
        metrics["text_audio_checksum_unknown"] = True
        return True, None, metrics
    if expected != actual:
        return False, "text_audio_checksum_mismatch", metrics
    return True, None, metrics


def _check_uncertain_speaker_share(
    speaker_stats: Mapping[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Plan §6.2.2 step 5 / simulator TRANSLATION_REVIEW_UNCERTAIN_THRESHOLD.
    High uncertain-speaker share means S2 had many ambiguous segments —
    auto-approve risks compounding ambiguity.

    Codex 第九轮 P1-2: missing signal → fail-closed (unevaluable_missing_signals).
    Mirrors simulator (smart_shadow_sim_simulator.py:187) which returns
    decision=unevaluable when this field is missing — vacuous-pass would
    silently auto-approve any job whose upstream collector failed to
    populate the field.
    """
    share = speaker_stats.get("uncertain_speaker_duration_share")
    metrics = {"uncertain_speaker_duration_share": share}
    if share is None:
        return False, "unevaluable_missing_uncertain_speaker_share", metrics
    if float(share) > UNCERTAIN_SHARE_THRESHOLD:
        return (
            False,
            f"high_uncertain_speaker_share_{float(share):.2f}",
            metrics,
        )
    return True, None, metrics


def _check_clone_eligible_ratio(
    speaker_stats: Mapping[str, Any],
    clone_sample_stats: Mapping[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Plan §6.2.2 step 6 / simulator TRANSLATION_REVIEW_MIN_CLONE_ELIGIBLE_RATIO.
    < 50% of ASR speakers having sufficient clone samples means the
    voice match downstream will mostly fall to presets — auto-approval
    of translation is premature when voice quality is at risk.

    Codex 第九轮 P1-2: missing signal → fail-closed; same rationale as
    _check_uncertain_speaker_share. Codex 第九轮 P1-3: reason_code
    format must mirror simulator exactly — "{eligible}/{asr}" with a
    forward slash, not "_of_" — so shadow-vs-production diff stays
    apples-to-apples.
    """
    asr_count = speaker_stats.get("asr_speaker_count")
    eligible = clone_sample_stats.get("eligible_speakers")
    metrics = {
        "asr_speaker_count": asr_count,
        "eligible_speakers": eligible,
    }
    if asr_count is None or eligible is None:
        return False, "unevaluable_missing_clone_signals", metrics
    asr = float(asr_count)
    if asr <= 0:
        # Div-by-zero guard. Treat as missing signal (fail-closed) —
        # 0 ASR speakers is itself an upstream-data anomaly that
        # shouldn't auto-approve.
        return False, "unevaluable_zero_asr_speakers", metrics
    ratio = float(eligible) / asr
    metrics["clone_eligible_ratio"] = ratio
    if ratio < CLONE_ELIGIBLE_RATIO_THRESHOLD:
        return (
            False,
            f"low_clone_eligible_ratio_{int(eligible)}/{int(asr)}",
            metrics,
        )
    return True, None, metrics


def evaluate_translation_review(
    *,
    translation_result: Mapping[str, Any],
    speaker_stats: Mapping[str, Any],
    clone_sample_stats: Mapping[str, Any],
    speaker_diff: Mapping[str, Any] | None = None,
    compliance_block: bool = False,
) -> TranslationReviewDecision:
    """Run all 6 + compliance checks in plan order; first failure wins.

    ``compliance_block``: short-circuit when the existing content-compliance
    pipeline has already flagged the translation as high-risk. Caller
    is responsible for plumbing the existing compliance signal in.

    Returns ``TranslationReviewDecision`` whose ``reason_code`` mirrors
    ``smart_shadow_sim_simulator.py`` so offline-vs-production diff is
    apples-to-apples.

    Order matters: the test suite asserts on first-failure semantics
    (e.g. uncertain-share failure must take precedence over a downstream
    clone-eligible failure when both trip).
    """
    aggregated_metrics: dict[str, Any] = {}

    # Run in plan order so the reason_code reflects the FIRST failing
    # check the user / dataset audit will see.
    checks = (
        ("glossary_preservation", _check_glossary_preservation, (translation_result,)),
        (
            "speaker_assignment",
            _check_speaker_assignment_consistent,
            (translation_result, speaker_diff),
        ),
        ("length_budget", _check_length_budget, (translation_result,)),
        ("text_audio_checksum", _check_text_audio_checksum, (translation_result,)),
        (
            "uncertain_speaker_share",
            _check_uncertain_speaker_share,
            (speaker_stats,),
        ),
        (
            "clone_eligible_ratio",
            _check_clone_eligible_ratio,
            (speaker_stats, clone_sample_stats),
        ),
    )
    for label, fn, args in checks:
        ok, reason, metrics = fn(*args)
        aggregated_metrics.update(metrics)
        if not ok:
            return TranslationReviewDecision(
                auto_approved=False,
                reason_code=reason,
                failed_check=label,
                metrics=aggregated_metrics,
            )

    # Compliance is a single-bit signal from the existing pipeline path
    # (Smart MVP doesn't add new compliance models per plan §13). Short-
    # circuit AFTER the deterministic checks so audit metrics still get
    # populated (callers might want to see "would have passed deterministic
    # but compliance blocked").
    if compliance_block:
        aggregated_metrics["compliance_block"] = True
        return TranslationReviewDecision(
            auto_approved=False,
            reason_code="compliance_high_risk",
            failed_check="content_compliance",
            metrics=aggregated_metrics,
        )

    return TranslationReviewDecision(
        auto_approved=True,
        reason_code=None,
        failed_check=None,
        metrics=aggregated_metrics,
    )


__all__ = [
    "CLONE_ELIGIBLE_RATIO_THRESHOLD",
    "GLOSSARY_PRESERVATION_THRESHOLD",
    "LENGTH_BUDGET_OVERFLOW_THRESHOLD",
    "TranslationReviewDecision",
    "UNCERTAIN_SHARE_THRESHOLD",
    "evaluate_translation_review",
]

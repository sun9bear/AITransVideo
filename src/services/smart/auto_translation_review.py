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

    Missing signal handled in the unified ``_precheck_missing_signals``
    pre-pass (called once before the 6 deterministic checks); this
    function only sees populated values. Codex 第十轮 P2: unifying the
    missing-signal reason aligns with simulator
    (smart_shadow_sim_simulator.py:187) which returns a single
    ``missing_signals`` reason + evidence-listed missing fields rather
    than per-field unevaluable codes.
    """
    share = speaker_stats.get("uncertain_speaker_duration_share")
    metrics = {"uncertain_speaker_duration_share": share}
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

    Missing signals handled in the unified pre-pass (see above). Codex
    第九轮 P1-3: reason_code format must mirror simulator exactly —
    "{eligible}/{asr}" with a forward slash, not "_of_" — so shadow-vs-
    production diff stays apples-to-apples.
    """
    asr_count = speaker_stats.get("asr_speaker_count")
    eligible = clone_sample_stats.get("eligible_speakers")
    metrics = {
        "asr_speaker_count": asr_count,
        "eligible_speakers": eligible,
    }
    asr = float(asr_count)
    if asr <= 0:
        # Div-by-zero guard. 0 ASR speakers is itself an upstream-data
        # anomaly that shouldn't auto-approve. Distinct from missing
        # signal — the field IS populated, just with a degenerate value.
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


# ---------------------------------------------------------------------------
# Missing-signals pre-pass (Codex 第十轮 P2)
# ---------------------------------------------------------------------------
#
# Simulator (scripts/smart_shadow_sim_simulator.py:187) checks all three
# missing-signal candidates in one go and returns a single
# ``reason="missing_signals"`` with an evidence list of the missing
# field names. To keep shadow-vs-production reason aggregation
# apples-to-apples, do the same here as a pre-pass before the 6
# deterministic checks. The 6 checks then only deal with populated
# values, simplifying their bodies.

_MISSING_SIGNAL_REASON = "missing_signals"

_MISSING_SIGNAL_FIELDS = (
    ("uncertain_speaker_duration_share", "speaker_stats"),
    ("asr_speaker_count", "speaker_stats"),
    ("eligible_speakers", "clone_sample_stats"),
)


def _precheck_missing_signals(
    *,
    speaker_stats: Mapping[str, Any],
    clone_sample_stats: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Return (ok, evidence). ok=False when any of the three required
    signals is missing; evidence carries a ``missing`` list with the
    field names so the caller can attach to TranslationReviewDecision.metrics.
    """
    sources = {
        "speaker_stats": speaker_stats,
        "clone_sample_stats": clone_sample_stats,
    }
    missing: list[str] = []
    for field_name, source_key in _MISSING_SIGNAL_FIELDS:
        if sources[source_key].get(field_name) is None:
            missing.append(field_name)
    if missing:
        return False, {"missing": missing}
    return True, {}


def evaluate_translation_review(
    *,
    translation_result: Mapping[str, Any],
    speaker_stats: Mapping[str, Any],
    clone_sample_stats: Mapping[str, Any],
    speaker_diff: Mapping[str, Any] | None = None,
    compliance_block: bool = False,
) -> TranslationReviewDecision:
    """Run all 6 deterministic checks for AUDIT METRICS ONLY.

    == 2026-05-20 spec change (user request) ==

    Smart mode 的初衷是「全自动」。除了下列硬限制以外，所有翻译审核期
    检查不再阻挡 pipeline：

      - eligibility / sample / clone quota / clone expiry / weak match —
        外部硬限制或 admin opt-in，保留
      - **content compliance** — 法律/安全风险，移到 S1 后早期 gate
        统一处理（pipeline 退出 + 退款），不再在此模块判定

    本函数保留所有 6 个检查的调用纯粹是为了**填 metrics**（让
    smart_quality_report 仍然展示 glossary 保留率、speaker 一致性等
    审计信号），但**永远返回 auto_approved=True**。

    历史行为（plan §6.2.2 first-failure）已废弃。如果未来想恢复严格
    模式，让某项 metric 变回硬 gate，那时再加一个 admin 开关，但
    默认必须是 auto-pass。

    ``compliance_block`` 参数保留以兼容旧调用方，但**不再读取** —
    合规阻挡的责任已经搬到 process.py 早期 gate。
    """
    aggregated_metrics: dict[str, Any] = {}

    # 跑 missing-signals precheck，只为采集 evidence；不再返回 False。
    signals_ok, missing_evidence = _precheck_missing_signals(
        speaker_stats=speaker_stats,
        clone_sample_stats=clone_sample_stats,
    )
    if not signals_ok:
        aggregated_metrics.update(missing_evidence)
        aggregated_metrics["missing_signals_advisory"] = True

    # 跑 6 个检查只为填 metrics；忽略 ok/reason 结果。
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
        try:
            ok, reason, metrics = fn(*args)
        except Exception:
            # Audit-only — failure to compute a metric must not block
            # the pipeline. Just skip and move on.
            continue
        aggregated_metrics.update(metrics)
        if not ok:
            # Annotate which advisory check would have failed under
            # the OLD strict policy, so admin QA can post-hoc review.
            aggregated_metrics[f"{label}_advisory_reason"] = reason

    # NOTE: compliance_block kwarg is intentionally ignored. The early-
    # pipeline gate in process.py (post-S1 transcript, pre-S3 translate)
    # exits the pipeline outright on content_compliance_payload.status
    # == "blocked", so by the time we reach translation review the
    # job has already been admitted past the legal gate.

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

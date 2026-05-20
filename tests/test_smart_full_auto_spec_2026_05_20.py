"""Smart 全自动化原则 — 2026-05-20 spec change.

== User feedback ==

After job_88bdca0966ce468fb6af36dc0bf4adeb (Google I/O 2026 keynote,
9 distinct speakers) hit translation_review handoff for
``uncertain_speaker_share=0.78`` despite 100% glossary preservation,
user clarified the smart product contract:

    "智能版还是会进翻译审核让用户确认的页面，智能版的初衷是全部自动
     完成的，后来只是加了音色克隆之前，个人音色库如果有弱匹配的个人
     音色，让用户确认一下，其它的都要自动完成"

That is: smart mode MUST be fully automatic, with these specific
exceptions (and only these):

  1. Eligibility gate (>3 main speakers) — physical product limit
  2. Sample insufficient (<10s clean audio) — pre-flight before paying
  3. Voice library safety water mark (≤3 free slots) — pre-flight at
     /jobs POST (admin can disable, admin role bypassed)
  4. MiniMax quota exhausted mid-flight — external provider limit
  5. Cloned voice expired between clone and TTS — external state
  6. Weak voice library match (Phase 4, admin opt-in default OFF) —
     the ONE in-pipeline confirmation user explicitly wants
  7. Content compliance violation — legal/safety, exits pipeline
     (not handoff) per ``ContentPolicyViolationError``

Everything else that USED to handoff at translation_review (glossary
< 80%, length budget overflow, text/audio checksum mismatch,
speaker assignment conflict, uncertain speaker share above
threshold, clone-eligible ratio low, missing signals) is now
audit-only — metrics still recorded for admin QA but the pipeline
proceeds to TTS automatically.

== This test file ==

Pins the ENTIRE smart full-auto contract source-side. Other tests
cover individual checks; this file is the spec gateway.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PROCESS_PY = _SRC / "pipeline" / "process.py"
_AUTO_TRANSLATION_REVIEW = _SRC / "services" / "smart" / "auto_translation_review.py"


class TestEvaluateTranslationReviewAlwaysAutoApproves:
    """``evaluate_translation_review`` must never return
    ``auto_approved=False`` for any input — that was the old
    strict-spec behavior."""

    def _passing_inputs(self):
        return {
            "translation_result": {
                "glossary_total_terms": 10,
                "glossary_preserved_terms": 9,
                "length_overflow_rate": 0.05,
                "rewrite_attempted": False,
                "subtitle_source_text_sha256": "abc",
                "final_spoken_text_sha256": "abc",
                "segments": [{"segment_id": "s1", "speaker_id": "speaker_a"}],
            },
            "speaker_stats": {
                "uncertain_speaker_duration_share": 0.05,
                "asr_speaker_count": 2,
            },
            "clone_sample_stats": {"eligible_speakers": 2},
        }

    def test_old_strict_spec_failures_now_auto_approve(self):
        """Sweep across every condition that USED to trigger handoff
        and verify each one now auto-passes."""
        from services.smart.auto_translation_review import evaluate_translation_review

        # Each tuple: (mutation_description, mutation_callable)
        scenarios = [
            (
                "glossary 0% preservation",
                lambda inp: inp["translation_result"].update({"glossary_preserved_terms": 0}),
            ),
            (
                "glossary 50% (below 80% old threshold)",
                lambda inp: inp["translation_result"].update({"glossary_preserved_terms": 5}),
            ),
            (
                "length overflow 30% (above 15% old threshold)",
                lambda inp: inp["translation_result"].update({"length_overflow_rate": 0.30}),
            ),
            (
                "text/audio checksum mismatch",
                lambda inp: inp["translation_result"].update({"final_spoken_text_sha256": "DIFFERENT"}),
            ),
            (
                "uncertain speaker share 78% (Google I/O scenario)",
                lambda inp: inp["speaker_stats"].update({"uncertain_speaker_duration_share": 0.78}),
            ),
            (
                "clone-eligible ratio 1/4 (below 50% old threshold)",
                lambda inp: (
                    inp["clone_sample_stats"].update({"eligible_speakers": 1}),
                    inp["speaker_stats"].update({"asr_speaker_count": 4}),
                ),
            ),
            (
                "zero ASR speakers (old: unevaluable hard fail)",
                lambda inp: inp["speaker_stats"].update({"asr_speaker_count": 0}),
            ),
            (
                "compliance_block=True (kwarg now ignored)",
                lambda inp: None,
            ),
        ]

        for description, mutate in scenarios:
            inputs = self._passing_inputs()
            if mutate is not None:
                mutate(inputs)
            kwargs = {}
            if description.startswith("compliance_block"):
                kwargs["compliance_block"] = True
            if "speaker_diff" in description:
                kwargs["speaker_diff"] = {"s1": "speaker_b"}
            decision = evaluate_translation_review(**inputs, **kwargs)
            assert decision.auto_approved is True, (
                f"Scenario '{description}' returned auto_approved=False "
                f"(reason={decision.reason_code!r}). New spec: smart "
                f"translation_review must NEVER block."
            )
            assert decision.reason_code is None, (
                f"Scenario '{description}' has reason_code={decision.reason_code!r}; "
                f"new spec returns None unconditionally."
            )

    def test_speaker_diff_mismatch_still_auto_approves(self):
        """Separate test because speaker_diff is a kwarg not part of
        the standard inputs dict."""
        from services.smart.auto_translation_review import evaluate_translation_review

        decision = evaluate_translation_review(
            **self._passing_inputs(),
            speaker_diff={"s1": "speaker_b"},  # mismatch
        )
        assert decision.auto_approved is True


class TestProcessPyCompliancePathStillExits:
    """Compliance check moved early (already at post-S1 location).
    On block, ``_run_content_compliance_review`` raises
    ``ContentPolicyViolationError`` which propagates up + is tagged
    as S2 failure by ``_classify_failed_stage``. Pipeline marks job
    failed with stage=S2 — this is what user wants ("退出流程").

    Pin the source-level structure so a future refactor doesn't
    accidentally remove the early exit."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_run_content_compliance_review_raises_on_block(self):
        """The early-pipeline gate. Non-admin compliance block →
        raise → top-level exception handler marks job failed."""
        source = self._source()
        # The raise site is in _run_content_compliance_review.
        assert "raise ContentPolicyViolationError(final_result)" in source, (
            "Early compliance gate missing — content_compliance must "
            "raise on block, which is what propagates up to fail the "
            "pipeline cleanly before S3 translation costs are incurred."
        )

    def test_compliance_error_classified_as_s2_failure(self):
        """``_classify_failed_stage`` tags ContentPolicyViolationError
        as S2 so the user-visible error_summary surfaces correctly."""
        source = self._source()
        # Anchor on the classifier
        classifier_idx = source.find("def _classify_failed_stage")
        assert classifier_idx >= 0
        # Window 1500 chars
        window = source[classifier_idx : classifier_idx + 1500]
        assert "isinstance(exc, ContentPolicyViolationError)" in window
        assert 'return "S2"' in window


class TestDefensiveHandoffsRemoved:
    """#7 (quota lookup failed) and #8 (clone mirror DB failed) used
    to handoff. Per 2026-05-20 spec these are infra issues, not user
    issues — must log + continue."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_quota_lookup_failure_logs_continues(self):
        source = self._source()
        # Find the "if _smart_quota_remaining is None:" block
        idx = source.find("if _smart_quota_remaining is None:")
        assert idx >= 0
        window = source[idx : idx + 3000]
        # New spec markers
        assert "quota_lookup_degraded" in window, (
            "Quota-unavailable branch should emit 'quota_lookup_degraded' "
            "audit (not 'voice_library_quota_unavailable' handoff)."
        )
        assert "999_999" in window, (
            "Quota-unavailable branch should set fallback quota to "
            "999_999 (effectively unlimited; real MiniMax quota still "
            "enforces upper bound)."
        )
        # Old handoff markers MUST be gone from this branch
        # (~2000-char window covers the entire fallback block)
        fallback_block = source[idx : idx + 2000]
        assert "emit_handoff_markers" not in fallback_block, (
            "Quota-unavailable branch still calls emit_handoff_markers — "
            "regression to old spec. Should be log + continue."
        )

    def test_clone_mirror_failure_logs_continues(self):
        source = self._source()
        # Find "if _smart_clone_mirror_failures:"
        idx = source.find("if _smart_clone_mirror_failures:")
        assert idx >= 0
        window = source[idx : idx + 3000]
        # New spec markers
        assert "clone_mirror_degraded" in window, (
            "Clone mirror failure branch should emit "
            "'clone_mirror_degraded' audit (not "
            "'clone_library_register_failed' handoff)."
        )
        # Old handoff markers MUST be gone from this branch
        fallback_block = source[idx : idx + 2500]
        assert "emit_handoff_markers" not in fallback_block, (
            "Clone mirror failure branch still calls "
            "emit_handoff_markers — regression to old spec."
        )
        assert "self._build_paused_result(" not in fallback_block, (
            "Clone mirror failure branch still returns paused result — "
            "regression to old spec. Should continue pipeline."
        )


class TestComplianceBlockKwargIsLegacy:
    """The ``compliance_block`` kwarg on ``evaluate_translation_review``
    is retained for backward-compat (callers may still pass it) but
    is no longer consumed."""

    def test_function_signature_still_accepts_compliance_block(self):
        from services.smart.auto_translation_review import evaluate_translation_review
        import inspect

        sig = inspect.signature(evaluate_translation_review)
        assert "compliance_block" in sig.parameters, (
            "compliance_block kwarg removed from signature — would "
            "break process.py and any other caller that still passes "
            "it. Keep the kwarg, just ignore the value."
        )

    def test_source_documents_compliance_block_is_ignored(self):
        """Inline comment must explain compliance_block is intentionally
        ignored to prevent future devs from re-wiring it."""
        source = _AUTO_TRANSLATION_REVIEW.read_text(encoding="utf-8")
        assert "compliance_block kwarg is intentionally ignored" in source.lower() or (
            "compliance_block" in source.lower() and "ignored" in source.lower()
        ), (
            "auto_translation_review.py should have a comment explaining "
            "compliance_block is no longer consumed (compliance handled "
            "via early-pipeline ContentPolicyViolationError path)."
        )

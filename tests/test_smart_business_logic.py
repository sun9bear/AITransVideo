"""Smart MVP P2 — business-logic acceptance suite (PR#3A).

Four pure modules, no process.py / no real provider. Locks the
deterministic contracts so the pipeline integration layer (PR#3C) can
trust them without re-deriving the math at each call site.

  - TestEligibilityGate (boundaries + exclusions + edge cases)
  - TestAutoTranslationReview (6 checks + first-failure + compliance)
  - TestRetryBudget (formula + per-segment cap + whole-task cap)
  - TestSidecarEmitter (append-only + atomic + failure paths + schema)
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Repo path setup — mirrors tests/conftest.py
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===================================================================
# eligibility_gate
# ===================================================================


class TestEligibilityGate:
    """Plan §6.1 + 主方案 §2.3."""

    def _stats(self, *speakers):
        return {"speakers": list(speakers)}

    def _sp(self, sid, share, *, dubbing_mode="dub", role=None):
        d = {"speaker_id": sid, "duration_share": share, "dubbing_mode": dubbing_mode}
        if role:
            d["role"] = role
        return d

    def test_one_main_speaker_approved(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(self._stats(self._sp("speaker_a", 1.0)))
        assert decision.approved is True
        assert decision.main_speaker_count == 1
        assert decision.main_speaker_ids == ("speaker_a",)
        assert decision.reason_code is None

    def test_two_main_speakers_approved(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(self._sp("a", 0.6), self._sp("b", 0.4))
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 2

    def test_three_main_speakers_approved_boundary(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("a", 0.5), self._sp("b", 0.3), self._sp("c", 0.2)
            )
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 3

    def test_four_main_speakers_rejected_with_correct_reason(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("a", 0.4), self._sp("b", 0.3),
                self._sp("c", 0.2), self._sp("d", 0.1)
            )
        )
        assert decision.approved is False
        assert decision.main_speaker_count == 4
        assert decision.reason_code == "main_speaker_count_exceeded"

    def test_low_share_excluded_below_threshold(self):
        """Speaker with duration_share < 0.10 (default) does NOT count
        as main even if they have a speaker_id. This is what lets a
        '4-speaker' raw S2 result still pass Smart eligibility when
        one of them is a low-share short-interjection speaker."""
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("a", 0.45),
                self._sp("b", 0.45),
                self._sp("c", 0.05),  # below 0.10 threshold
                self._sp("d", 0.05),  # below 0.10 threshold
            )
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 2
        # Excluded speakers recorded with reason so sidecar can audit.
        excluded_ids = {e["speaker_id"] for e in decision.excluded_speakers}
        assert excluded_ids == {"c", "d"}
        for e in decision.excluded_speakers:
            assert e["reason"].startswith("low_share_")

    def test_keep_original_excluded_regardless_of_share(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("host", 0.5),
                # Big-share speaker but marked keep_original — must
                # NOT count against the main limit.
                self._sp("audience_chorus", 0.5, dubbing_mode="keep_original"),
            )
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 1
        assert decision.main_speaker_ids == ("host",)
        # Reason captured for audit.
        assert decision.excluded_speakers[0]["reason"] == "dubbing_mode_keep_original"

    def test_observer_role_excluded(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("host", 0.6),
                self._sp("audience", 0.4, role="audience"),
            )
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 1

    def test_custom_threshold_and_limit(self):
        """Tighter low_share + relaxed main_speaker_limit."""
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            self._stats(
                self._sp("a", 0.3), self._sp("b", 0.3),
                self._sp("c", 0.2), self._sp("d", 0.15),
                self._sp("e", 0.05),
            ),
            low_share_threshold=0.20,
            main_speaker_limit=4,
        )
        # Speakers d (0.15) and e (0.05) excluded — share < 0.20.
        # 3 mains (a/b/c), limit is 4 → approve.
        assert decision.approved is True
        assert decision.main_speaker_count == 3
        assert decision.threshold_used == 0.20
        assert decision.limit_used == 4

    def test_empty_speakers_returns_no_speakers_detected(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility({"speakers": []})
        assert decision.approved is False
        assert decision.reason_code == "no_speakers_detected"
        assert decision.main_speaker_count == 0

    def test_missing_speakers_key_returns_no_speakers_detected(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility({})
        assert decision.approved is False
        assert decision.reason_code == "no_speakers_detected"

    def test_speaker_row_without_id_excluded_with_reason(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility(
            {"speakers": [
                {"speaker_id": "host", "duration_share": 0.7, "dubbing_mode": "dub"},
                {"duration_share": 0.3, "dubbing_mode": "dub"},  # no speaker_id
            ]}
        )
        assert decision.approved is True
        assert decision.main_speaker_count == 1
        assert decision.main_speaker_ids == ("host",)
        # Anonymous row recorded as excluded with missing_speaker_id reason.
        reasons = [e["reason"] for e in decision.excluded_speakers]
        assert "missing_speaker_id" in reasons


# ===================================================================
# auto_translation_review
# ===================================================================


class TestAutoTranslationReview:
    """Plan §6.2.2 + Codex F6 — six checks + compliance + first-failure semantics."""

    def _passing_inputs(self):
        return {
            "translation_result": {
                "glossary_total_terms": 10,
                "glossary_preserved_terms": 9,  # 90% > 80%
                "length_overflow_rate": 0.05,
                "rewrite_attempted": False,
                "subtitle_source_text_sha256": "abc123",
                "final_spoken_text_sha256": "abc123",
                "segments": [
                    {"segment_id": "s1", "speaker_id": "speaker_a"},
                ],
            },
            "speaker_stats": {
                "uncertain_speaker_duration_share": 0.05,
                "asr_speaker_count": 2,
            },
            "clone_sample_stats": {"eligible_speakers": 2},
        }

    def test_all_checks_pass_returns_auto_approved(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        decision = evaluate_translation_review(**self._passing_inputs())
        assert decision.auto_approved is True
        assert decision.reason_code is None
        assert decision.failed_check is None
        assert decision.metrics["glossary_preservation_rate"] == pytest.approx(0.9)

    def test_glossary_preservation_below_threshold_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_preserved_terms"] = 6  # 60% < 80%
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.failed_check == "glossary_preservation"
        assert "glossary_preservation_low" in decision.reason_code

    def test_length_budget_overflow_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["length_overflow_rate"] = 0.20  # 20% > 15%
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.failed_check == "length_budget"
        assert "length_overflow_post_rewrite" in decision.reason_code

    def test_text_audio_checksum_mismatch_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["final_spoken_text_sha256"] = "different"
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "text_audio_checksum_mismatch"

    def test_uncertain_speaker_share_above_threshold_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["speaker_stats"]["uncertain_speaker_duration_share"] = 0.15
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.failed_check == "uncertain_speaker_share"
        assert decision.reason_code == "high_uncertain_speaker_share_0.15"

    def test_clone_eligible_ratio_below_threshold_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["clone_sample_stats"]["eligible_speakers"] = 1  # 1/3 < 0.5
        inputs["speaker_stats"]["asr_speaker_count"] = 3
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.failed_check == "clone_eligible_ratio"
        assert decision.reason_code == "low_clone_eligible_ratio_1_of_3"

    def test_speaker_mismatch_rejected(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        decision = evaluate_translation_review(
            **inputs,
            speaker_diff={"s1": "speaker_b"},  # translation says speaker_a
        )
        assert decision.auto_approved is False
        assert decision.failed_check == "speaker_assignment"

    def test_compliance_block_rejects_after_deterministic_pass(self):
        """compliance_block short-circuits, but only AFTER the 6 checks
        — so audit metrics still get populated for the would-have-
        passed deterministic state."""
        from services.smart.auto_translation_review import evaluate_translation_review

        decision = evaluate_translation_review(
            **self._passing_inputs(),
            compliance_block=True,
        )
        assert decision.auto_approved is False
        assert decision.reason_code == "compliance_high_risk"
        # Deterministic metrics still captured.
        assert decision.metrics["compliance_block"] is True
        assert decision.metrics["glossary_preservation_rate"] == pytest.approx(0.9)

    def test_first_failure_wins_when_multiple_violations(self):
        """If glossary fails AND uncertain-share fails, glossary wins
        (it's earlier in plan order). Lets ops triage the most-
        actionable failure first."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_preserved_terms"] = 1  # fail glossary
        inputs["speaker_stats"]["uncertain_speaker_duration_share"] = 0.99  # fail uncertain
        decision = evaluate_translation_review(**inputs)
        assert decision.failed_check == "glossary_preservation"

    def test_missing_glossary_treated_as_vacuous_pass(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_total_terms"] = 0
        inputs["translation_result"]["glossary_preserved_terms"] = 0
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.metrics["glossary_preservation_rate"] is None

    def test_missing_uncertain_share_treated_as_vacuous_pass(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        del inputs["speaker_stats"]["uncertain_speaker_duration_share"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.metrics.get("uncertain_speaker_share_unknown") is True


# ===================================================================
# retry_budget
# ===================================================================


class TestRetryBudget:
    """Plan §6.3."""

    def test_total_budget_short_video_uses_1_5x_multiplier(self):
        from services.smart.retry_budget import compute_total_budget_minutes

        # 10 min × 1.5 = 15 min (vs +30 = 40 min); min is 15.
        assert compute_total_budget_minutes(10) == 15.0

    def test_total_budget_at_crossover(self):
        from services.smart.retry_budget import compute_total_budget_minutes

        # 60 min × 1.5 = 90 min == 60 + 30. Both branches give 90.
        assert compute_total_budget_minutes(60) == 90.0

    def test_total_budget_long_video_uses_30min_offset(self):
        from services.smart.retry_budget import compute_total_budget_minutes

        # 120 min × 1.5 = 180 min vs 120 + 30 = 150 min; long-video cap.
        assert compute_total_budget_minutes(120) == 150.0

    def test_total_budget_zero_source_returns_zero(self):
        from services.smart.retry_budget import compute_total_budget_minutes

        assert compute_total_budget_minutes(0) == 0.0

    def test_retts_within_budget_approved(self):
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=60.0,  # 1 min consumed of 15
            per_segment_retts_taken=0,
            per_segment_rewrite_taken=0,
            avg_per_retts_audio_seconds=10.0,
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.RETTS)
        assert decision.allowed is True
        assert decision.reason == "approved"
        assert decision.total_budget_seconds == 900.0  # 15 min
        assert decision.remaining_seconds == 840.0  # 14 min left

    def test_per_segment_retts_cap_exhausted_refused(self):
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
            PER_SEGMENT_RETTS_CAP,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=0.0,
            per_segment_retts_taken=PER_SEGMENT_RETTS_CAP,
            per_segment_rewrite_taken=0,
            avg_per_retts_audio_seconds=0.0,
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.RETTS)
        assert decision.allowed is False
        assert "per_segment_retts_cap_exhausted" in decision.reason

    def test_per_segment_rewrite_cap_exhausted_refused(self):
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
            PER_SEGMENT_REWRITE_CAP,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=0.0,
            per_segment_retts_taken=0,
            per_segment_rewrite_taken=PER_SEGMENT_REWRITE_CAP,
            avg_per_retts_audio_seconds=0.0,
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.REWRITE)
        assert decision.allowed is False
        assert "per_segment_rewrite_cap_exhausted" in decision.reason

    def test_whole_task_budget_exhausted_refused(self):
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=900.0,  # 15 min == full budget
            per_segment_retts_taken=0,
            per_segment_rewrite_taken=0,
            avg_per_retts_audio_seconds=5.0,
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.RETTS)
        assert decision.allowed is False
        assert decision.reason == "whole_task_budget_exhausted"

    def test_remaining_below_avg_cost_refused(self):
        """plan §6.3: when remaining < avg per-retry cost, refuse so
        a runaway early segment doesn't starve later ones."""
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=895.0,  # 5s remaining
            per_segment_retts_taken=0,
            per_segment_rewrite_taken=0,
            avg_per_retts_audio_seconds=10.0,  # avg 10s
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.RETTS)
        assert decision.allowed is False
        assert "whole_task_remaining_below_avg_cost" in decision.reason

    def test_first_request_no_avg_yet_approved(self):
        """avg=0 (no prior data) skips the conservative gate."""
        from services.smart.retry_budget import (
            BudgetSnapshot, RetryKind, evaluate_retry_request,
        )

        snapshot = BudgetSnapshot(
            source_minutes=10.0,
            consumed_retts_audio_seconds=0.0,
            per_segment_retts_taken=0,
            per_segment_rewrite_taken=0,
            avg_per_retts_audio_seconds=0.0,
        )
        decision = evaluate_retry_request(snapshot, kind=RetryKind.RETTS)
        assert decision.allowed is True


# ===================================================================
# sidecar_emitter
# ===================================================================


class TestSidecarEmitter:
    """Plan §6.4. _file_lock + append-only + atomic + failure paths."""

    def test_emit_smart_decision_appends_one_line(self, tmp_path):
        from services.smart.sidecar_emitter import (
            emit_smart_decision, smart_decisions_path,
        )

        project_dir = tmp_path
        ok = emit_smart_decision(
            project_dir,
            decision_type="speaker_gate",
            decision="approved",
            evidence={"main_speaker_count": 2},
            reason_code=None,
            smart_decision_id="dec_001",
            created_at="2026-05-14T12:00:00Z",
        )
        assert ok is True

        path = smart_decisions_path(project_dir)
        assert path.exists()
        with open(path, encoding="utf-8") as fp:
            lines = fp.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["schema_version"] == 1
        assert record["decision_type"] == "speaker_gate"
        assert record["decision"] == "approved"
        assert record["smart_decision_id"] == "dec_001"
        assert record["evidence"]["main_speaker_count"] == 2

    def test_emit_smart_decision_appends_multiple_lines_in_order(self, tmp_path):
        """Append-only semantic — second emit doesn't overwrite first."""
        from services.smart.sidecar_emitter import (
            emit_smart_decision, smart_decisions_path,
        )

        for i, dt in enumerate(("speaker_gate", "voice_clone", "translation_auto_approve")):
            emit_smart_decision(
                tmp_path,
                decision_type=dt,
                decision="approved",
                smart_decision_id=f"dec_{i:03d}",
                created_at=f"2026-05-14T12:0{i}:00Z",
            )
        with open(smart_decisions_path(tmp_path), encoding="utf-8") as fp:
            records = [json.loads(line) for line in fp]
        assert [r["decision_type"] for r in records] == [
            "speaker_gate", "voice_clone", "translation_auto_approve"
        ]

    def test_emit_smart_decision_unknown_decision_type_raises(self, tmp_path):
        from services.smart.sidecar_emitter import emit_smart_decision

        with pytest.raises(ValueError, match="unknown decision_type"):
            emit_smart_decision(
                tmp_path,
                decision_type="not_a_real_type",
                decision="approved",
                smart_decision_id="dec_x",
                created_at="2026-05-14T12:00:00Z",
            )

    def test_emit_smart_decision_unknown_decision_value_raises(self, tmp_path):
        from services.smart.sidecar_emitter import emit_smart_decision

        with pytest.raises(ValueError, match="unknown decision"):
            emit_smart_decision(
                tmp_path,
                decision_type="speaker_gate",
                decision="maybe",
                smart_decision_id="dec_x",
                created_at="2026-05-14T12:00:00Z",
            )

    def test_emit_smart_decision_empty_id_raises(self, tmp_path):
        from services.smart.sidecar_emitter import emit_smart_decision

        with pytest.raises(ValueError, match="smart_decision_id"):
            emit_smart_decision(
                tmp_path,
                decision_type="speaker_gate",
                decision="approved",
                smart_decision_id="",
                created_at="2026-05-14T12:00:00Z",
            )

    def test_emit_smart_decision_extra_fields_appended_at_top_level(self, tmp_path):
        from services.smart.sidecar_emitter import (
            emit_smart_decision, smart_decisions_path,
        )

        emit_smart_decision(
            tmp_path,
            decision_type="voice_clone",
            decision="rejected",
            reason_code="quota_low",
            smart_decision_id="dec_e",
            created_at="2026-05-14T12:00:00Z",
            extra={"speaker_id": "speaker_a", "retry_count": 1},
        )
        with open(smart_decisions_path(tmp_path), encoding="utf-8") as fp:
            record = json.loads(fp.read())
        assert record["speaker_id"] == "speaker_a"
        assert record["retry_count"] == 1
        # extra cannot clobber required fields.
        assert record["decision_type"] == "voice_clone"

    def test_emit_smart_decision_extra_does_not_clobber_required_fields(self, tmp_path):
        from services.smart.sidecar_emitter import (
            emit_smart_decision, smart_decisions_path,
        )

        emit_smart_decision(
            tmp_path,
            decision_type="speaker_gate",
            decision="approved",
            smart_decision_id="dec_g",
            created_at="2026-05-14T12:00:00Z",
            # Caller maliciously tries to overwrite decision_type via extra.
            extra={"decision_type": "evil", "schema_version": 999},
        )
        with open(smart_decisions_path(tmp_path), encoding="utf-8") as fp:
            record = json.loads(fp.read())
        assert record["decision_type"] == "speaker_gate"
        assert record["schema_version"] == 1

    def test_emit_smart_decision_io_failure_returns_false(self, tmp_path, monkeypatch):
        """Plan §6.4 末段: I/O failure logs exception + returns False
        rather than raising. Caller emits JobEvent WARNING."""
        from services.smart import sidecar_emitter

        def explode(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", explode)
        ok = sidecar_emitter.emit_smart_decision(
            tmp_path,
            decision_type="speaker_gate",
            decision="approved",
            smart_decision_id="dec_io",
            created_at="2026-05-14T12:00:00Z",
        )
        assert ok is False

    def test_write_smart_quality_report_atomic(self, tmp_path):
        from services.smart.sidecar_emitter import (
            write_smart_quality_report, smart_quality_report_path,
        )

        payload = {
            "main_speaker_count": 2,
            "preset_downgrade_segment_ratio": 0.15,
        }
        ok = write_smart_quality_report(tmp_path, payload)
        assert ok is True
        path = smart_quality_report_path(tmp_path)
        assert path.exists()
        # No leftover .tmp file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        assert data["schema_version"] == 1
        assert data["main_speaker_count"] == 2
        assert data["preset_downgrade_segment_ratio"] == 0.15

    def test_write_smart_cost_summary_atomic(self, tmp_path):
        from services.smart.sidecar_emitter import (
            write_smart_cost_summary, smart_cost_summary_path,
        )

        payload = {
            "llm_input_tokens": 1500,
            "tts_chars_total": 5000,
            "internal_cost_usd_estimate": 0.42,
        }
        ok = write_smart_cost_summary(tmp_path, payload)
        assert ok is True
        path = smart_cost_summary_path(tmp_path)
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        assert data["schema_version"] == 1
        assert data["llm_input_tokens"] == 1500

    def test_atomic_write_io_failure_returns_false(self, tmp_path, monkeypatch):
        from services.smart import sidecar_emitter

        def explode(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", explode)
        ok = sidecar_emitter.write_smart_quality_report(
            tmp_path, {"main_speaker_count": 1}
        )
        assert ok is False

    def test_audit_subdir_created_on_first_emit(self, tmp_path):
        """The audit/ subdir doesn't have to pre-exist."""
        from services.smart.sidecar_emitter import emit_smart_decision

        assert not (tmp_path / "audit").exists()
        emit_smart_decision(
            tmp_path,
            decision_type="speaker_gate",
            decision="approved",
            smart_decision_id="dec_audit",
            created_at="2026-05-14T12:00:00Z",
        )
        assert (tmp_path / "audit").is_dir()

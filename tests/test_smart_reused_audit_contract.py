"""Audit contract for REUSED voice decisions — Task #27 (prereq for #26).

Codex 2026-05-24 (third review) acknowledged: the on-disk
smart_decisions.jsonl currently cannot distinguish the three
REUSED tiers because ``_apply_smart_reused_voice_decision`` hardcodes
``reason_code="reused_user_voice"`` and drops Phase 5 metrics
(``auto_reused_from_possible_match`` / ``possible_match_count`` /
``top_candidate_*``).

This test set pins the post-fix audit contract:

  Tier                       reason_code on disk                            evidence keys
  ─────────────────────────  ──────────────────────────────────────────────  ──────────────────────────────
  Strong same-source REUSED  "reused_user_voice"                            match_confidence="strong", ...
  Strong_named REUSED        "reused_user_voice"                            match_confidence="strong_named", ...
  Phase 5 possible auto      "possible_user_voice_match_auto_reused"        auto_reused_from_possible_match=True,
                                                                            possible_match_count=N,
                                                                            top_candidate_confidence="..."

Task #26 UI/aggregation depends on these distinctions being readable
from the disk. Fix lands BEFORE Task #26 implementation.

Also pins the symmetric fix in ``smart_quality_report.json``
(voice_decisions list) so the two audit files don't diverge.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_reused_decision(
    *,
    speaker_id: str = "speaker_a",
    voice_id: str = "vt_xyz",
    reason_code: str = "reused_user_voice",
    metrics: dict | None = None,
):
    """Build a VoiceReviewDecision in REUSED shape."""
    from services.smart.auto_voice_review import (
        VoiceReviewDecision, VoiceReviewChoice,
    )
    return VoiceReviewDecision(
        speaker_id=speaker_id,
        speaker_name=speaker_id,
        choice=VoiceReviewChoice.REUSED,
        cloned_voice_id=voice_id,
        cloned_provider_name="minimax_voice_clone",
        cloned_model_name="minimax_tts",
        reason_code=reason_code,
        smart_decision_id=f"dec_{uuid.uuid4().hex[:8]}",
        metrics=metrics or {},
    )


def _read_smart_decisions(project_dir: Path) -> list[dict]:
    """Parse audit/smart_decisions.jsonl into list of records."""
    path = project_dir / "audit" / "smart_decisions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _fake_usage_meter():
    m = MagicMock()
    m.record_voice_reuse = MagicMock()
    return m


# ─────────────────────────────────────────────────────────────────────
# 1. reason_code MUST NOT be hardcoded
# ─────────────────────────────────────────────────────────────────────


class TestReasonCodePassthrough:
    """reason_code on disk must equal decision.reason_code, not a
    hardcoded "reused_user_voice"."""

    def test_strong_same_source_reused_emits_reused_user_voice(self, tmp_path):
        """Baseline path — strong same-source REUSED keeps reason_code
        = "reused_user_voice"."""
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="reused_user_voice",
            metrics={
                "match_confidence": "strong",
                "match_reason": "same_source_content_hash_and_speaker_id",
                "matched_user_voice_id": "7",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="job_test1",
            user_id="user_test1",
        )

        records = _read_smart_decisions(tmp_path)
        assert len(records) == 1
        assert records[0]["reason_code"] == "reused_user_voice"
        assert records[0]["evidence"]["match_confidence"] == "strong"

    def test_strong_named_reused_emits_reused_user_voice_with_strong_named_confidence(self, tmp_path):
        """strong_named is still in the 'reused_user_voice' reason
        bucket — distinguished by evidence.match_confidence."""
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="reused_user_voice",
            metrics={
                "match_confidence": "strong_named",
                "match_reason": "cross_source_unique_specific_name",
                "matched_user_voice_id": "42",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="job_test2",
            user_id="user_test2",
        )

        records = _read_smart_decisions(tmp_path)
        assert records[0]["reason_code"] == "reused_user_voice"
        assert records[0]["evidence"]["match_confidence"] == "strong_named"

    def test_possible_auto_reused_emits_distinct_reason_code(self, tmp_path):
        """THE BUG codex flagged — Phase 5 auto-reuse decisions had
        reason_code overwritten to "reused_user_voice" on disk.

        Post-fix: reason_code on disk = decision.reason_code =
        "possible_user_voice_match_auto_reused"."""
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={
                "auto_reused_from_possible_match": True,
                "possible_match_count": 2,
                "top_candidate_voice_id": "vt_top",
                "top_candidate_label": "Matt Abrahams (其他视频)",
                "top_candidate_match_scope": "cross_source_named",
                "top_candidate_confidence": "weak",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="job_test3",
            user_id="user_test3",
        )

        records = _read_smart_decisions(tmp_path)
        assert records[0]["reason_code"] == "possible_user_voice_match_auto_reused", (
            "Phase 5 auto-reuse decisions MUST land on disk with their "
            "own reason_code so Task #26 indicator buckets can be split. "
            "If this fails, _apply_smart_reused_voice_decision is still "
            "hardcoding 'reused_user_voice'."
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Phase 5 evidence fields must be preserved
# ─────────────────────────────────────────────────────────────────────


class TestEvidencePreservesPhase5Fields:
    def test_evidence_includes_auto_reused_from_possible_match_flag(self, tmp_path):
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={
                "auto_reused_from_possible_match": True,
                "possible_match_count": 3,
                "top_candidate_voice_id": "vt_top",
                "top_candidate_confidence": "weak",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        assert rec["evidence"]["auto_reused_from_possible_match"] is True, (
            f"evidence keys: {list(rec['evidence'].keys())}"
        )

    def test_evidence_includes_possible_match_count(self, tmp_path):
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={
                "auto_reused_from_possible_match": True,
                "possible_match_count": 7,
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        assert rec["evidence"]["possible_match_count"] == 7

    def test_evidence_includes_top_candidate_confidence(self, tmp_path):
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={
                "auto_reused_from_possible_match": True,
                "top_candidate_confidence": "weak",
                "top_candidate_match_scope": "cross_source_named",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        assert rec["evidence"]["top_candidate_confidence"] == "weak"
        assert rec["evidence"]["top_candidate_match_scope"] == "cross_source_named"

    def test_strong_path_does_not_inject_phase5_fields(self, tmp_path):
        """Defensive: strong/strong_named decisions don't have Phase 5
        metrics, so evidence MUST NOT carry None values for those
        Phase 5 keys (would pollute analytics — anything not None
        could be treated as 'Phase 5 hit'). Either absent or False."""
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="reused_user_voice",
            metrics={
                "match_confidence": "strong",
                "match_reason": "same_source",
                "matched_user_voice_id": "1",
            },
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        # auto_reused_from_possible_match must be absent OR explicitly False
        flag = rec["evidence"].get("auto_reused_from_possible_match", False)
        assert flag is False, (
            f"strong decision must not look like Phase 5; got "
            f"auto_reused_from_possible_match={flag!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# 3. extra carries voice_reuse_reason_code (codex's specific ask)
# ─────────────────────────────────────────────────────────────────────


class TestExtraCarriesReasonCode:
    """codex: extra.voice_clone_decision was hardcoded "reused_user_voice".
    Add voice_reuse_reason_code = decision.reason_code so future
    readers of extra don't re-encounter the drift.

    Note: sidecar_emitter flattens ``extra`` fields onto the top-level
    record, so look at ``rec.get("voice_reuse_reason_code")`` directly,
    not ``rec["extra"]["voice_reuse_reason_code"]``."""

    def test_extra_has_voice_reuse_reason_code_for_possible_auto(self, tmp_path):
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={"auto_reused_from_possible_match": True},
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        assert rec.get("voice_reuse_reason_code") == (
            "possible_user_voice_match_auto_reused"
        ), (
            f"top-level voice_reuse_reason_code (via extra) must = "
            f"decision.reason_code so future readers don't re-encounter "
            f"the drift. Got: {rec.get('voice_reuse_reason_code')!r}; "
            f"record keys: {sorted(rec.keys())}"
        )

    def test_extra_has_voice_reuse_reason_code_for_strong(self, tmp_path):
        from pipeline.process import _apply_smart_reused_voice_decision

        decision = _make_reused_decision(
            reason_code="reused_user_voice",
            metrics={"match_confidence": "strong"},
        )
        _apply_smart_reused_voice_decision(
            speaker_entry={},
            decision=decision,
            usage_meter=_fake_usage_meter(),
            project_dir=tmp_path,
            job_id="j",
            user_id="u",
        )
        rec = _read_smart_decisions(tmp_path)[0]
        assert rec.get("voice_reuse_reason_code") == "reused_user_voice"


# ─────────────────────────────────────────────────────────────────────
# 4. quality_report.voice_decisions builder contract (codex 三轮 follow-up)
# ─────────────────────────────────────────────────────────────────────
# Symmetric to the smart_decisions.jsonl tests above — pins that
# smart_quality_report.json's voice_decisions list also carries
# reason_code + Phase 5 fields, so the two audit files don't diverge.
# Tests target the extracted helper _build_quality_report_voice_decisions.


def _make_cloned_decision(*, speaker_id="speaker_a", voice_id="vt_new"):
    from services.smart.auto_voice_review import (
        VoiceReviewDecision, VoiceReviewChoice,
    )
    return VoiceReviewDecision(
        speaker_id=speaker_id,
        speaker_name=speaker_id,
        choice=VoiceReviewChoice.CLONED,
        cloned_voice_id=voice_id,
        cloned_provider_name="minimax_voice_clone",
        cloned_model_name="minimax_tts",
        reason_code="clone_succeeded",
        smart_decision_id=f"dec_{uuid.uuid4().hex[:8]}",
        metrics={},
    )


def _make_preset_decision(*, speaker_id="speaker_a", reason="sample_too_short"):
    from services.smart.auto_voice_review import (
        VoiceReviewDecision, VoiceReviewChoice,
    )
    return VoiceReviewDecision(
        speaker_id=speaker_id,
        speaker_name=speaker_id,
        choice=VoiceReviewChoice.PRESET,
        cloned_voice_id=None,
        cloned_provider_name=None,
        cloned_model_name=None,
        reason_code=reason,
        smart_decision_id=f"dec_{uuid.uuid4().hex[:8]}",
        metrics={},
    )


class TestQualityReportVoiceDecisionsBuilder:
    """Pin _build_quality_report_voice_decisions output schema —
    smart_quality_report.json voice_decisions must carry reason_code
    + Phase 5 fields so analytics doesn't have to read 2 audit files
    with different shapes (the bug Task #27 fixes)."""

    def test_strong_entry_has_reason_code_and_no_phase5_fields(self):
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [_make_reused_decision(
            reason_code="reused_user_voice",
            metrics={
                "match_confidence": "strong",
                "match_reason": "same_source_content_hash_and_speaker_id",
                "matched_user_voice_id": "7",
            },
        )]
        out = _build_quality_report_voice_decisions(decisions, {"speaker_a": 25.0})

        assert len(out) == 1
        entry = out[0]
        assert entry["choice"] == "reused_user_voice"
        assert entry["reason_code"] == "reused_user_voice", (
            "REUSED entry MUST carry reason_code so smart_quality_report.json "
            "and smart_decisions.jsonl agree on the tier signal."
        )
        assert entry["match_confidence"] == "strong"
        assert entry["sample_seconds"] == 25.0
        # Phase 5 fields MUST be absent (not present-but-None) on strong path.
        assert "auto_reused_from_possible_match" not in entry, (
            f"strong-path entry must NOT carry Phase 5 fields; "
            f"keys: {sorted(entry.keys())}"
        )
        assert "possible_match_count" not in entry
        assert "top_candidate_confidence" not in entry

    def test_strong_named_entry_keeps_distinct_confidence(self):
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [_make_reused_decision(
            reason_code="reused_user_voice",
            metrics={
                "match_confidence": "strong_named",
                "match_reason": "cross_source_unique_specific_name",
                "matched_user_voice_id": "42",
            },
        )]
        out = _build_quality_report_voice_decisions(decisions, None)

        entry = out[0]
        assert entry["reason_code"] == "reused_user_voice"
        assert entry["match_confidence"] == "strong_named", (
            "strong_named is distinguished from strong via match_confidence. "
            "Task #26 analytics buckets depend on this field."
        )
        assert "auto_reused_from_possible_match" not in entry

    def test_possible_auto_entry_carries_phase5_fields(self):
        """THE codex ask: post_edit/Phase 5 audit must include enough
        signal that Task #26 can build a possible_auto bucket."""
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [_make_reused_decision(
            reason_code="possible_user_voice_match_auto_reused",
            metrics={
                "auto_reused_from_possible_match": True,
                "possible_match_count": 3,
                "top_candidate_voice_id": "vt_top",
                "top_candidate_label": "Matt Abrahams (其他视频)",
                "top_candidate_match_scope": "cross_source_named",
                "top_candidate_confidence": "weak",
            },
        )]
        out = _build_quality_report_voice_decisions(decisions, None)

        entry = out[0]
        assert entry["reason_code"] == "possible_user_voice_match_auto_reused", (
            "Phase 5 entries MUST keep their distinct reason_code so "
            "smart_quality_report.json doesn't lose the signal that "
            "smart_decisions.jsonl already preserves."
        )
        assert entry["auto_reused_from_possible_match"] is True
        assert entry["possible_match_count"] == 3
        assert entry["top_candidate_confidence"] == "weak"
        assert entry["top_candidate_match_scope"] == "cross_source_named"

    def test_cloned_entry_has_no_reason_code_field(self):
        """CLONED (new clone) entries don't go through the REUSED
        branch — they don't need reason_code in voice_decisions
        because CLONED choice IS the signal."""
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [_make_cloned_decision()]
        out = _build_quality_report_voice_decisions(decisions, None)

        entry = out[0]
        assert entry["choice"] == "cloned"
        # CLONED branch doesn't inject reason_code (back-compat with
        # pre-Task-#27 callers; analytics treats CLONED as its own
        # bucket and doesn't need a reason_code disambiguator).
        assert "reason_code" not in entry
        assert "auto_reused_from_possible_match" not in entry

    def test_preset_entry_carries_fallback_reason(self):
        """PRESET entries (clone refused / sample insufficient) carry
        the WHY in fallback_reason — pre-existing contract; pin so it
        doesn't regress alongside the Task #27 reason_code work."""
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [_make_preset_decision(reason="insufficient_sample_seconds_lt_10")]
        out = _build_quality_report_voice_decisions(decisions, None)

        entry = out[0]
        assert entry["choice"] == "preset"
        assert entry["fallback_reason"] == "insufficient_sample_seconds_lt_10"
        # PRESET doesn't go through REUSED branch.
        assert "reason_code" not in entry
        assert "auto_reused_from_possible_match" not in entry

    def test_mixed_decisions_each_get_correct_shape(self):
        """End-to-end: a multi-speaker job mixing all three REUSED
        tiers + CLONED + PRESET produces a list with the correct
        per-entry shape."""
        from pipeline.process import _build_quality_report_voice_decisions

        decisions = [
            _make_reused_decision(
                speaker_id="speaker_a",
                reason_code="reused_user_voice",
                metrics={"match_confidence": "strong"},
            ),
            _make_reused_decision(
                speaker_id="speaker_b",
                reason_code="reused_user_voice",
                metrics={"match_confidence": "strong_named"},
            ),
            _make_reused_decision(
                speaker_id="speaker_c",
                reason_code="possible_user_voice_match_auto_reused",
                metrics={
                    "auto_reused_from_possible_match": True,
                    "possible_match_count": 2,
                    "top_candidate_confidence": "weak",
                },
            ),
            _make_cloned_decision(speaker_id="speaker_d"),
            _make_preset_decision(speaker_id="speaker_e", reason="quota_exhausted"),
        ]
        out = _build_quality_report_voice_decisions(decisions, None)

        assert len(out) == 5
        by_speaker = {e["speaker_id"]: e for e in out}

        # All REUSED entries have reason_code; only Phase 5 has the flag.
        assert by_speaker["speaker_a"]["match_confidence"] == "strong"
        assert "auto_reused_from_possible_match" not in by_speaker["speaker_a"]

        assert by_speaker["speaker_b"]["match_confidence"] == "strong_named"
        assert "auto_reused_from_possible_match" not in by_speaker["speaker_b"]

        assert by_speaker["speaker_c"]["auto_reused_from_possible_match"] is True
        assert by_speaker["speaker_c"]["possible_match_count"] == 2

        # CLONED + PRESET don't carry REUSED-branch fields.
        assert by_speaker["speaker_d"]["choice"] == "cloned"
        assert "reason_code" not in by_speaker["speaker_d"]

        assert by_speaker["speaker_e"]["choice"] == "preset"
        assert by_speaker["speaker_e"]["fallback_reason"] == "quota_exhausted"

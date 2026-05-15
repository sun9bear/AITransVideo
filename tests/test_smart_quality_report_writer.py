"""Smart MVP P3-a — write_smart_quality_report helper tests.

Per decision log §1 (docs/plans/2026-05-15-smart-mvp-p3-decisions.md),
``smart_quality_report.json`` v1 schema is locked. process.py needs a
helper ``_emit_smart_quality_report(project_dir, *, ...)`` that
collects per-job decision data into the v1 shape and delegates to
``services.smart.sidecar_emitter.write_smart_quality_report`` for the
actual disk write.

These tests run BEFORE the helper exists (TDD red), and define the
contract: kwarg names, default behaviours, schema-conformance,
error-swallow semantics.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ===========================================================================
# Cycle 1 — happy-path full payload
# ===========================================================================


class TestQualityReportHappyPath:
    """Helper writes a full-shape v1 report when called with all
    sections populated (the post-success terminal path)."""

    def test_writes_audit_smart_quality_report_json_file(self, tmp_path):
        from pipeline.process import _emit_smart_quality_report

        project_dir = tmp_path / "project_p3a_happy"
        project_dir.mkdir()

        ok = _emit_smart_quality_report(
            project_dir,
            job_id="job_p3a_001",
            user_id="user_p3a",
            service_mode="smart",
            smart_state_final={
                "status": "completed",
                "credits_policy": "capture_full",
            },
            speaker_summary={
                "main_speaker_count": 1,
                "main_speaker_ids": ["speaker_a"],
                "excluded_speakers": [],
            },
            voice_decisions=[
                {
                    "speaker_id": "speaker_a",
                    "choice": "cloned",
                    "voice_id": "vt_speaker_a_test",
                    "clone_provider": "minimax_voice_clone",
                    "sample_seconds": 29.1,
                    "smart_decision_id": "dec_a_xxx",
                },
            ],
            translation_review={
                "auto_approved": True,
                "failed_check": None,
                "metrics": {
                    "asr_speaker_count": 1,
                    "clone_eligible_ratio": 1.0,
                    "uncertain_speaker_duration_share": 0.0,
                },
            },
            retry_summary={
                "rewrite_attempts_used": 0,
                "retts_attempts_used": 0,
                "budget_remaining_minutes": 12.3,
            },
            handoff_history=[],
        )

        assert ok is True, (
            "Helper must return True on successful write so caller can "
            "branch on outcome (parallel sidecar_emitter helpers)."
        )

        target = project_dir / "audit" / "smart_quality_report.json"
        assert target.exists(), (
            f"Quality report file not written; expected at {target}"
        )

        payload = json.loads(target.read_text(encoding="utf-8"))

        # Schema version (locked by sidecar_emitter, not caller)
        assert payload["schema_version"] == 1

        # Identity fields
        assert payload["job_id"] == "job_p3a_001"
        assert payload["user_id"] == "user_p3a"
        assert payload["service_mode"] == "smart"

        # Smart state final
        assert payload["smart_state_final"]["status"] == "completed"
        assert payload["smart_state_final"]["credits_policy"] == (
            "capture_full"
        )

        # Speaker summary
        assert payload["speaker_summary"]["main_speaker_count"] == 1
        assert payload["speaker_summary"]["main_speaker_ids"] == ["speaker_a"]

        # Voice decisions list
        assert len(payload["voice_decisions"]) == 1
        vd = payload["voice_decisions"][0]
        assert vd["speaker_id"] == "speaker_a"
        assert vd["choice"] == "cloned"
        assert vd["voice_id"] == "vt_speaker_a_test"
        assert vd["smart_decision_id"] == "dec_a_xxx"  # links to JSONL event

        # Translation review
        assert payload["translation_review"]["auto_approved"] is True
        assert payload["translation_review"]["failed_check"] is None
        assert payload["translation_review"]["metrics"][
            "clone_eligible_ratio"
        ] == 1.0

        # Retry summary
        assert payload["retry_summary"]["rewrite_attempts_used"] == 0
        assert payload["retry_summary"]["retts_attempts_used"] == 0
        assert payload["retry_summary"]["budget_remaining_minutes"] == 12.3

        # Handoff history (empty for happy path)
        assert payload["handoff_history"] == []

        # generated_at is auto-stamped (not from caller)
        assert "generated_at" in payload
        assert isinstance(payload["generated_at"], str)
        assert "T" in payload["generated_at"]  # ISO 8601 shape

    def test_helper_never_raises_on_io_failure(self, tmp_path, monkeypatch):
        """Plan §6.4 末段 contract (mirrors emit_smart_decision):
        sidecar emit failure MUST NOT block the user-facing pipeline.
        Returns False instead."""
        from pipeline.process import _emit_smart_quality_report

        # Force the underlying writer to raise.
        from services.smart import sidecar_emitter

        def _boom(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            sidecar_emitter, "write_smart_quality_report", _boom
        )

        project_dir = tmp_path / "project_p3a_io_fail"
        project_dir.mkdir()

        # Should NOT raise
        ok = _emit_smart_quality_report(
            project_dir,
            job_id="job_x", user_id="user_x", service_mode="smart",
            smart_state_final={"status": "completed", "credits_policy": "capture_full"},
            speaker_summary={"main_speaker_count": 0, "main_speaker_ids": [], "excluded_speakers": []},
            voice_decisions=[],
            translation_review=None,
            retry_summary={"rewrite_attempts_used": 0, "retts_attempts_used": 0, "budget_remaining_minutes": 0.0},
            handoff_history=[],
        )
        assert ok is False, (
            "Helper must return False on writer failure so caller can "
            "log + continue, not block the user-facing pipeline."
        )


# ===========================================================================
# Cycle 2 — handoff variants + edge cases
# ===========================================================================


class TestQualityReportHandoffPath:
    """Helper writes a v1 report for each handoff variant — schema must
    accommodate non-completed terminal states."""

    def test_handoff_payload_includes_reason_and_handoff_history(self, tmp_path):
        """Smart job downgraded at voice review: handoff_history captures
        the stage + reason; smart_state_final.status reflects downgrade."""
        from pipeline.process import _emit_smart_quality_report

        project_dir = tmp_path / "project_p3a_handoff"
        project_dir.mkdir()

        ok = _emit_smart_quality_report(
            project_dir,
            job_id="job_handoff",
            user_id="user_x",
            service_mode="smart",
            smart_state_final={
                "status": "downgraded_to_studio",
                "credits_policy": "refund_full",
                "reason": "voice_library_quota_at_safety_water_mark_0_le_3",
            },
            speaker_summary={
                "main_speaker_count": 1,
                "main_speaker_ids": ["speaker_a"],
                "excluded_speakers": [],
            },
            voice_decisions=[],  # never reached cloned/preset stage
            translation_review=None,  # never ran
            retry_summary={
                "rewrite_attempts_used": 0,
                "retts_attempts_used": 0,
                "budget_remaining_minutes": 0.0,
            },
            handoff_history=[
                {
                    "stage": "voice_selection_review",
                    "reason": "voice_library_quota_at_safety_water_mark_0_le_3",
                    "occurred_at": "2026-05-15T04:07:13+00:00",
                },
            ],
        )
        assert ok is True

        payload = json.loads(
            (project_dir / "audit" / "smart_quality_report.json")
            .read_text(encoding="utf-8")
        )
        assert payload["smart_state_final"]["status"] == "downgraded_to_studio"
        assert payload["smart_state_final"]["reason"] == (
            "voice_library_quota_at_safety_water_mark_0_le_3"
        )
        assert payload["voice_decisions"] == []
        assert payload["translation_review"] is None
        assert len(payload["handoff_history"]) == 1
        assert payload["handoff_history"][0]["stage"] == (
            "voice_selection_review"
        )

    def test_eligibility_rejection_payload(self, tmp_path):
        """Eligibility failure → handoff before voice/translation review;
        speaker_summary still populated (excluded_speakers carries the
        diagnostics), voice_decisions/translation_review absent."""
        from pipeline.process import _emit_smart_quality_report

        project_dir = tmp_path / "project_p3a_elig_reject"
        project_dir.mkdir()

        ok = _emit_smart_quality_report(
            project_dir,
            job_id="job_elig",
            user_id="user_x",
            service_mode="smart",
            smart_state_final={
                "status": "downgraded_to_studio",
                "credits_policy": "refund_full",
                "reason": "main_speaker_count_exceeded",
            },
            speaker_summary={
                "main_speaker_count": 4,
                "main_speaker_ids": [
                    "speaker_a", "speaker_b", "speaker_c", "speaker_d",
                ],
                "excluded_speakers": [],
            },
            voice_decisions=[],
            translation_review=None,
            retry_summary={
                "rewrite_attempts_used": 0, "retts_attempts_used": 0,
                "budget_remaining_minutes": 0.0,
            },
            handoff_history=[
                {
                    "stage": "voice_selection_review",
                    "reason": "main_speaker_count_exceeded",
                    "occurred_at": "2026-05-15T05:00:00+00:00",
                },
            ],
        )
        assert ok is True
        payload = json.loads(
            (project_dir / "audit" / "smart_quality_report.json")
            .read_text(encoding="utf-8")
        )
        assert payload["speaker_summary"]["main_speaker_count"] == 4
        assert (
            payload["smart_state_final"]["reason"]
            == "main_speaker_count_exceeded"
        )

    def test_mixed_voice_decisions_cloned_and_preset(self, tmp_path):
        """Multi-speaker job with mixed cloned + preset outcomes."""
        from pipeline.process import _emit_smart_quality_report

        project_dir = tmp_path / "project_p3a_mixed"
        project_dir.mkdir()

        ok = _emit_smart_quality_report(
            project_dir,
            job_id="job_mixed",
            user_id="user_x",
            service_mode="smart",
            smart_state_final={
                "status": "completed", "credits_policy": "capture_full",
            },
            speaker_summary={
                "main_speaker_count": 2,
                "main_speaker_ids": ["speaker_a", "speaker_b"],
                "excluded_speakers": [],
            },
            voice_decisions=[
                {
                    "speaker_id": "speaker_a", "choice": "cloned",
                    "voice_id": "vt_a", "clone_provider": "minimax_voice_clone",
                    "sample_seconds": 25.0, "smart_decision_id": "dec_a",
                },
                {
                    "speaker_id": "speaker_b", "choice": "preset",
                    "voice_id": "preset_x", "clone_provider": None,
                    "sample_seconds": 8.0,  # below 10s, fell to preset
                    "smart_decision_id": "dec_b",
                    "fallback_reason": "insufficient_sample_seconds_lt_10",
                },
            ],
            translation_review={
                "auto_approved": True, "failed_check": None,
                "metrics": {},
            },
            retry_summary={
                "rewrite_attempts_used": 0, "retts_attempts_used": 0,
                "budget_remaining_minutes": 12.0,
            },
            handoff_history=[],
        )
        assert ok is True
        payload = json.loads(
            (project_dir / "audit" / "smart_quality_report.json")
            .read_text(encoding="utf-8")
        )
        choices = [d["choice"] for d in payload["voice_decisions"]]
        assert choices == ["cloned", "preset"]
        # Preset entry preserves fallback_reason audit field
        preset = [d for d in payload["voice_decisions"] if d["choice"] == "preset"][0]
        assert preset["fallback_reason"] == "insufficient_sample_seconds_lt_10"


# ===========================================================================
# Cycle 3 — wiring anchors (terminal + handoff call sites)
# ===========================================================================


_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestQualityReportTerminalWiring:
    """Each happy-path call to ``_emit_smart_terminal_completion_marker``
    must be paired with a ``_emit_smart_quality_report`` call at the
    same site (so every successfully-completed smart job leaves a
    quality_report on disk).

    The terminal-marker helper itself only takes ``service_mode`` —
    it doesn't have access to project_dir + decision data. So the
    quality_report emit lives at the CALLER's site (where local
    state is in scope), not inside the helper.
    """

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_main_run_terminal_marker_call_is_paired_with_quality_report(self):
        """The MAIN-RUN happy-path terminal must emit quality_report —
        this is the first time smart decision data is fully populated,
        and the only place audit can be authoritatively recorded.

        The RESUME-publish-only call site (commit copy_as_new /
        overwrite) intentionally does NOT re-emit quality_report:
          - Resume path has no fresh smart decision data in scope
            (no _smart_eligibility / _smart_voice_review etc.; the
            original run already audited)
          - Re-emitting would clobber the original audit with empty
            sections from a user-edited re-publish

        The terminal-marker emission DOES happen on resume (it
        re-flips status → completed for the editing/jianying gates),
        but that's state, not audit.
        """
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("
        # Module-level helper (parallels _emit_smart_audit pattern),
        # NOT a ProcessPipeline method — call without self.
        qr_call = "_emit_smart_quality_report("

        idx = 0
        marker_sites = []
        while True:
            idx = source.find(marker_call, idx)
            if idx < 0:
                break
            marker_sites.append(idx)
            idx += 1

        assert len(marker_sites) >= 2, (
            f"Expected ≥2 terminal-marker call sites; got "
            f"{len(marker_sites)}. (b3a established main-run + "
            f"resume-publish call sites.)"
        )

        # Site 1 = main run (first occurrence) — MUST be paired with
        # quality_report emit within next ~150 lines (5000 chars).
        # The payload-building logic between marker call and
        # quality_report call is intentionally inline (collects from
        # locals().get for safe access on requires_review=False
        # smart jobs), so the lookahead has to span it.
        first_site = marker_sites[0]
        first_lookahead = source[first_site : first_site + 5000]
        assert qr_call in first_lookahead, (
            "Main-run terminal-marker call site is NOT paired with a "
            "_emit_smart_quality_report call within 150 lines. "
            "PR#3C-P3-a contract: every successful smart job's main "
            "run must emit the quality report.\n"
            f"Main-run site lookahead (first 5000 chars):\n"
            f"{first_lookahead}"
        )

        # Subsequent sites = resume publish-only — MUST NOT emit
        # quality_report (would clobber original audit). Pin the
        # absence so a future PR doesn't accidentally add it.
        for site in marker_sites[1:]:
            lookahead = source[site : site + 5000]
            assert qr_call not in lookahead, (
                "Resume-publish-only terminal-marker site has a "
                "_emit_smart_quality_report call within 30 lines. "
                "Resume paths must NOT re-emit quality_report — the "
                "original run already wrote it; resume re-emit would "
                "clobber the audit with empty sections (resume has no "
                "smart decision locals in scope). If you need to write "
                "a separate post-edit audit artifact, use a different "
                "filename + sidecar helper."
            )

    def test_terminal_marker_helper_signature_unchanged(self):
        """Defensive: the terminal-marker helper's signature stays
        ``(self, *, service_mode: str | None)``. PR#3C-P3-a chose to
        wire quality_report at CALLER sites rather than refactor the
        helper to accept project_dir + decision data, because that
        would have broken existing
        ``test_terminal_marker_helper_emits_completed_for_smart`` etc.
        in test_smart_studio_gate_acceptance.py.

        If a future PR refactors the helper to take more args, update
        the existing tests AND remove this guard.
        """
        import inspect

        from pipeline.process import ProcessPipeline

        sig = inspect.signature(
            ProcessPipeline._emit_smart_terminal_completion_marker
        )
        params = list(sig.parameters.keys())
        # self + service_mode (kw-only)
        assert params == ["self", "service_mode"], (
            f"Terminal-marker helper signature changed: {params!r}. "
            f"Either revert the change or update the test_smart_studio "
            f"existing tests AND remove this guard."
        )

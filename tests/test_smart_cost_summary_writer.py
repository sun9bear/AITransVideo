"""Smart MVP P3-b — write_smart_cost_summary helper tests.

Per decision log §2 (docs/plans/2026-05-15-smart-mvp-p3-decisions.md),
``smart_cost_summary.json`` v1 schema is locked + this file is the
admin-only counterpart to quality_report. process.py needs a helper
``_emit_smart_cost_summary(project_dir, *, ...)`` mirroring the
P3-a quality_report helper shape.

These tests run BEFORE the helper exists (TDD red).

Like P3-a, the helper:
  - Module-level (NOT a method) — parallels _emit_smart_audit /
    _emit_smart_quality_report pattern
  - Delegates to services.smart.sidecar_emitter.write_smart_cost_summary
    (which already exists from PR#3A, stamps schema_version=1)
  - Returns bool (True on success, False on any failure)
  - Never raises (plan §6.4 末段)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ===========================================================================
# Cycle 1 — helper writes full-shape v1 payload
# ===========================================================================


class TestCostSummaryWriterHappyPath:
    """Helper writes a v1 cost summary with all sections populated."""

    def test_writes_audit_smart_cost_summary_json(self, tmp_path):
        from pipeline.process import _emit_smart_cost_summary

        project_dir = tmp_path / "project_p3b_happy"
        project_dir.mkdir()

        ok = _emit_smart_cost_summary(
            project_dir,
            job_id="job_p3b_001",
            service_mode="smart",
            minutes_processed=12.5,
            credits_charged=1250,
            credits_policy="capture_full",
            asr_seconds=45.2,
            llm_translation_chars=5234,
            tts_chars=8120,
            voice_clone_calls=1,
            minimax_quota_used_after=1,
        )
        assert ok is True

        target = project_dir / "audit" / "smart_cost_summary.json"
        assert target.exists(), f"cost_summary file not written; expected {target}"

        payload = json.loads(target.read_text(encoding="utf-8"))

        # Schema version stamped by sidecar_emitter
        assert payload["schema_version"] == 1

        # Top-level identity
        assert payload["job_id"] == "job_p3b_001"
        assert payload["service_mode"] == "smart"

        # Top-level cost facts
        assert payload["minutes_processed"] == 12.5
        assert payload["credits_charged"] == 1250
        assert payload["credits_policy"] == "capture_full"

        # Internal-only breakdown (admin-only display per Codex Q2)
        breakdown = payload["cost_breakdown_internal_only"]
        assert breakdown["asr_seconds"] == 45.2
        assert breakdown["llm_translation_chars"] == 5234
        assert breakdown["tts_chars"] == 8120
        assert breakdown["voice_clone_calls"] == 1
        assert breakdown["minimax_quota_used_after"] == 1

        # Auto-stamped generated_at
        assert "generated_at" in payload
        assert isinstance(payload["generated_at"], str)
        assert "T" in payload["generated_at"]

    def test_helper_never_raises_on_io_failure(self, tmp_path, monkeypatch):
        """Plan §6.4 末段: emit failure must NOT block pipeline."""
        from pipeline.process import _emit_smart_cost_summary
        from services.smart import sidecar_emitter

        def _boom(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sidecar_emitter, "write_smart_cost_summary", _boom)

        project_dir = tmp_path / "project_p3b_io"
        project_dir.mkdir()

        ok = _emit_smart_cost_summary(
            project_dir,
            job_id="job_x", service_mode="smart",
            minutes_processed=0.0, credits_charged=0,
            credits_policy="capture_full",
            asr_seconds=0.0, llm_translation_chars=0, tts_chars=0,
            voice_clone_calls=0, minimax_quota_used_after=0,
        )
        assert ok is False

    def test_helper_accepts_none_for_settle_dependent_fields(self, tmp_path):
        """``credits_charged`` + ``minimax_quota_used_after`` are
        determined by Gateway AFTER pipeline terminal (settle_job_credit_ledger
        + /user-voices/quota lookup). Pipeline writes None for these
        and Gateway updates the file post-settle (P3-b follow-up) OR
        the renderer handles None gracefully (current P3-b scope).
        """
        from pipeline.process import _emit_smart_cost_summary

        project_dir = tmp_path / "project_p3b_none"
        project_dir.mkdir()

        ok = _emit_smart_cost_summary(
            project_dir,
            job_id="job_x", service_mode="smart",
            minutes_processed=12.5,
            credits_charged=None,  # settled later by Gateway
            credits_policy="capture_full",
            asr_seconds=45.2, llm_translation_chars=5234, tts_chars=8120,
            voice_clone_calls=1,
            minimax_quota_used_after=None,  # queried later by Gateway
        )
        assert ok is True

        payload = json.loads(
            (project_dir / "audit" / "smart_cost_summary.json")
            .read_text(encoding="utf-8")
        )
        assert payload["credits_charged"] is None
        assert (
            payload["cost_breakdown_internal_only"]["minimax_quota_used_after"]
            is None
        )


# ===========================================================================
# Cycle 2 — wiring at main-run terminal (paired with quality_report,
# same dual-gate)
# ===========================================================================


_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestCostSummaryTerminalWiring:
    """cost_summary emission lives next to quality_report emission at
    the main-run terminal — same dual-gate (service_mode==smart AND
    effective_pipeline_mode==smart), same scope-down rationale
    (handoff/resume paths skip)."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_main_run_terminal_emits_cost_summary(self):
        """At main-run terminal, _emit_smart_cost_summary call must
        appear within the same gated block as _emit_smart_quality_report."""
        source = self._source()

        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0

        lookahead = source[site : site + 9000]

        qr_call_idx = lookahead.find("_emit_smart_quality_report(")
        cs_call_idx = lookahead.find("_emit_smart_cost_summary(")

        assert qr_call_idx >= 0, "_emit_smart_quality_report wiring missing"
        assert cs_call_idx >= 0, (
            "_emit_smart_cost_summary call not found within 6500 chars "
            "of main-run terminal marker call. PR#3C-P3-b contract: "
            "every successful smart job's main run must also emit "
            "cost_summary alongside quality_report.\n"
            f"Lookahead first 3000 chars:\n{lookahead[:3000]}"
        )

    def test_cost_summary_uses_same_dual_gate_as_quality_report(self):
        """Cost summary MUST use the same dual-gate condition as
        quality_report (Codex 第三十五轮 P1 same rationale):
        handoff-after-continue smart jobs at terminal have
        ``effective_pipeline_mode==studio``, and writing an empty
        cost_summary there would mislead admin diagnostics.
        """
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0
        lookahead = source[site : site + 9000]

        cs_call_idx = lookahead.find("_emit_smart_cost_summary(")
        assert cs_call_idx >= 0

        # All gate text must appear in the region before the call.
        gate_region = lookahead[:cs_call_idx]
        assert 'self._current_service_mode == "smart"' in gate_region
        assert (
            'job_effective_pipeline_mode == "smart"' in gate_region
            or "job_effective_pipeline_mode == 'smart'" in gate_region
        ), (
            "Cost summary call lives outside the dual-gate region — "
            "handoff-after-continue smart jobs would write empty "
            "cost_summary. Match the quality_report gate.\n"
            f"Gate region (last 1500 chars):\n{gate_region[-1500:]}"
        )

    def test_resume_publish_only_terminal_skips_cost_summary(self):
        """Resume path skips cost_summary too — already written on the
        original main run; the resume re-write would clobber with
        empty post-edit-only cost data."""
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("

        idx = 0
        sites = []
        while True:
            idx = source.find(marker_call, idx)
            if idx < 0:
                break
            sites.append(idx)
            idx += 1

        assert len(sites) >= 2

        # Site 2 = resume publish-only. Must NOT call cost_summary.
        for site in sites[1:]:
            lookahead = source[site : site + 9000]
            assert "_emit_smart_cost_summary(" not in lookahead, (
                "Resume publish-only terminal site has a cost_summary "
                "call — would clobber the original audit. Same "
                "scope-down rationale as quality_report (decision log §P3-a)."
            )

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
            pending_credits_charged=1250,
            credits_policy="capture_full",
            asr_seconds=45.2,
            llm_translation_chars=5234,
            tts_chars=8120,
            voice_clone_calls=1,
            pending_minimax_quota_used_after=1,
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

        # Top-level cost facts. ``pending_credits_charged`` is the
        # settle-dependent field (Codex 第三十六轮 P2): pipeline writes
        # the pending value (None until Gateway runs settle_job_credit_ledger
        # post-pipeline); explicit ``pending_`` prefix prevents admin UI
        # from mis-reading None as "0 credits charged".
        assert payload["minutes_processed"] == 12.5
        assert payload["pending_credits_charged"] == 1250
        assert payload["credits_policy"] == "capture_full"

        # Internal-only breakdown (admin-only display per Codex Q2).
        # ``pending_minimax_quota_used_after`` similarly pending until
        # Gateway queries /user-voices/quota post-pipeline.
        breakdown = payload["cost_breakdown_internal_only"]
        assert breakdown["asr_seconds"] == 45.2
        assert breakdown["llm_translation_chars"] == 5234
        assert breakdown["tts_chars"] == 8120
        assert breakdown["voice_clone_calls"] == 1
        assert breakdown["pending_minimax_quota_used_after"] == 1

        # Old unprefixed keys MUST NOT appear (regression-pin to keep
        # admin UI consumers from accidentally still reading the old
        # names that look settled).
        assert "credits_charged" not in payload
        assert "minimax_quota_used_after" not in breakdown

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
            minutes_processed=0.0, pending_credits_charged=0,
            credits_policy="capture_full",
            asr_seconds=0.0, llm_translation_chars=0, tts_chars=0,
            voice_clone_calls=0, pending_minimax_quota_used_after=0,
        )
        assert ok is False

    def test_helper_accepts_none_for_settle_dependent_fields(self, tmp_path):
        """Settle-dependent fields are determined by Gateway AFTER
        pipeline terminal (settle_job_credit_ledger + /user-voices/quota
        lookup). Pipeline writes None for these. Codex 第三十六轮 P2:
        renamed to ``pending_*`` so admin UI clearly sees "this hasn't
        been settled yet" rather than mis-reading ``credits_charged=None``
        as "no credits / free job".
        """
        from pipeline.process import _emit_smart_cost_summary

        project_dir = tmp_path / "project_p3b_none"
        project_dir.mkdir()

        ok = _emit_smart_cost_summary(
            project_dir,
            job_id="job_x", service_mode="smart",
            minutes_processed=12.5,
            pending_credits_charged=None,  # settled later by Gateway
            credits_policy="capture_full",
            asr_seconds=45.2, llm_translation_chars=5234, tts_chars=8120,
            voice_clone_calls=1,
            pending_minimax_quota_used_after=None,  # queried later by Gateway
        )
        assert ok is True

        payload = json.loads(
            (project_dir / "audit" / "smart_cost_summary.json")
            .read_text(encoding="utf-8")
        )
        assert payload["pending_credits_charged"] is None
        assert (
            payload["cost_breakdown_internal_only"][
                "pending_minimax_quota_used_after"
            ]
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

        lookahead = source[site : site + 12000]

        qr_call_idx = lookahead.find("_emit_smart_quality_report(")
        cs_call_idx = lookahead.find("_emit_smart_cost_summary(")

        assert qr_call_idx >= 0, "_emit_smart_quality_report wiring missing"
        assert cs_call_idx >= 0, (
            "_emit_smart_cost_summary call not found within 12000 chars "
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
        lookahead = source[site : site + 12000]

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
            lookahead = source[site : site + 12000]
            assert "_emit_smart_cost_summary(" not in lookahead, (
                "Resume publish-only terminal site has a cost_summary "
                "call — would clobber the original audit. Same "
                "scope-down rationale as quality_report (decision log §P3-a)."
            )


# ===========================================================================
# Cycle 3 — Codex 第三十六轮 P1: cost_summary wired at smart handoff sites
# ===========================================================================


class TestCostSummaryWiringAtHandoffSites:
    """Codex 第三十六轮 P1: decision log §2 explicitly says cost_summary
    is written for *every smart job, regardless of completion, so admin
    can retrospectively diagnose handoff jobs*. The pre-fix implementation
    only wrote at happy-path terminal, leaving quota-brake / sample-
    failure / eligibility-reject / mirror-failure / translation-review
    handoff jobs without admin cost visibility (404 on the admin
    endpoint, which is exactly when admin most needs the data).

    These tests pin that every ``emit_handoff_markers`` call site has a
    cost_summary emission within the same handoff-return block — so the
    admin endpoint resolves for *all* smart jobs.
    """

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def _find_handoff_call_sites(self, source: str) -> list[int]:
        """Locate every actual ``emit_handoff_markers(`` call (not a
        comment reference). Returns char offsets in source order."""
        import re

        # Match real call sites: line starts with whitespace + the call
        # name. Skip comment lines (starting with #).
        pattern = re.compile(
            r"^(?!\s*#)\s*emit_handoff_markers\(",
            re.MULTILINE,
        )
        return [m.start() for m in pattern.finditer(source)]

    def test_every_smart_handoff_site_writes_cost_summary(self):
        """Each ``emit_handoff_markers(`` call site must have a
        ``_emit_smart_cost_summary`` reference within 3500 chars after
        — the typical handoff-return block is:

          emit_handoff_markers(...)            # mark state
          state_manager.set_stage(...)          # update job stage
          current_stage_name = None             # housekeeping
          _write_usage_summary(usage_meter)     # snapshot meter
          _emit_smart_cost_summary[_from_meter](...)  # NEW (P3-b-fix)
          return self._build_paused_result(...)  # paused return
        """
        source = self._source()
        sites = self._find_handoff_call_sites(source)
        assert len(sites) >= 5, (
            f"Expected ≥5 smart handoff sites (eligibility / sample / "
            f"quota / voice / mirror / voice-expiry / translation); "
            f"found {len(sites)}. process.py shape changed unexpectedly."
        )

        missing = []
        for i, site in enumerate(sites):
            window = source[site : site + 3500]
            if "_emit_smart_cost_summary" not in window:
                snippet = window[:600]
                missing.append((i + 1, site, snippet))

        assert not missing, (
            "Codex 第三十六轮 P1: the following smart handoff sites do "
            "NOT write cost_summary, violating decision log §2 (every "
            "smart job, regardless of completion).\n"
            + "\n\n".join(
                f"  Site #{idx} @ offset {off}:\n{snip}"
                for idx, off, snip in missing
            )
        )

    def test_handoff_cost_summary_gated_on_smart_mode(self):
        """The handoff-site cost_summary call must be reached only by
        smart code. Two ways to prove this for any given site:

        1. Inline explicit gate: ``self._current_service_mode == "smart"``
           appears between the handoff call and the cost_summary call.
           Used at the shared-branch sites (voice expiry) where both
           smart and studio reach the same paused_result.
        2. Upstream emit_handoff_markers: every ``emit_handoff_markers``
           call passes ``smart_state_update=`` (required kwarg), and the
           helper is only called from smart-only code paths. The
           cost_summary call always comes AFTER the emit_handoff_markers
           call in the same block, so ``smart_state_update=`` in the
           pre-call window proves the smart-only context.

        Studio-only sites (e.g. ``_write_usage_summary`` followed by
        ``return self._build_paused_result(...)`` without an
        ``emit_handoff_markers`` upstream — like the legacy translation
        review studio branch) are not iterated by this test (they're
        not in ``_find_handoff_call_sites``).
        """
        source = self._source()
        sites = self._find_handoff_call_sites(source)
        assert sites

        for i, site in enumerate(sites):
            window = source[site : site + 3500]
            cs_idx = window.find("_emit_smart_cost_summary")
            if cs_idx < 0:
                # Tested separately; skip here.
                continue
            pre_call = window[:cs_idx]
            # ``smart_state_update=`` is a required kwarg of every
            # emit_handoff_markers call — its presence in the pre-call
            # region (which always contains the handoff call) proves
            # the cost_summary call is on a smart-only handoff path.
            # Inline gates ``self._current_service_mode == "smart"`` are
            # also accepted (used at shared smart/studio sites).
            has_smart_gate = (
                "smart_state_update=" in pre_call
                or 'self._current_service_mode == "smart"' in pre_call
                or "self._current_service_mode == 'smart'" in pre_call
            )
            assert has_smart_gate, (
                f"Smart handoff cost_summary call at site #{i+1} (offset "
                f"{site + cs_idx}) is not provably gated to smart-only. "
                "Studio jobs would incorrectly write cost_summary.\n"
                f"Pre-call region (last 1500 chars):\n{pre_call[-1500:]}"
            )

    def test_quota_lookup_failure_is_fail_open_not_handoff(self):
        """Codex 第三十六轮 P1 originally called out quota-brake as a
        failure case that must write cost_summary. The 2026-05-20
        full-auto change (commit 7aa0abcb) deliberately *removed* the
        old fail-closed quota handoff: the voice-library quota *lookup
        failure* path no longer hands off to studio with the
        ``smart_handoff_quota_unavailable`` execution_mode /
        ``voice_library_quota_unavailable`` reason code. It now fails
        OPEN — emits a ``quota_lookup_degraded`` audit and CONTINUES the
        pipeline with an unlimited fallback quota — because lookup
        failures are almost always transient infra, and Smart mode is
        fully automatic. Its cost visibility therefore comes from the
        eventual main-run terminal cost_summary (covered by
        ``test_main_run_terminal_emits_cost_summary``), not a handoff.

        This test pins that deliberate decision and guards against an
        accidental reintroduction of a fail-closed handoff that would
        terminate WITHOUT writing cost_summary (the exact Codex
        第三十六轮 P1 / terminal-settlement-single-entry concern).

        The surviving real quota gate — MiniMax quota exhausted
        *mid-flight* (``provider_quota_exhausted_mid_flight`` in
        auto_voice_review) — routes through the generic voice_review
        PAUSED handoff, whose cost_summary emission is already covered
        by ``test_every_smart_handoff_site_writes_cost_summary``.
        """
        source = self._source()

        # The old fail-closed quota handoff marker must stay gone. If it
        # reappears, the branch MUST also write cost_summary — re-pin
        # this test against the new shape rather than reverting blindly.
        assert "smart_handoff_quota_unavailable" not in source, (
            "Old fail-closed quota handoff (smart_handoff_quota_unavailable) "
            "reintroduced into process.py. If quota-lookup failure must hand "
            "off again, that branch MUST write cost_summary (Codex 第三十六轮 "
            "P1 / feedback_terminal_state_single_entry)."
        )

        # Anchor on the fail-open continuation audit emitted when the
        # quota lookup returns None.
        degraded_anchor = source.find("quota_lookup_failed_continuing")
        assert degraded_anchor >= 0, (
            "Fail-open quota-lookup-degraded path not found "
            "(quota_lookup_failed_continuing); pipeline shape changed — "
            "re-derive the quota cost-visibility contract before editing."
        )

        # From the degraded audit, the pipeline must resume into the
        # voice-review evaluation WITHOUT an intervening handoff. A
        # handoff here would terminate the job at a point that — per the
        # original P1 concern — would need its own cost_summary write.
        resume_idx = source.find("evaluate_voice_review(", degraded_anchor)
        assert resume_idx > degraded_anchor, (
            "Could not find the evaluate_voice_review continuation after "
            "the quota-lookup-degraded audit."
        )
        between = source[degraded_anchor:resume_idx]
        assert "emit_handoff_markers(" not in between, (
            "The quota-lookup-failure path emits a handoff before resuming "
            "into voice review. It must fail OPEN and continue (2026-05-20 "
            "full-auto). A handoff here without a cost_summary write would "
            "leave a terminal-settled job with no admin cost visibility.\n"
            f"Region between degraded audit and voice review:\n{between}"
        )

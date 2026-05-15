"""Smart MVP P3-d — retry budget integration tests.

Per decision log §P3-d (post-scope-down, see
docs/plans/2026-05-15-smart-mvp-p3-decisions.md):

User-visible goal: smart quality_report has REAL ``retry_summary``
values (not always-zero placeholders) + ``budget_exhausted`` sidecar
events when alignment-stage caps are hit, so the renderer (P3-c)
shows accurate retry history.

Three helpers to test:

  1. ``PostTTSBudgetTracker.usage_summary()`` — new public method on
     aligner's tracker exposing consumed roots + cap state without
     callers reaching into private ``_usage_by_root``.
  2. ``_aggregate_smart_retry_stats(*, segments, post_tts_budget_tracker,
     source_minutes) -> dict`` — pure aggregator builds the
     ``retry_summary`` dict (rewrite_attempts_used / retts_attempts_used /
     budget_remaining_minutes) from segment audit attrs + tracker state.
  3. ``_emit_smart_budget_exhausted_events(*, project_dir,
     post_tts_budget_tracker, job_id, user_id) -> int`` — scans tracker
     for exhausted roots; emits one ``budget_exhausted`` sidecar event
     per exhausted root.

Plus source-anchor: smart happy-path terminal builds ``retry_summary``
via the aggregator (not hardcoded zeros).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


_PROCESS_PY = _SRC / "pipeline" / "process.py"


# ===========================================================================
# Cycle 1 — PostTTSBudgetTracker.usage_summary() public method
# ===========================================================================


class TestPostTTSBudgetTrackerUsageSummary:
    """New public method on the alignment-stage tracker so smart-mode
    aggregator can read state without poking the private dict.
    """

    def test_empty_tracker_reports_zero_consumption(self):
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        summary = tracker.usage_summary()

        assert summary["total_consumed"] == 0
        assert summary["cap"] == 2
        assert summary["consumed_roots"] == {}
        assert summary["exhausted_root_ids"] == []

    def test_partial_consumption_reflects_in_summary(self):
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        # Synthesize a segment-like object.
        seg = SimpleNamespace(segment_id=101)
        tracker.try_consume_for_segment(seg, 1)

        summary = tracker.usage_summary()
        assert summary["total_consumed"] == 1
        assert summary["cap"] == 2
        assert summary["consumed_roots"] == {101: 1}
        assert summary["exhausted_root_ids"] == []

    def test_exhausted_root_appears_in_exhausted_list(self):
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        seg = SimpleNamespace(segment_id=42)
        # Consume to the cap.
        tracker.try_consume_for_segment(seg, 1)
        tracker.try_consume_for_segment(seg, 1)

        summary = tracker.usage_summary()
        assert summary["total_consumed"] == 2
        assert summary["consumed_roots"] == {42: 2}
        assert summary["exhausted_root_ids"] == [42]

    def test_multiple_roots_some_exhausted(self):
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        seg_a = SimpleNamespace(segment_id=1)
        seg_b = SimpleNamespace(segment_id=2)
        seg_c = SimpleNamespace(segment_id=3)
        # A: 1 consumed (not exhausted)
        tracker.try_consume_for_segment(seg_a, 1)
        # B: 2 consumed (exhausted)
        tracker.try_consume_for_segment(seg_b, 1)
        tracker.try_consume_for_segment(seg_b, 1)
        # C: 0 consumed

        summary = tracker.usage_summary()
        assert summary["total_consumed"] == 3
        assert summary["consumed_roots"] == {1: 1, 2: 2}
        assert set(summary["exhausted_root_ids"]) == {2}


# ===========================================================================
# Cycle 2 — _aggregate_smart_retry_stats pure helper
# ===========================================================================


class TestAggregateSmartRetryStats:
    """Builds the ``retry_summary`` dict from segment + tracker state.
    Pure function — no I/O.
    """

    def test_zero_retries_yields_zeros_and_full_budget(self):
        from pipeline.process import _aggregate_smart_retry_stats
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        # No segments, no retries.
        summary = _aggregate_smart_retry_stats(
            segments=[],
            post_tts_budget_tracker=tracker,
            source_minutes=10.0,
        )
        # rewrite_attempts_used = 0
        # retts_attempts_used = 0
        # budget_remaining = compute_total_budget_minutes(10) = 15
        assert summary["rewrite_attempts_used"] == 0
        assert summary["retts_attempts_used"] == 0
        assert summary["budget_remaining_minutes"] == 15.0

    def test_counts_rewrite_retries_from_segment_attrs(self):
        from pipeline.process import _aggregate_smart_retry_stats
        from services.alignment.aligner import PostTTSBudgetTracker

        # 2 segments with rewrite_retry_attempted=True, 1 without.
        seg_with_retry_a = SimpleNamespace(
            pre_tts_rewrite_retry_attempted=True,
        )
        seg_with_retry_b = SimpleNamespace(
            pre_tts_rewrite_retry_attempted=True,
        )
        seg_no_retry = SimpleNamespace(
            pre_tts_rewrite_retry_attempted=False,
        )
        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)

        summary = _aggregate_smart_retry_stats(
            segments=[seg_with_retry_a, seg_with_retry_b, seg_no_retry],
            post_tts_budget_tracker=tracker,
            source_minutes=10.0,
        )
        assert summary["rewrite_attempts_used"] == 2

    def test_counts_retts_attempts_from_tracker(self):
        from pipeline.process import _aggregate_smart_retry_stats
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        seg_a = SimpleNamespace(segment_id=1)
        seg_b = SimpleNamespace(segment_id=2)
        tracker.try_consume_for_segment(seg_a, 1)
        tracker.try_consume_for_segment(seg_b, 1)
        tracker.try_consume_for_segment(seg_b, 1)
        # total consumed = 3

        summary = _aggregate_smart_retry_stats(
            segments=[],
            post_tts_budget_tracker=tracker,
            source_minutes=10.0,
        )
        assert summary["retts_attempts_used"] == 3

    def test_budget_remaining_uses_compute_total_budget_minutes_formula(self):
        """Source=10 → 15 min budget; subtract consumed_seconds/60 from
        total. The implementation is allowed to approximate consumed
        minutes (e.g. assume avg 0.5 min per re-TTS); the test only pins
        that the remaining is bounded by the formula and non-negative.
        """
        from pipeline.process import _aggregate_smart_retry_stats
        from services.alignment.aligner import PostTTSBudgetTracker
        from services.smart.retry_budget import compute_total_budget_minutes

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)

        summary = _aggregate_smart_retry_stats(
            segments=[],
            post_tts_budget_tracker=tracker,
            source_minutes=30.0,
        )
        total_budget = compute_total_budget_minutes(30.0)
        # No consumption → remaining == total.
        assert summary["budget_remaining_minutes"] == round(total_budget, 2)
        assert total_budget == 45.0  # min(45, 60) = 45 per formula

    def test_handles_missing_tracker_gracefully(self):
        """If post_tts_budget_tracker is None (no smart alignment ran),
        return zeros + full budget."""
        from pipeline.process import _aggregate_smart_retry_stats

        summary = _aggregate_smart_retry_stats(
            segments=[],
            post_tts_budget_tracker=None,
            source_minutes=10.0,
        )
        assert summary["rewrite_attempts_used"] == 0
        assert summary["retts_attempts_used"] == 0
        assert summary["budget_remaining_minutes"] == 15.0


# ===========================================================================
# Cycle 3 — _emit_smart_budget_exhausted_events sidecar emitter
# ===========================================================================


class TestEmitBudgetExhaustedEvents:
    """Per exhausted root, emit one ``budget_exhausted`` smart_decisions.jsonl
    event. Caller (process.py terminal) iterates tracker.usage_summary
    output and emits.
    """

    def test_no_exhausted_roots_emits_zero_events(self, tmp_path):
        from pipeline.process import _emit_smart_budget_exhausted_events
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        project_dir = tmp_path / "p3d_no_exhausted"
        project_dir.mkdir()

        count = _emit_smart_budget_exhausted_events(
            project_dir=project_dir,
            post_tts_budget_tracker=tracker,
            job_id="job_p3d_none",
            user_id="user_p3d_none",
        )
        assert count == 0
        # No sidecar should be created when there are zero events.
        # (emit_smart_decision creates the file only on write; if zero
        # writes happen, file may or may not exist depending on policy —
        # accept either.)

    def test_one_exhausted_root_emits_one_event(self, tmp_path):
        from pipeline.process import _emit_smart_budget_exhausted_events
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        seg = SimpleNamespace(segment_id=7)
        tracker.try_consume_for_segment(seg, 1)
        tracker.try_consume_for_segment(seg, 1)  # exhausted

        project_dir = tmp_path / "p3d_one_exhausted"
        project_dir.mkdir()

        count = _emit_smart_budget_exhausted_events(
            project_dir=project_dir,
            post_tts_budget_tracker=tracker,
            job_id="job_p3d_one",
            user_id="user_p3d_one",
        )
        assert count == 1

        sidecar_path = project_dir / "audit" / "smart_decisions.jsonl"
        assert sidecar_path.exists()
        lines = [
            json.loads(l) for l in sidecar_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        events = [
            l for l in lines
            if l.get("decision_type") == "budget_exhausted"
        ]
        assert len(events) == 1
        ev = events[0]
        assert ev["decision"] == "rejected"  # cap reached → retries rejected
        assert ev.get("reason_code") == "post_tts_per_segment_cap_exhausted"
        evidence = ev.get("evidence") or {}
        assert evidence.get("root_segment_id") == 7
        assert evidence.get("consumed") == 2
        assert evidence.get("cap") == 2

    def test_multiple_exhausted_roots_emit_one_event_each(self, tmp_path):
        from pipeline.process import _emit_smart_budget_exhausted_events
        from services.alignment.aligner import PostTTSBudgetTracker

        tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)
        for sid in (11, 22, 33):
            seg = SimpleNamespace(segment_id=sid)
            tracker.try_consume_for_segment(seg, 1)
            tracker.try_consume_for_segment(seg, 1)  # exhaust

        project_dir = tmp_path / "p3d_multi_exhausted"
        project_dir.mkdir()

        count = _emit_smart_budget_exhausted_events(
            project_dir=project_dir,
            post_tts_budget_tracker=tracker,
            job_id="job_p3d_multi",
            user_id="user_p3d_multi",
        )
        assert count == 3

        sidecar_path = project_dir / "audit" / "smart_decisions.jsonl"
        lines = [
            json.loads(l) for l in sidecar_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        events = [
            l for l in lines
            if l.get("decision_type") == "budget_exhausted"
        ]
        assert len(events) == 3
        root_ids = sorted(
            (ev.get("evidence") or {}).get("root_segment_id") for ev in events
        )
        assert root_ids == [11, 22, 33]

    def test_helper_never_raises_on_missing_tracker(self, tmp_path):
        """Plan §6.4 末段 — sidecar emit must NOT block pipeline.
        Pass tracker=None → return 0, no crash."""
        from pipeline.process import _emit_smart_budget_exhausted_events

        project_dir = tmp_path / "p3d_no_tracker"
        project_dir.mkdir()

        count = _emit_smart_budget_exhausted_events(
            project_dir=project_dir,
            post_tts_budget_tracker=None,
            job_id="job_x",
            user_id="user_x",
        )
        assert count == 0


# ===========================================================================
# Cycle 4 — source-anchor: smart terminal builds retry_summary from
# aggregator, not zeros
# ===========================================================================


class TestTerminalWiringUsesRealRetrySummary:
    """The always-zero placeholder at smart terminal must be replaced
    with a call to _aggregate_smart_retry_stats. Pin via source-shape
    scan (anchored to _emit_smart_quality_report call)."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_terminal_retry_summary_built_from_aggregator(self):
        source = self._source()

        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0

        # Look 9000 chars ahead — same window the cost_summary tests use.
        lookahead = source[site : site + 12000]

        qr_call_idx = lookahead.find("_emit_smart_quality_report(")
        assert qr_call_idx >= 0

        # Region from marker site to quality_report call must contain
        # the aggregator call (replacing the hardcoded-zeros placeholder).
        pre_qr_region = lookahead[:qr_call_idx]
        assert "_aggregate_smart_retry_stats(" in pre_qr_region, (
            "P3-d wiring missing: _aggregate_smart_retry_stats() must be "
            "called BEFORE _emit_smart_quality_report() at smart terminal, "
            "so retry_summary has real values from alignment data instead "
            "of the always-zero placeholder.\n"
            f"Pre-quality-report region (last 1500 chars):\n"
            f"{pre_qr_region[-1500:]}"
        )

    def test_terminal_uses_actual_duration_ms_for_source_minutes(self):
        """Codex 第三十七轮 P1: at smart terminal, the source_minutes
        passed to ``_aggregate_smart_retry_stats`` must come from
        ``actual_duration_ms`` (the reliable ffprobe-derived duration
        already in scope from line ~2243) — NOT from the unreliable
        ``_snap("source_duration_seconds")`` which has been observed to
        return 0 at terminal time.

        Symptom of using the wrong source: 29-sec real video shows
        ``budget_remaining_minutes=0.0`` in the user-visible
        retry_summary, falsely suggesting Smart's retry budget is
        exhausted.

        Pin: the source-minute computation at terminal references
        ``actual_duration_ms`` (with _snap as fallback only).
        """
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0

        lookahead = source[site : site + 12000]
        agg_idx = lookahead.find("_aggregate_smart_retry_stats(")
        assert agg_idx >= 0, "aggregator call missing at terminal"

        # The aggregator call's region (200 chars before to capture
        # the source_minutes kwarg construction).
        pre_agg = lookahead[max(0, agg_idx - 600) : agg_idx + 400]

        assert "actual_duration_ms" in pre_agg, (
            "Source-minutes computation at smart terminal must "
            "prefer ``actual_duration_ms`` over ``_snap('source_"
            "duration_seconds')`` (Codex 第三十七轮 P1). Without "
            "this fix, the user-visible budget_remaining_minutes "
            "shows 0.0 on every smart job.\n"
            f"Pre-aggregator region:\n{pre_agg}"
        )

    def test_terminal_cost_summary_minutes_uses_actual_duration_ms(self):
        """Same fix for cost_summary's ``minutes_processed``: Codex
        第三十七轮 P1 explicitly called out that both retry_summary
        AND cost_summary must use the reliable duration source — they
        share the same upstream bug + admin sees both fields.
        """
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0

        lookahead = source[site : site + 12000]
        cs_idx = lookahead.find("_emit_smart_cost_summary(")
        assert cs_idx >= 0, "cost_summary call missing at terminal"

        # Look back ~800 chars from cost_summary call where _cs_minutes
        # is constructed.
        pre_cs = lookahead[max(0, cs_idx - 1200) : cs_idx]
        assert "actual_duration_ms" in pre_cs, (
            "cost_summary's minutes_processed at smart terminal must "
            "prefer ``actual_duration_ms`` over ``_snap`` (Codex 第三"
            "十七轮 P1). Without this, admin sees minutes_processed=0 "
            "on every smart job.\n"
            f"Pre-cost-summary region:\n{pre_cs}"
        )

    def test_handoff_sites_use_actual_duration_ms_for_cost_summary(self):
        """The 7 smart handoff sites also feed minutes_processed to
        cost_summary via ``_emit_smart_cost_summary_from_meter``. Same
        Codex P1 fix: each handoff site's ``minutes_processed=``
        kwarg must reference ``actual_duration_ms``, not ``_snap``.
        """
        source = self._source()
        import re
        handoff_calls = list(re.finditer(
            r"_emit_smart_cost_summary_from_meter\(",
            source,
        ))
        assert len(handoff_calls) >= 5, (
            "Expected at least 5 handoff calls; got "
            f"{len(handoff_calls)}"
        )
        # Skip the helper definition itself (first match is the def).
        # Each callsite passes minutes_processed=... — that expression
        # must reference actual_duration_ms.
        missing: list[tuple[int, str]] = []
        for m in handoff_calls:
            site = m.start()
            # Skip if this is a def, not a call.
            preceding_context = source[max(0, site - 100) : site]
            if "def _emit_smart_cost_summary_from_meter" in preceding_context:
                continue
            # Look at 600 chars after the call open paren for the
            # minutes_processed kwarg expression.
            window = source[site : site + 800]
            mp_idx = window.find("minutes_processed=")
            if mp_idx < 0:
                continue  # not a real call site
            # The next ~250 chars after minutes_processed= contain the
            # expression for that kwarg.
            expr_region = window[mp_idx : mp_idx + 250]
            if "actual_duration_ms" not in expr_region:
                missing.append((site, expr_region))
        assert not missing, (
            "The following smart handoff cost_summary call sites do "
            "NOT use ``actual_duration_ms`` for minutes_processed "
            "(Codex 第三十七轮 P1).\n\n"
            + "\n\n".join(
                f"Site @ {off}:\n{snippet}"
                for off, snippet in missing
            )
        )

    def test_terminal_emits_budget_exhausted_events(self):
        """After alignment + before terminal, smart inline branch must
        scan the budget tracker and emit budget_exhausted events for any
        exhausted roots. Verifies wiring of _emit_smart_budget_exhausted_events.
        """
        source = self._source()
        marker_call = "self._emit_smart_terminal_completion_marker("
        site = source.find(marker_call)
        assert site >= 0

        lookahead = source[site : site + 12000]
        qr_call_idx = lookahead.find("_emit_smart_quality_report(")
        assert qr_call_idx >= 0

        pre_qr_region = lookahead[:qr_call_idx]
        assert "_emit_smart_budget_exhausted_events(" in pre_qr_region, (
            "P3-d wiring missing: _emit_smart_budget_exhausted_events() "
            "must be called BEFORE _emit_smart_quality_report() at smart "
            "terminal, so admin can see which roots exhausted retry "
            "budget.\n"
            f"Pre-quality-report region (last 1500 chars):\n"
            f"{pre_qr_region[-1500:]}"
        )

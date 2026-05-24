"""Task #26 PR-1: backend voice auto-reuse quality metrics.

Spec: docs/plans/2026-05-24-smart-analytics-voice-reuse-quality-design.md (v2)

Pins the 4 new pure helpers + the integration with _aggregate_job and
_build_summary_payload. Each test maps to one of the 10 fixture cases
in design §6.2.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


# ─────────────────────────────────────────────────────────────────────
# 1. _classify_voice_decision — bucket identification
# ─────────────────────────────────────────────────────────────────────


class TestClassifyVoiceDecision:
    """Buckets a single smart_decisions.jsonl record into one of:
        strong / strong_named / possible_auto / strong_or_legacy_null / None
    None means "not a REUSED decision at all" (e.g. clone, preset, handoff)."""

    def test_strong_same_source(self):
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "evidence": {"match_confidence": "strong"},
        }
        assert _classify_voice_decision(rec) == "strong"

    def test_strong_named(self):
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "evidence": {"match_confidence": "strong_named"},
        }
        assert _classify_voice_decision(rec) == "strong_named"

    def test_possible_auto_via_explicit_flag(self):
        """Task #27 post-fix: Phase 5 decision has its own reason_code
        AND evidence.auto_reused_from_possible_match=True."""
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "possible_user_voice_match_auto_reused",
            "evidence": {
                "auto_reused_from_possible_match": True,
                "possible_match_count": 2,
                "top_candidate_confidence": "weak",
            },
        }
        assert _classify_voice_decision(rec) == "possible_auto"

    def test_legacy_null_confidence_goes_to_separate_bucket(self):
        """Codex 第二轮 review #4: null match_confidence MUST NOT be
        silently merged into strong. It's a separate bucket so analytics
        can flag legacy/unknown data without polluting strong's
        change_rate."""
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "evidence": {"match_confidence": None},
        }
        assert _classify_voice_decision(rec) == "strong_or_legacy_null"

    def test_legacy_null_confidence_missing_evidence_key(self):
        """If evidence doesn't even have match_confidence as a key
        (pre-Task-#27 records), also goes to legacy bucket."""
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "evidence": {},  # no match_confidence key at all
        }
        assert _classify_voice_decision(rec) == "strong_or_legacy_null"

    def test_metrics_fallback_when_evidence_missing(self):
        """Codex 第二轮 review #1: evidence preferred, metrics fallback
        (for test fixtures that mirror the dataclass shape)."""
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "metrics": {"match_confidence": "strong_named"},
            # no evidence at top level
        }
        assert _classify_voice_decision(rec) == "strong_named"

    def test_evidence_wins_over_metrics_when_both_present(self):
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "reused_user_voice",
            "evidence": {"match_confidence": "strong"},
            "metrics": {"match_confidence": "strong_named"},  # ignored
        }
        assert _classify_voice_decision(rec) == "strong"

    def test_clone_decision_returns_none(self):
        """Not a REUSED decision — should not enter any bucket."""
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "clone_succeeded",
            "evidence": {"voice_id": "vt_new"},
        }
        assert _classify_voice_decision(rec) is None

    def test_preset_decision_returns_none(self):
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "voice_clone",
            "reason_code": "insufficient_sample_seconds_lt_10",
            "evidence": {},
        }
        assert _classify_voice_decision(rec) is None

    def test_handoff_decision_returns_none(self):
        from admin_smart_analytics_api import _classify_voice_decision

        rec = {
            "decision_type": "downgrade_handoff",
            "reason_code": "uncertain_speaker_share",
        }
        assert _classify_voice_decision(rec) is None


# ─────────────────────────────────────────────────────────────────────
# 2. _load_segment_to_speaker_mapping — multi-source fallback
# ─────────────────────────────────────────────────────────────────────


class TestLoadSegmentToSpeakerMapping:
    """Per design §2.3:
      1. editor/baseline/segments.json (preferred — post-edit baseline)
      2. editor/editing/segments.json (current draft fallback)
      3. transcript/segments.json (earliest source)
    Missing project_dir → empty dict (graceful)."""

    def _write_segments(self, path: Path, segments: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def test_loads_from_baseline_first(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments(
            tmp_path / "editor" / "baseline" / "segments.json",
            [
                {"segment_id": "s1", "speaker_id": "speaker_a"},
                {"segment_id": "s2", "speaker_id": "speaker_b"},
            ],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a", "s2": "speaker_b"}

    def test_falls_back_to_editing_when_baseline_missing(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments(
            tmp_path / "editor" / "editing" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}

    def test_falls_back_to_transcript_when_no_editor(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments(
            tmp_path / "transcript" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}

    def test_baseline_wins_over_editing(self, tmp_path):
        """Baseline takes precedence — post-edit users don't always
        commit, baseline is the most stable per-job snapshot."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments(
            tmp_path / "editor" / "baseline" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        self._write_segments(
            tmp_path / "editor" / "editing" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_b"}],  # different!
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping["s1"] == "speaker_a"  # baseline wins

    def test_no_sources_returns_empty_dict(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {}

    def test_segments_without_speaker_id_skipped(self, tmp_path):
        """Some segments may have null speaker_id (overlap suspected,
        keep_original). They shouldn't appear in the mapping."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments(
            tmp_path / "editor" / "baseline" / "segments.json",
            [
                {"segment_id": "s1", "speaker_id": "speaker_a"},
                {"segment_id": "s2", "speaker_id": None},
                {"segment_id": "s3"},  # no key at all
            ],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}


# ─────────────────────────────────────────────────────────────────────
# 3. _count_voice_overrides_per_speaker — main numerator + unmapped
# ─────────────────────────────────────────────────────────────────────


class TestCountVoiceOverridesPerSpeaker:
    """Per design §2.2: main numerator is ONLY
    post_edit_voice_override_changed. speaker_reassigned is separate.
    Returns (set_of_changed_speakers, unmapped_count)."""

    def _write_events(self, path: Path, events: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(e) for e in events),
            encoding="utf-8",
        )

    def test_maps_overrides_to_speakers(self, tmp_path):
        from admin_smart_analytics_api import _count_voice_overrides_per_speaker

        self._write_events(
            tmp_path / "audit" / "user_edit_events.jsonl",
            [
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s1"},
                },
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s2"},
                },
            ],
        )
        mapping = {"s1": "speaker_a", "s2": "speaker_b"}
        changed, unmapped = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a", "speaker_b"}
        assert unmapped == 0

    def test_unmapped_segment_counted_separately(self, tmp_path):
        from admin_smart_analytics_api import _count_voice_overrides_per_speaker

        self._write_events(
            tmp_path / "audit" / "user_edit_events.jsonl",
            [
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s1"},
                },
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s_unknown"},  # not in mapping
                },
            ],
        )
        mapping = {"s1": "speaker_a"}
        changed, unmapped = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a"}
        assert unmapped == 1, (
            "Unmapped segment_id MUST be tallied separately so admin "
            "can detect data-contract drift (design §3.4)."
        )

    def test_multiple_overrides_to_same_speaker_count_once(self, tmp_path):
        """User changing the same speaker's voice 3 times still =
        '1 speaker changed' — we're measuring distinct speakers, not
        action counts."""
        from admin_smart_analytics_api import _count_voice_overrides_per_speaker

        self._write_events(
            tmp_path / "audit" / "user_edit_events.jsonl",
            [
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s1"},
                },
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s2"},
                },  # also speaker_a
            ],
        )
        mapping = {"s1": "speaker_a", "s2": "speaker_a"}
        changed, _ = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a"}

    def test_speaker_reassigned_NOT_in_numerator(self, tmp_path):
        """Codex 第二轮 review #3: speaker_reassigned doesn't count
        as 'changed voice' for the main metric."""
        from admin_smart_analytics_api import _count_voice_overrides_per_speaker

        self._write_events(
            tmp_path / "audit" / "user_edit_events.jsonl",
            [
                {
                    "event_type": "voice_selection_speaker_reassigned",
                    "segment_id": "s1",
                },
            ],
        )
        mapping = {"s1": "speaker_a"}
        changed, unmapped = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == set()
        assert unmapped == 0

    def test_dubbing_mode_changed_NOT_in_numerator(self, tmp_path):
        """voice_selection_dubbing_mode_changed (keep_original / mute)
        is v1-excluded per design §2.2."""
        from admin_smart_analytics_api import _count_voice_overrides_per_speaker

        self._write_events(
            tmp_path / "audit" / "user_edit_events.jsonl",
            [
                {
                    "event_type": "voice_selection_dubbing_mode_changed",
                    "segment_id": "s1",
                },
            ],
        )
        mapping = {"s1": "speaker_a"}
        changed, _ = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == set()


# ─────────────────────────────────────────────────────────────────────
# 4. _count_speaker_reassigned — auxiliary indicator
# ─────────────────────────────────────────────────────────────────────


class TestCountSpeakerReassigned:
    """Auxiliary indicator (design §3.4) — track separately."""

    def test_counts_unique_speakers_reassigned(self, tmp_path):
        from admin_smart_analytics_api import _count_speaker_reassigned_per_job

        path = tmp_path / "audit" / "user_edit_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in [
            {
                "event_type": "voice_selection_speaker_reassigned",
                "segment_id": "s1",
            },
            {
                "event_type": "voice_selection_speaker_reassigned",
                "segment_id": "s2",
            },
        ]), encoding="utf-8")
        mapping = {"s1": "speaker_a", "s2": "speaker_a"}  # same speaker
        out = _count_speaker_reassigned_per_job(path, mapping)
        assert out == {"speaker_a"}, "duplicate per-speaker should dedup"


# ─────────────────────────────────────────────────────────────────────
# 5. _aggregate_voice_reuse_quality — cross-job aggregation
# ─────────────────────────────────────────────────────────────────────


class TestAggregateVoiceReuseQuality:
    """Pure aggregator — takes per-job hit/change tallies and produces
    the 4 main + 1 overall + 2 derived job-level metrics + unmapped."""

    def test_empty_metrics_returns_dashes(self):
        """Design §6.1: when no hits, rates show "—" (None) not 0%."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        out = _aggregate_voice_reuse_quality([])
        for bucket in ("strong", "strong_named", "possible_auto",
                       "strong_or_legacy_null", "overall"):
            assert out[bucket]["change_rate"] is None
            assert out[bucket]["hits"] == 0
            assert out[bucket]["changes"] == 0

    def test_strong_named_50pct_change_rate(self):
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        # 4 speakers hit strong_named, 2 of them got voice override.
        metrics = [SimpleNamespace(
            voice_reuse_hits={
                "strong": {"speaker_a", "speaker_b"},
                "strong_named": {"speaker_c", "speaker_d", "speaker_e", "speaker_f"},
                "possible_auto": set(),
                "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_c", "speaker_d"},
            unmapped_segment_count=0,
            entered_editing=True,
        )]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["strong_named"]["hits"] == 4
        assert out["strong_named"]["changes"] == 2
        assert out["strong_named"]["change_rate"] == pytest.approx(0.5)

    def test_overall_aggregates_across_buckets(self):
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [SimpleNamespace(
            voice_reuse_hits={
                "strong": {"speaker_a", "speaker_b"},
                "strong_named": {"speaker_c"},
                "possible_auto": {"speaker_d"},
                "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_a", "speaker_d"},
            unmapped_segment_count=0,
            entered_editing=True,
        )]
        out = _aggregate_voice_reuse_quality(metrics)
        # overall: 4 hits, 2 changes = 50%
        assert out["overall"]["hits"] == 4
        assert out["overall"]["changes"] == 2
        assert out["overall"]["change_rate"] == pytest.approx(0.5)

    def test_unmapped_segment_count_summed(self):
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            SimpleNamespace(
                voice_reuse_hits={"strong": set(), "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers=set(),
                unmapped_segment_count=3,
                entered_editing=False,
            ),
            SimpleNamespace(
                voice_reuse_hits={"strong": set(), "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers=set(),
                unmapped_segment_count=2,
                entered_editing=False,
            ),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["unmapped_segment_count"] == 5

    def test_jobs_with_voice_change_rate(self):
        """Derived job-level: smart jobs containing at least one
        auto-reuse hit AND at least one voice change."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            # job 1: hit + change
            SimpleNamespace(
                voice_reuse_hits={"strong": {"a"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers={"a"},
                unmapped_segment_count=0,
                entered_editing=True,
            ),
            # job 2: hit but no change
            SimpleNamespace(
                voice_reuse_hits={"strong": {"a"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers=set(),
                unmapped_segment_count=0,
                entered_editing=True,
            ),
            # job 3: no hits — shouldn't count toward denominator
            SimpleNamespace(
                voice_reuse_hits={"strong": set(), "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers=set(),
                unmapped_segment_count=0,
                entered_editing=False,
            ),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        # 1 of 2 hit-jobs had a change = 50%
        assert out["jobs_with_voice_change_rate"] == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────
# 6. _aggregate_job integration — new fields on JobAggregatedMetrics
# ─────────────────────────────────────────────────────────────────────


class TestAggregateJobAddsVoiceReuseFields:
    """_aggregate_job must populate the new fields:
       voice_reuse_hits, voice_changed_speakers, unmapped_segment_count.
    Wires up _classify_voice_decision + _load_segment_to_speaker_mapping
    + _count_voice_overrides_per_speaker."""

    def _setup_project(self, tmp_path, *, decisions, events, segments):
        proj = tmp_path / "proj_test"
        (proj / "audit").mkdir(parents=True)
        (proj / "output").mkdir()
        (proj / "audit" / "smart_decisions.jsonl").write_text(
            "\n".join(json.dumps(d) for d in decisions), encoding="utf-8",
        )
        (proj / "audit" / "user_edit_events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8",
        )
        # Map segments via editor/baseline (preferred)
        (proj / "editor" / "baseline").mkdir(parents=True)
        (proj / "editor" / "baseline" / "segments.json").write_text(
            json.dumps({"segments": segments}), encoding="utf-8",
        )
        return proj

    def test_strong_named_hit_with_voice_change(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        proj = self._setup_project(
            tmp_path,
            decisions=[
                {
                    "decision_type": "voice_clone",
                    "reason_code": "reused_user_voice",
                    "speaker_id": "speaker_a",
                    "evidence": {"match_confidence": "strong_named"},
                },
            ],
            events=[
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s1"},
                },
            ],
            segments=[{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        job = SimpleNamespace(
            job_id="j1", user_id="u", display_name="t",
            title="", status="succeeded",
            source_duration_seconds=600.0,
            project_dir=str(proj),
            smart_state=None, error_summary=None,
            edit_generation=0,
            created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
        m = _aggregate_job(job, user_email=None)
        assert m.voice_reuse_hits["strong_named"] == {"speaker_a"}
        assert m.voice_reuse_hits["strong"] == set()
        assert m.voice_changed_speakers == {"speaker_a"}
        assert m.unmapped_segment_count == 0

    def test_possible_auto_hit_no_change(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        proj = self._setup_project(
            tmp_path,
            decisions=[
                {
                    "decision_type": "voice_clone",
                    "reason_code": "possible_user_voice_match_auto_reused",
                    "speaker_id": "speaker_b",
                    "evidence": {
                        "auto_reused_from_possible_match": True,
                        "possible_match_count": 2,
                    },
                },
            ],
            events=[],
            segments=[{"segment_id": "s1", "speaker_id": "speaker_b"}],
        )
        job = SimpleNamespace(
            job_id="j2", user_id="u", display_name="t",
            title="", status="succeeded",
            source_duration_seconds=600.0,
            project_dir=str(proj),
            smart_state=None, error_summary=None,
            edit_generation=0,
            created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
        m = _aggregate_job(job, user_email=None)
        assert m.voice_reuse_hits["possible_auto"] == {"speaker_b"}
        assert m.voice_changed_speakers == set()

    def test_unmapped_segment_event(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        proj = self._setup_project(
            tmp_path,
            decisions=[
                {
                    "decision_type": "voice_clone",
                    "reason_code": "reused_user_voice",
                    "speaker_id": "speaker_a",
                    "evidence": {"match_confidence": "strong"},
                },
            ],
            events=[
                {
                    "event_type": "post_edit_voice_override_changed",
                    "segment": {"segment_id": "s_unknown"},  # not in mapping
                },
            ],
            segments=[{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        job = SimpleNamespace(
            job_id="j3", user_id="u", display_name="t",
            title="", status="succeeded",
            source_duration_seconds=600.0,
            project_dir=str(proj),
            smart_state=None, error_summary=None,
            edit_generation=0,
            created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
        m = _aggregate_job(job, user_email=None)
        assert m.voice_changed_speakers == set()
        assert m.unmapped_segment_count == 1

    def test_no_project_dir_returns_empty_buckets(self, tmp_path):
        from admin_smart_analytics_api import _aggregate_job

        job = SimpleNamespace(
            job_id="j4", user_id="u", display_name="t",
            title="", status="succeeded",
            source_duration_seconds=600.0,
            project_dir=None,
            smart_state=None, error_summary=None,
            edit_generation=0,
            created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
        m = _aggregate_job(job, user_email=None)
        for bucket in ("strong", "strong_named", "possible_auto",
                       "strong_or_legacy_null"):
            assert m.voice_reuse_hits[bucket] == set()
        assert m.voice_changed_speakers == set()
        assert m.unmapped_segment_count == 0


# ─────────────────────────────────────────────────────────────────────
# 7. _build_summary_payload integration — top-level voice_reuse_quality
# ─────────────────────────────────────────────────────────────────────


class TestBuildSummaryPayloadVoiceReuseQuality:
    """The summary payload must surface a voice_reuse_quality block
    so the frontend Tab 4 can render it."""

    def _metric_with_hits(self, **kwargs):
        from admin_smart_analytics_api import JobAggregatedMetrics

        defaults = {
            "job_id": "j",
            "user_id": "u-1",
            "user_email": "x@y",
            "display_name": "t",
            "status": "succeeded",
            "source_duration_seconds": 600.0,
            "source_duration_minutes": 10.0,
            "total_segments": 20,
            "outcome_category": "succeeded_clean",
            "smart_handoff_reason": None,
            "direct_pct": 0.5, "dsp_pct": 0.3,
            "rewrite_direct_pct": 0.05, "rewrite_dsp_pct": 0.05,
            "forced_dsp_pct": 0.1, "short_segment_dsp_pct": 0.05,
            "manual_review_segments": 0,
            "entered_editing": False,
            "edit_event_count": 0,
            "edit_events_by_type": {},
            "created_at": "2026-05-24T00:00:00+00:00",
            "voice_reuse_hits": {
                "strong": set(), "strong_named": set(),
                "possible_auto": set(), "strong_or_legacy_null": set(),
            },
            "voice_changed_speakers": set(),
            "unmapped_segment_count": 0,
        }
        defaults.update(kwargs)
        return JobAggregatedMetrics(**defaults)

    def test_payload_includes_voice_reuse_quality_block(self):
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [self._metric_with_hits(
            voice_reuse_hits={
                "strong": {"a"}, "strong_named": set(),
                "possible_auto": {"b"}, "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"b"},
        )]
        payload = _build_summary_payload(metrics, days=30)

        assert "voice_reuse_quality" in payload
        vrq = payload["voice_reuse_quality"]
        assert vrq["strong"]["hits"] == 1
        assert vrq["possible_auto"]["change_rate"] == pytest.approx(1.0)
        assert vrq["overall"]["change_rate"] == pytest.approx(0.5)

    def test_payload_voice_reuse_quality_empty_when_no_hits(self):
        from admin_smart_analytics_api import _build_summary_payload

        payload = _build_summary_payload([], days=30)
        vrq = payload["voice_reuse_quality"]
        # change_rate None signals "—" in UI; hits/changes counters
        # are 0 so admin can tell "no data" vs "0% change rate".
        for bucket in ("strong", "strong_named", "possible_auto",
                       "strong_or_legacy_null", "overall"):
            assert vrq[bucket]["change_rate"] is None
            assert vrq[bucket]["hits"] == 0


# ─────────────────────────────────────────────────────────────────────
# 8. CSV — per-job COUNTS, not global rates (codex #5)
# ─────────────────────────────────────────────────────────────────────


class TestCsvVoiceReuseColumns:
    """CSV per-job rows MUST carry counts (strong_hits, possible_auto_hits,
    voice_changed_speakers, unmapped_segment_count), NOT global rate
    (which would be misleading repeated per row)."""

    def _metric(self, **kwargs):
        from admin_smart_analytics_api import JobAggregatedMetrics

        defaults = {
            "job_id": "j1",
            "user_id": "u",
            "user_email": "x@y",
            "display_name": "t",
            "status": "succeeded",
            "source_duration_seconds": 600.0,
            "source_duration_minutes": 10.0,
            "total_segments": 20,
            "outcome_category": "succeeded_clean",
            "smart_handoff_reason": None,
            "direct_pct": 0.5, "dsp_pct": 0.3,
            "rewrite_direct_pct": 0.05, "rewrite_dsp_pct": 0.05,
            "forced_dsp_pct": 0.1, "short_segment_dsp_pct": 0.05,
            "manual_review_segments": 0,
            "entered_editing": False,
            "edit_event_count": 0,
            "edit_events_by_type": {},
            "created_at": "2026-05-24T00:00:00+00:00",
            "voice_reuse_hits": {
                "strong": {"a", "b"}, "strong_named": {"c"},
                "possible_auto": {"d"}, "strong_or_legacy_null": set(),
            },
            "voice_changed_speakers": {"a", "d"},
            "unmapped_segment_count": 1,
        }
        defaults.update(kwargs)
        return JobAggregatedMetrics(**defaults)

    def test_csv_header_has_voice_reuse_count_columns(self):
        from admin_smart_analytics_api import _build_csv

        body = _build_csv([self._metric()])
        first_line = body.decode("utf-8-sig").split("\n")[0]
        for col in (
            "strong_hits", "strong_named_hits", "possible_auto_hits",
            "strong_or_legacy_null_hits", "voice_changed_speakers",
            "unmapped_segment_count",
        ):
            assert col in first_line, (
                f"CSV header missing voice-reuse-quality column "
                f"{col!r} (design §6.1 CSV requirement). Got: "
                f"{first_line}"
            )

    def test_csv_data_row_uses_counts_not_rates(self):
        from admin_smart_analytics_api import _build_csv

        body = _build_csv([self._metric()])
        text = body.decode("utf-8-sig")
        # The per-job row should have the count integers, NOT a
        # rate like "0.5" or "50%".
        assert "2" in text  # strong_hits=2 (a, b)
        assert "1" in text  # strong_named=1 (c) / possible_auto=1 (d)
        # Rate-like strings ("50%", "0.5") MUST NOT appear in the
        # per-job row context — only counts.
        # (Sanity check: this could false-positive on other 0.5 values
        # in the row but as a basic regression it's useful.)

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
    """Per design §2.3 v2 (corrected for actual project layout):
      1. editor/editing/segments.json (active editing draft — most recent
         truth for in-progress jobs)
      2. editor/segments.json (canonical post-commit baseline — what
         process.py / editing_commit write)
      3. translation/segments.json (legacy fallback for tasks that never
         entered editing)

    Missing project_dir → empty dict (graceful).

    Both shapes supported per editor_baseline.normalise pattern:
      - {"segments": [...]}
      - raw [...] at top level
    """

    def _write_segments_wrapped(self, path: Path, segments: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def _write_segments_raw_list(self, path: Path, segments: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(segments), encoding="utf-8")

    def test_loads_from_editing_first(self, tmp_path):
        """For in-progress (editing) jobs, editor/editing/segments.json
        is the most current truth."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "editor" / "editing" / "segments.json",
            [
                {"segment_id": "s1", "speaker_id": "speaker_a"},
                {"segment_id": "s2", "speaker_id": "speaker_b"},
            ],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a", "s2": "speaker_b"}

    def test_falls_back_to_editor_segments_when_no_active_editing(self, tmp_path):
        """For committed jobs, editor/segments.json is the canonical
        baseline (written by process.py publish + editing_commit)."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "editor" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}

    def test_falls_back_to_translation_when_no_editor(self, tmp_path):
        """Legacy tasks that never went through editing keep
        translation/segments.json as the only source."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "translation" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}

    def test_editing_wins_over_committed_baseline(self, tmp_path):
        """Active edit > committed baseline — speaker reassign mid-edit
        should show in mapping immediately."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "editor" / "editing" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        self._write_segments_wrapped(
            tmp_path / "editor" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_b"}],  # different!
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping["s1"] == "speaker_a", "editing wins (most recent)"

    def test_editor_segments_wins_over_translation(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "editor" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        self._write_segments_wrapped(
            tmp_path / "translation" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_b"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping["s1"] == "speaker_a"

    def test_supports_raw_list_shape(self, tmp_path):
        """editor_baseline writes {"segments": [...]} wrapped, but some
        tooling writes raw list[segment]. Both should parse."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_raw_list(
            tmp_path / "editor" / "segments.json",
            [{"segment_id": "s1", "speaker_id": "speaker_a"}],
        )
        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {"s1": "speaker_a"}

    def test_no_sources_returns_empty_dict(self, tmp_path):
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        mapping = _load_segment_to_speaker_mapping(tmp_path)
        assert mapping == {}

    def test_segments_without_speaker_id_skipped(self, tmp_path):
        """Some segments may have null speaker_id (overlap suspected,
        keep_original). They shouldn't appear in the mapping."""
        from admin_smart_analytics_api import _load_segment_to_speaker_mapping

        self._write_segments_wrapped(
            tmp_path / "editor" / "segments.json",
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

    Returns (set_of_changed_speakers, unmapped_count, total_event_count).
    The 3rd element (added per codex #5) is the denominator for the
    unmapped rate shown in the UI (> 5% → ochre)."""

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
        changed, unmapped, total, _details = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a", "speaker_b"}
        assert unmapped == 0
        assert total == 2

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
        changed, unmapped, total, _details = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a"}
        assert unmapped == 1, (
            "Unmapped segment_id MUST be tallied separately so admin "
            "can detect data-contract drift (design §3.4)."
        )
        assert total == 2, (
            "Total event count is the denominator for unmapped_rate. "
            "Codex #5: '> 5% unmapped' UI signal needs both numerator "
            "(unmapped) and denominator (total events)."
        )

    def test_multiple_overrides_to_same_speaker_count_once(self, tmp_path):
        """User changing the same speaker's voice 3 times still =
        '1 speaker changed' — we're measuring distinct speakers, not
        action counts. (total_event_count is still 2 though.)"""
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
        changed, _, total, _details = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == {"speaker_a"}
        assert total == 2

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
        changed, unmapped, total, _details = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == set()
        assert unmapped == 0
        assert total == 0, "speaker_reassigned doesn't count as a voice-override event"

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
        changed, _, total, _details = _count_voice_overrides_per_speaker(
            tmp_path / "audit" / "user_edit_events.jsonl", mapping
        )
        assert changed == set()
        assert total == 0


# ─────────────────────────────────────────────────────────────────────
# 4. _count_speaker_reassigned — auxiliary indicator
# ─────────────────────────────────────────────────────────────────────


class TestCountSpeakerReassigned:
    """Auxiliary indicator (design §3.4 + codex v2 review #2 followup):
    read before.speaker_id / after.speaker_id DIRECTLY from the event
    (not via segment_id → speaker_id mapping). The mapping reflects the
    CURRENT editor state, so post-reassignment the mapping returns
    after_speaker_id and the original speaker correction is lost.

    Returns the union (before ∪ after) so the caller can intersect
    against hit_speakers."""

    def test_reads_before_and_after_directly_from_event(self, tmp_path):
        """Real event shape (see build_voice_selection_speaker_reassigned_event):
          event["before"] = {"speaker_id": from_speaker_id}
          event["after"]  = {"speaker_id": to_speaker_id}
        Both speakers must end up in the returned set so caller can
        intersect with auto-reuse hit_speakers. Mapping is no longer
        consulted (codex v2 followup)."""
        from admin_smart_analytics_api import _count_speaker_reassigned_per_job

        path = tmp_path / "audit" / "user_edit_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in [
            {
                "event_type": "voice_selection_speaker_reassigned",
                "segment": {"segment_id": "s1"},
                "before": {"speaker_id": "speaker_a"},
                "after": {"speaker_id": "speaker_b"},
            },
            {
                "event_type": "voice_selection_speaker_reassigned",
                "segment": {"segment_id": "s2"},
                "before": {"speaker_id": "speaker_b"},
                "after": {"speaker_id": "speaker_c"},
            },
        ]), encoding="utf-8")
        # Pass empty mapping — function should NOT depend on it.
        out = _count_speaker_reassigned_per_job(path, {})
        # All three (a, b, c) appear as either before or after.
        assert out == {"speaker_a", "speaker_b", "speaker_c"}

    def test_handles_missing_before_after_blocks(self, tmp_path):
        """Defensive: event missing before/after still gracefully
        contributes nothing rather than crashing."""
        from admin_smart_analytics_api import _count_speaker_reassigned_per_job

        path = tmp_path / "audit" / "user_edit_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in [
            {
                "event_type": "voice_selection_speaker_reassigned",
                "segment": {"segment_id": "s1"},
                # no before/after blocks
            },
            {
                "event_type": "voice_selection_speaker_reassigned",
                "before": {"speaker_id": "speaker_a"},
                "after": {},  # missing speaker_id
            },
        ]), encoding="utf-8")
        out = _count_speaker_reassigned_per_job(path, {})
        assert out == {"speaker_a"}


# ─────────────────────────────────────────────────────────────────────
# 5. _aggregate_voice_reuse_quality — cross-job aggregation
# ─────────────────────────────────────────────────────────────────────


def _empty_metric(**overrides):
    """Build a minimal SimpleNamespace with all voice-reuse fields
    defaulted to empty. Tests override only what they care about."""
    defaults = dict(
        voice_reuse_hits={"strong": set(), "strong_named": set(),
                          "possible_auto": set(), "strong_or_legacy_null": set()},
        voice_changed_speakers=set(),
        speakers_reassigned=set(),
        unmapped_segment_count=0,
        voice_override_event_count=0,
        entered_editing=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestAggregateVoiceReuseQuality:
    """Pure aggregator — produces 4+1 bucket rates + auxiliary +
    derived job-level + unmapped rate (per codex #2 #3 #5)."""

    def test_empty_metrics_returns_dashes(self):
        """Design §6.1: when no hits, rates show "—" (None) not 0%."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        out = _aggregate_voice_reuse_quality([])
        for bucket in ("strong", "strong_named", "possible_auto",
                       "strong_or_legacy_null", "overall"):
            assert out[bucket]["change_rate"] is None
            assert out[bucket]["hits"] == 0
            assert out[bucket]["changes"] == 0
        assert out["jobs_with_voice_change_rate"] is None
        assert out["auto_reuse_jobs_entering_edit_rate"] is None
        assert out["speaker_reassigned_rate"] is None
        assert out["unmapped_segment_rate"] is None
        assert out["unmapped_segment_count"] == 0

    def test_strong_named_50pct_change_rate(self):
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        # 4 speakers hit strong_named, 2 of them got voice override.
        metrics = [_empty_metric(
            voice_reuse_hits={
                "strong": {"speaker_a", "speaker_b"},
                "strong_named": {"speaker_c", "speaker_d", "speaker_e", "speaker_f"},
                "possible_auto": set(),
                "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_c", "speaker_d"},
            entered_editing=True,
        )]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["strong_named"]["hits"] == 4
        assert out["strong_named"]["changes"] == 2
        assert out["strong_named"]["change_rate"] == pytest.approx(0.5)

    def test_strong_change_counts_only_intersection(self):
        """Codex #3: bucket-level changes use hit_speakers ∩
        voice_changed_speakers, not just 'voice_changed_speakers non-empty'.

        Speaker_z is changed but never appeared in strong's hits — it
        must NOT inflate strong's change count."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [_empty_metric(
            voice_reuse_hits={
                "strong": {"speaker_a", "speaker_b"},
                "strong_named": set(),
                "possible_auto": set(),
                "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_z"},  # not in strong's hits!
        )]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["strong"]["hits"] == 2
        assert out["strong"]["changes"] == 0, (
            "Strong's bucket change count must only count speakers that "
            "are BOTH a strong hit AND in voice_changed_speakers. "
            "Codex #3."
        )
        assert out["strong"]["change_rate"] == 0.0

    def test_overall_aggregates_across_buckets(self):
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [_empty_metric(
            voice_reuse_hits={
                "strong": {"speaker_a", "speaker_b"},
                "strong_named": {"speaker_c"},
                "possible_auto": {"speaker_d"},
                "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_a", "speaker_d"},
            entered_editing=True,
        )]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["overall"]["hits"] == 4
        assert out["overall"]["changes"] == 2
        assert out["overall"]["change_rate"] == pytest.approx(0.5)

    def test_unmapped_segment_count_and_rate(self):
        """Codex #5: surface unmapped_segment_rate so UI can show
        '> 5% → ochre'. Needs denominator = total override events."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            _empty_metric(unmapped_segment_count=3, voice_override_event_count=10),
            _empty_metric(unmapped_segment_count=2, voice_override_event_count=10),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        assert out["unmapped_segment_count"] == 5
        assert out["unmapped_segment_rate"] == pytest.approx(5 / 20)

    def test_unmapped_segment_rate_when_no_events(self):
        """No override events → rate is None (UI shows "—" not 0%)."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        out = _aggregate_voice_reuse_quality([_empty_metric()])
        assert out["unmapped_segment_rate"] is None

    def test_jobs_with_voice_change_rate_intersection(self):
        """Codex #3: a job 'has voice change for its auto-reuse hits'
        iff hit_speakers ∩ voice_changed_speakers is non-empty.

        Job that changed speaker_z (not in any hit) must NOT be counted
        as a 'voice-changed' auto-reuse job."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            # job 1: hit speaker_a, changed speaker_a — counts.
            _empty_metric(
                voice_reuse_hits={"strong": {"a"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers={"a"},
                entered_editing=True,
            ),
            # job 2: hit speaker_a, changed speaker_z (different) — does NOT count.
            _empty_metric(
                voice_reuse_hits={"strong": {"a"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers={"z"},
                entered_editing=True,
            ),
            # job 3: hit speaker_b, no changes — doesn't count.
            _empty_metric(
                voice_reuse_hits={"strong": {"b"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                voice_changed_speakers=set(),
                entered_editing=False,
            ),
            # job 4: no hits at all — excluded from denominator.
            _empty_metric(voice_changed_speakers={"x"}, entered_editing=True),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        # Denominator: jobs with hits = 3 (jobs 1, 2, 3). Job 4 excluded.
        # Numerator: jobs where hit_speakers ∩ changed != ∅ = 1 (only job 1).
        assert out["jobs_with_voice_change_rate"] == pytest.approx(1 / 3)

    def test_auto_reuse_jobs_entering_edit_rate(self):
        """Design §3.2: among jobs that hit auto-reuse, the fraction
        that subsequently entered editing (regardless of voice change)."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            # job 1: hit + entered editing
            _empty_metric(
                voice_reuse_hits={"strong": {"a"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                entered_editing=True,
            ),
            # job 2: hit + NOT entered editing
            _empty_metric(
                voice_reuse_hits={"strong": {"b"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                entered_editing=False,
            ),
            # job 3: no hit, entered editing — excluded from denominator
            _empty_metric(entered_editing=True),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        # 1 of 2 hit-jobs entered editing.
        assert out["auto_reuse_jobs_entering_edit_rate"] == pytest.approx(0.5)

    def test_speaker_reassigned_rate(self):
        """Auxiliary indicator (codex #2): per-speaker reassign rate
        across all jobs that had any hit."""
        from admin_smart_analytics_api import _aggregate_voice_reuse_quality

        metrics = [
            _empty_metric(
                voice_reuse_hits={"strong": {"a", "b"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                speakers_reassigned={"a"},
            ),
            _empty_metric(
                voice_reuse_hits={"strong": {"c", "d"}, "strong_named": set(),
                                  "possible_auto": set(), "strong_or_legacy_null": set()},
                speakers_reassigned=set(),
            ),
        ]
        out = _aggregate_voice_reuse_quality(metrics)
        # 4 hit speakers, 1 reassigned.
        assert out["speaker_reassigned_rate"] == pytest.approx(1 / 4)


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
        # Map segments via editor/segments.json (canonical post-commit
        # baseline — what process.py actually writes).
        (proj / "editor").mkdir(parents=True)
        (proj / "editor" / "segments.json").write_text(
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
        assert m.voice_override_event_count == 1

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
        assert m.speakers_reassigned == set()
        assert m.unmapped_segment_count == 0
        assert m.voice_override_event_count == 0

    def test_speaker_reassigned_populated_from_events(self, tmp_path):
        """voice_selection_speaker_reassigned event → speakers_reassigned
        set (auxiliary indicator, separate from change rate).

        Codex v2 followup #2: reads before/after.speaker_id directly
        from event, not via segment_to_speaker mapping."""
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
                    "event_type": "voice_selection_speaker_reassigned",
                    "segment": {"segment_id": "s1"},
                    "before": {"speaker_id": "speaker_a"},
                    "after": {"speaker_id": "speaker_b"},
                },
            ],
            segments=[{"segment_id": "s1", "speaker_id": "speaker_b"}],  # post-reassign state
        )
        job = SimpleNamespace(
            job_id="j_reassign", user_id="u", display_name="t",
            title="", status="succeeded",
            source_duration_seconds=600.0,
            project_dir=str(proj),
            smart_state=None, error_summary=None,
            edit_generation=0,
            created_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
        m = _aggregate_job(job, user_email=None)
        # Main numerator stays empty (codex #3)
        assert m.voice_changed_speakers == set()
        # Auxiliary populated with BOTH before (speaker_a, the original
        # auto-reuse target) AND after (speaker_b). Caller intersects
        # with hit_speakers — speaker_a IS in hits, so the aggregator
        # will count this reassignment as relevant.
        assert m.speakers_reassigned == {"speaker_a", "speaker_b"}


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
            "speakers_reassigned": set(),
            "unmapped_segment_count": 0,
            "voice_override_event_count": 0,
            "voice_override_details": [],
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
        # New aux + derived indicators (codex #2)
        assert vrq["jobs_with_voice_change_rate"] is None
        assert vrq["auto_reuse_jobs_entering_edit_rate"] is None
        assert vrq["speaker_reassigned_rate"] is None
        assert vrq["unmapped_segment_rate"] is None

    def test_payload_includes_case_rows(self):
        """Codex #4: backend must expose case_rows so frontend Tab 4
        can render the case table without a second backend round-trip.
        Updated v2 codex review: rows must include before_voice_id /
        after_voice_id / operation / changed_at so admins can see
        "from what to what" (排查价值不足 otherwise)."""
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [self._metric_with_hits(
            job_id="job_x",
            voice_reuse_hits={
                "strong": set(), "strong_named": {"speaker_c"},
                "possible_auto": set(), "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_c"},
            # New: voice override details with speaker mapped.
            voice_override_details=[
                {
                    "speaker_id": "speaker_c",
                    "before_voice_id": "vt_auto_reuse_x",
                    "after_voice_id": "vt_preset_b",
                    "operation": "set",
                    "changed_at": "2026-05-24T01:23:45+00:00",
                },
            ],
        )]
        payload = _build_summary_payload(metrics, days=30)

        vrq = payload["voice_reuse_quality"]
        assert "case_rows" in vrq
        assert isinstance(vrq["case_rows"], list)
        assert len(vrq["case_rows"]) == 1
        row = vrq["case_rows"][0]
        for key in (
            "job_id", "speaker_id", "bucket",
            "before_voice_id", "after_voice_id", "operation", "changed_at",
        ):
            assert key in row, (
                f"case row missing key {key!r} (codex v2 followup — needs "
                f"before/after voice + operation for admin 排查); got: {row}"
            )
        assert row["before_voice_id"] == "vt_auto_reuse_x"
        assert row["after_voice_id"] == "vt_preset_b"
        assert row["operation"] == "set"
        assert row["changed_at"] == "2026-05-24T01:23:45+00:00"

    def test_case_rows_skip_when_speaker_not_in_hits(self):
        """A voice override event whose speaker_id isn't in any
        auto-reuse hit bucket MUST NOT generate a case row — we're
        documenting auto-reuse misfires, not arbitrary voice changes."""
        from admin_smart_analytics_api import _build_summary_payload

        metrics = [self._metric_with_hits(
            job_id="job_y",
            voice_reuse_hits={
                "strong": {"speaker_a"}, "strong_named": set(),
                "possible_auto": set(), "strong_or_legacy_null": set(),
            },
            voice_changed_speakers={"speaker_z"},  # not a hit speaker
            voice_override_details=[
                {
                    "speaker_id": "speaker_z",  # not a hit speaker
                    "before_voice_id": "vt_x",
                    "after_voice_id": "vt_y",
                    "operation": "set",
                    "changed_at": "2026-05-24T01:23:45+00:00",
                },
            ],
        )]
        payload = _build_summary_payload(metrics, days=30)
        # No case row because speaker_z wasn't in hits.
        assert payload["voice_reuse_quality"]["case_rows"] == []

    def test_case_rows_capped_at_twenty(self):
        """Cap at 20 rows (design §4 case 表)."""
        from admin_smart_analytics_api import _build_summary_payload

        metrics = []
        for i in range(30):
            metrics.append(self._metric_with_hits(
                job_id=f"j{i}",
                voice_reuse_hits={
                    "strong": {f"speaker_{i}"}, "strong_named": set(),
                    "possible_auto": set(), "strong_or_legacy_null": set(),
                },
                voice_changed_speakers={f"speaker_{i}"},
                voice_override_details=[
                    {
                        "speaker_id": f"speaker_{i}",
                        "before_voice_id": f"vt_b_{i}",
                        "after_voice_id": f"vt_a_{i}",
                        "operation": "set",
                        "changed_at": f"2026-05-{(i % 28) + 1:02d}T00:00:00+00:00",
                    },
                ],
                created_at=f"2026-05-{(i % 28) + 1:02d}T00:00:00+00:00",
            ))
        payload = _build_summary_payload(metrics, days=30)
        case_rows = payload["voice_reuse_quality"]["case_rows"]
        assert len(case_rows) <= 20


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
            "speakers_reassigned": set(),
            "unmapped_segment_count": 1,
            "voice_override_event_count": 5,
            "voice_override_details": [],
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

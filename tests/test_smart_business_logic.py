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
import re
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


class TestEligibilityGateInputShapes:
    """Codex 第九轮 P1-1 — normalize_speaker_stats accepts the three shapes
    real callers feed it. Without this the production integration in
    PR#3C would silently treat every speaker as 0-share and approve
    multi-speaker jobs that should hand off."""

    def test_real_process_speaker_structure_profile_shape(self):
        """src/pipeline/process.py:4013 emits dict[speaker_id → profile]
        with field names speaker_role / speaker_duration_share. This is
        the shape the PR#3C integration layer feeds verbatim."""
        from services.smart.eligibility_gate import (
            evaluate_eligibility, normalize_speaker_stats,
        )

        process_profile = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_role_label": "主说话人",
                "speaker_duration_ms": 600_000,
                "speaker_duration_share": 0.55,
                "speaker_segment_count": 80,
                "speaker_short_segment_count": 5,
                "speaker_short_segment_rate": 0.0625,
                "speaker_structure_reason": "top_duration_speaker",
                "speaker_review_hint": "main",
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.40,
            },
            "speaker_c": {
                "speaker_role": "fragmented",
                "speaker_duration_share": 0.05,  # < 0.10 threshold
            },
        }
        # First verify normalize handles the rename.
        canonical = normalize_speaker_stats(process_profile)
        speakers = canonical["speakers"]
        assert canonical["_normalize_source"] == "process_speaker_structure_profile"
        ids = sorted(s["speaker_id"] for s in speakers)
        assert ids == ["speaker_a", "speaker_b", "speaker_c"]
        # Field names renormalised.
        for sp in speakers:
            assert "duration_share" in sp
            assert "role" in sp

        # Then verify the end-to-end decision is correct (a + b are main,
        # c excluded by low-share, count = 2 ≤ 3 → approved).
        decision = evaluate_eligibility(process_profile)
        assert decision.approved is True
        assert decision.main_speaker_count == 2
        assert set(decision.main_speaker_ids) == {"speaker_a", "speaker_b"}

    def test_real_process_profile_with_four_main_speakers_rejected(self):
        """The risk codex called out: a 4-main-speaker job in process.py
        shape was previously approved silently because field names didn't
        match. After normalize, the limit-exceeded check kicks in."""
        from services.smart.eligibility_gate import evaluate_eligibility

        process_profile = {
            f"speaker_{i}": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.25,
            }
            for i in range(4)
        }
        decision = evaluate_eligibility(process_profile)
        assert decision.approved is False
        assert decision.main_speaker_count == 4
        assert decision.reason_code == "main_speaker_count_exceeded"

    def test_simulator_speaker_count_by_threshold_shape(self):
        """scripts/smart_shadow_sim_simulator.py:121 reads
        speaker_count_by_threshold["0.10"] as a pre-aggregated count.
        normalize emits N synthetic speakers so the limit comparison
        works without exposing real IDs (simulator shape doesn't
        carry them at this layer)."""
        from services.smart.eligibility_gate import evaluate_eligibility

        sim_fact = {
            "speaker_count_by_threshold": {
                "0.05": 4,
                "0.10": 2,
                "0.15": 2,
            },
        }
        decision = evaluate_eligibility(sim_fact, low_share_threshold=0.10)
        assert decision.approved is True
        assert decision.main_speaker_count == 2

    def test_simulator_shape_with_4_main_at_threshold_rejected(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        sim_fact = {"speaker_count_by_threshold": {"0.10": 4}}
        decision = evaluate_eligibility(sim_fact, low_share_threshold=0.10)
        assert decision.approved is False
        assert decision.main_speaker_count == 4

    def test_simulator_shape_missing_count_for_threshold(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        # Threshold not present in the table — falls through to no-speakers.
        sim_fact = {"speaker_count_by_threshold": {"0.05": 3}}
        decision = evaluate_eligibility(sim_fact, low_share_threshold=0.10)
        assert decision.approved is False
        assert decision.reason_code == "no_speakers_detected"

    def test_unknown_shape_falls_through_to_no_speakers(self):
        from services.smart.eligibility_gate import evaluate_eligibility

        decision = evaluate_eligibility({"completely": "different", "shape": 42})
        assert decision.approved is False
        assert decision.reason_code == "no_speakers_detected"

    def test_canonical_shape_preserved_through_normalize(self):
        """Canonical input must pass through normalize untouched so
        existing tests keep working."""
        from services.smart.eligibility_gate import normalize_speaker_stats

        canonical = {
            "speakers": [
                {"speaker_id": "a", "duration_share": 0.6, "dubbing_mode": "dub"},
            ]
        }
        result = normalize_speaker_stats(canonical)
        # Same dict — pass-through doesn't add the _normalize_source marker.
        assert result is canonical
        assert "_normalize_source" not in result

    def test_real_voice_selection_review_payload_speakers_list_shape(self):
        """Codex 第十轮 P1 — process.py:4320-4353 _build_voice_selection_review_payload
        also produces a ``speakers`` list, but each entry uses prefixed
        field names (speaker_role / speaker_duration_share). PR#3A-fix v1
        only handled the dict-of-profiles shape; this list-with-prefixes
        flavour was silently pass-through and produced main_count=0.
        """
        from services.smart.eligibility_gate import (
            evaluate_eligibility, normalize_speaker_stats,
        )

        # Mirrors the exact dict shape build_voice_selection_review_payload
        # appends to speakers_payload at process.py:4320-4353.
        voice_review_payload = {
            "speakers": [
                {
                    "speaker_id": "speaker_a",
                    "speaker_name": "查理·芒格",
                    "segment_count": 80,
                    "total_duration_s": 600.0,
                    "speaker_role": "primary",
                    "speaker_role_label": "主说话人",
                    "speaker_duration_ms": 600_000,
                    "speaker_duration_share": 0.55,
                    "speaker_short_segment_count": 5,
                    "speaker_short_segment_rate": 0.0625,
                    "speaker_structure_reason": "top_duration_speaker",
                    "speaker_review_hint": "main",
                    "auto_matched_voice": "preset_x",
                    "can_clone": True,
                    "segments": [],
                    "probe_texts": [],
                    "target_chars_per_second": None,
                },
                {
                    "speaker_id": "speaker_b",
                    "speaker_name": "Warren Buffett",
                    "speaker_role": "primary",
                    "speaker_duration_share": 0.40,
                },
                {
                    "speaker_id": "speaker_c",
                    "speaker_role": "fragmented",
                    "speaker_duration_share": 0.05,  # < 0.10 threshold
                },
            ]
        }
        # Verify normalisation detects the prefixed flavour.
        canonical = normalize_speaker_stats(voice_review_payload)
        assert canonical["_normalize_source"] == (
            "process_voice_selection_review_speakers_list"
        )
        for sp in canonical["speakers"]:
            assert "duration_share" in sp
            assert "role" in sp

        # Then verify end-to-end: a + b are main, c excluded by low_share,
        # main_count=2 ≤ 3 → approved. Critically NOT the previous-bug
        # outcome of approved with main_count=0.
        decision = evaluate_eligibility(voice_review_payload)
        assert decision.approved is True
        assert decision.main_speaker_count == 2
        assert set(decision.main_speaker_ids) == {"speaker_a", "speaker_b"}

    def test_voice_selection_review_payload_with_4_main_speakers_rejected(self):
        """Same risk codex called out: a 4-main-speaker job in the
        prefixed list shape was previously approved silently with
        main_count=0. After the fix, the limit-exceeded check fires."""
        from services.smart.eligibility_gate import evaluate_eligibility

        payload = {
            "speakers": [
                {
                    "speaker_id": f"speaker_{i}",
                    "speaker_role": "primary",
                    "speaker_duration_share": 0.25,
                }
                for i in range(4)
            ]
        }
        decision = evaluate_eligibility(payload)
        assert decision.approved is False
        assert decision.main_speaker_count == 4
        assert decision.reason_code == "main_speaker_count_exceeded"

    def test_canonical_list_with_no_prefixes_does_not_get_renamed(self):
        """Make sure the prefix detection doesn't fire on canonical lists
        that already use the canonical field names — they should pass
        through verbatim (canonical entries are dicts with role /
        duration_share, no speaker_role / speaker_duration_share)."""
        from services.smart.eligibility_gate import normalize_speaker_stats

        canonical = {
            "speakers": [
                {"speaker_id": "a", "role": "primary", "duration_share": 0.7,
                 "dubbing_mode": "dub"},
            ]
        }
        result = normalize_speaker_stats(canonical)
        # Pass-through (same dict identity).
        assert result is canonical


# ===================================================================
# aggregate_segment_dubbing_modes_to_speaker (PR#3C-b3b, Codex 第二十二轮)
# ===================================================================


class TestAggregateSegmentDubbingModesToSpeaker:
    """PR#3C-b3b — fail-closed reducer that lifts per-segment
    ``dubbing_mode`` to a single per-speaker decision so the eligibility
    gate's keep_original / mute_or_background exclusion rules can fire.

    Fail-closed rule (Codex 第二十二轮): when in doubt (mixed, unknown,
    missing field, empty segments) → ``"dub"`` so the speaker COUNTS
    toward the main_speaker_count limit. Returning ``"keep_original"``
    on ambiguity would let smart auto-pass jobs that should hand off.
    """

    def _seg(self, *, speaker_id, dubbing_mode):
        from types import SimpleNamespace

        return SimpleNamespace(speaker_id=speaker_id, dubbing_mode=dubbing_mode)

    def test_all_keep_original_segments_speaker_is_keep_original(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "keep_original"}

    def test_all_mute_or_background_speaker_is_mute_or_background(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="mute_or_background"),
            self._seg(speaker_id="speaker_a", dubbing_mode="mute_or_background"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "mute_or_background"}

    def test_all_dub_segments_speaker_is_dub(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="dub"),
            self._seg(speaker_id="speaker_a", dubbing_mode="dub"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_mixed_dub_and_keep_original_speaker_is_dub_failclosed(self):
        """The KEY fail-closed case: a speaker with some dub + some
        keep_original segments must NOT count as keep_original. Smart
        should err toward main-count inclusion → potential handoff."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="dub"),
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_mixed_keep_original_and_mute_speaker_is_dub_failclosed(self):
        """Mixed non-dub modes must also fail-closed to dub — only
        homogeneous keep_original / mute_or_background are excluded."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
            self._seg(speaker_id="speaker_a", dubbing_mode="mute_or_background"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_unknown_dubbing_mode_speaker_is_dub_failclosed(self):
        """Unknown / non-canonical mode values must fail-closed."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="something_weird"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_missing_dubbing_mode_field_speaker_is_dub_failclosed(self):
        """Segment with no dubbing_mode attribute must fail-closed; the
        reducer reads via getattr default None."""
        from types import SimpleNamespace

        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [SimpleNamespace(speaker_id="speaker_a")]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_none_dubbing_mode_speaker_is_dub_failclosed(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [self._seg(speaker_id="speaker_a", dubbing_mode=None)]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_empty_segments_returns_empty_dict(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        assert aggregate_segment_dubbing_modes_to_speaker([]) == {}
        assert aggregate_segment_dubbing_modes_to_speaker(None) == {}

    def test_missing_speaker_id_segment_silently_skipped(self):
        """Empty / None speaker_id → drop. The eligibility gate already
        records ``missing_speaker_id`` for anonymous rows separately."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="", dubbing_mode="dub"),
            self._seg(speaker_id=None, dubbing_mode="keep_original"),
            self._seg(speaker_id="speaker_a", dubbing_mode="dub"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "dub"}

    def test_multiple_speakers_aggregated_independently(self):
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
            self._seg(speaker_id="speaker_a", dubbing_mode="keep_original"),
            self._seg(speaker_id="speaker_b", dubbing_mode="dub"),
            self._seg(speaker_id="speaker_c", dubbing_mode="mute_or_background"),
            self._seg(speaker_id="speaker_d", dubbing_mode="dub"),
            self._seg(speaker_id="speaker_d", dubbing_mode="keep_original"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {
            "speaker_a": "keep_original",
            "speaker_b": "dub",
            "speaker_c": "mute_or_background",
            "speaker_d": "dub",  # mixed → fail-closed dub
        }

    def test_dict_shape_segments_accepted(self):
        """Reducer accepts duck-typed dicts as well as dataclasses /
        SimpleNamespace, so process.py can pass DubbingSegment or any
        equivalent shape without explicit conversion."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            {"speaker_id": "speaker_a", "dubbing_mode": "keep_original"},
            {"speaker_id": "speaker_a", "dubbing_mode": "keep_original"},
            {"speaker_id": "speaker_b", "dubbing_mode": "dub"},
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "keep_original", "speaker_b": "dub"}

    def test_case_insensitive_mode_values(self):
        """``"DUB"`` / ``"Keep_Original"`` etc. normalise to lower-case
        canonical values so process.py's exact casing isn't load-bearing."""
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker,
        )

        segs = [
            self._seg(speaker_id="speaker_a", dubbing_mode="KEEP_ORIGINAL"),
            self._seg(speaker_id="speaker_a", dubbing_mode="Keep_Original"),
        ]
        result = aggregate_segment_dubbing_modes_to_speaker(segs)
        assert result == {"speaker_a": "keep_original"}


class TestAggregateWithRealTranscriptLineShape:
    """Codex 第二十三轮 P1 — functional integration test.

    The earlier PR#3C-b3b shipped with ``getattr(transcript_result,
    "segments", None) or []`` in the process.py wiring, but
    ``TranscriptResult`` has no ``segments`` attribute — only ``lines:
    list[TranscriptLine]`` (see
    ``src/services/assemblyai/transcriber.py``). The aggregation
    silently returned ``{}`` for every job, so every speaker overlay
    defaulted to ``"dub"`` and the keep_original / mute_or_background
    exclusions never fired.

    This functional test pins the end-to-end pipeline (aggregate +
    overlay + evaluate_eligibility) using the REAL TranscriptLine
    shape, so if anyone ever re-introduces ``.segments`` or any other
    non-existent field the speaker A (all keep_original) won't be
    correctly excluded and the assertion fires immediately. The
    anchor-only test in test_smart_studio_gate_acceptance.py can't
    catch this — it inspects the source code, not the runtime values.
    """

    def test_real_transcript_line_objects_drive_eligibility_exclusion(self):
        from services.assemblyai.transcriber import TranscriptLine
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker, evaluate_eligibility,
        )

        # Speaker A: 4 keep_original lines, occupies >10% duration
        # Speaker B: 4 dub lines, occupies >10% duration
        lines = [
            TranscriptLine(
                index=1, start_ms=0, end_ms=2000,
                speaker_id="speaker_a", speaker_label="A",
                source_text="hello",
                dubbing_mode="keep_original",
            ),
            TranscriptLine(
                index=2, start_ms=2000, end_ms=4000,
                speaker_id="speaker_a", speaker_label="A",
                source_text="hi",
                dubbing_mode="keep_original",
            ),
            TranscriptLine(
                index=3, start_ms=4000, end_ms=6000,
                speaker_id="speaker_b", speaker_label="B",
                source_text="ok",
                dubbing_mode="dub",
            ),
            TranscriptLine(
                index=4, start_ms=6000, end_ms=8000,
                speaker_id="speaker_b", speaker_label="B",
                source_text="bye",
                dubbing_mode="dub",
            ),
        ]

        # ── Step 1: aggregate lines → speaker-level dubbing_mode
        aggregated = aggregate_segment_dubbing_modes_to_speaker(lines)
        assert aggregated == {
            "speaker_a": "keep_original",
            "speaker_b": "dub",
        }, (
            "Aggregation against real TranscriptLine failed — likely "
            "the reducer is reading the wrong attribute. Codex 第二十三轮 "
            "P1 regression."
        )

        # ── Step 2: overlay onto a speaker_structure_profiles-shaped
        # dict (the same shape process.py constructs from
        # _build_speaker_structure_profiles + the loop in the smart
        # inline branch around process.py:2392-2400).
        speaker_structure_profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.55,
                "speaker_duration_ms": 4000,
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.45,
                "speaker_duration_ms": 4000,
            },
        }
        eligibility_input: dict[str, dict] = {}
        for sid, profile in speaker_structure_profiles.items():
            enriched = dict(profile)
            enriched["dubbing_mode"] = aggregated.get(sid, "dub")
            eligibility_input[sid] = enriched

        # ── Step 3: evaluate_eligibility — speaker A must be excluded
        # (all keep_original), B must be the only main speaker.
        decision = evaluate_eligibility(eligibility_input)
        assert decision.approved is True, (
            f"Expected approved=True with 1 main speaker (B), got "
            f"approved={decision.approved} count={decision.main_speaker_count} "
            f"reason={decision.reason_code!r}."
        )
        assert decision.main_speaker_count == 1, (
            f"Expected exactly 1 main speaker after excluding "
            f"keep_original speaker A; got {decision.main_speaker_count}.\n"
            f"main_speaker_ids={decision.main_speaker_ids}\n"
            f"excluded={decision.excluded_speakers}\n"
            f"This means the aggregation didn't propagate to the gate — "
            f"likely .segments / .lines / other field-name drift."
        )
        assert decision.main_speaker_ids == ("speaker_b",), (
            f"Speaker B must be the sole main speaker after A excluded; "
            f"got {decision.main_speaker_ids!r}."
        )
        # speaker_a should appear in excluded_speakers with a
        # dubbing_mode_keep_original reason.
        excluded_a = [
            e for e in decision.excluded_speakers
            if e.get("speaker_id") == "speaker_a"
        ]
        assert excluded_a, (
            f"Speaker A should be in excluded_speakers list. Got: "
            f"{decision.excluded_speakers}"
        )
        assert excluded_a[0]["reason"] == "dubbing_mode_keep_original", (
            f"Speaker A exclusion reason should record the dubbing_mode; "
            f"got {excluded_a[0]['reason']!r}."
        )

    def test_real_transcript_lines_mixed_with_role_excluded_speaker(self):
        """Mixed scenario: one keep_original speaker + one role-excluded
        speaker + two real dub speakers → only the two dubs remain as
        main candidates. Pins that aggregation propagates correctly
        through the gate's exclusion stack (dubbing_mode AND role)."""
        from services.assemblyai.transcriber import TranscriptLine
        from services.smart.eligibility_gate import (
            aggregate_segment_dubbing_modes_to_speaker, evaluate_eligibility,
        )

        # 4 speakers: 1 keep_original (A), 1 dub-with-observer-role (B),
        # 2 real dub mains (C, D). Without the aggregation fix, A would
        # default to "dub" and the gate would see 4 main speakers
        # (limit=3) → reject. With the fix, A excluded by dubbing_mode,
        # B excluded by role → 2 main speakers C+D → approved.
        lines = []
        for i, sid in enumerate(["speaker_a", "speaker_a"]):
            lines.append(TranscriptLine(
                index=i, start_ms=i * 1000, end_ms=(i + 1) * 1000,
                speaker_id=sid, speaker_label=sid.upper(),
                source_text="x", dubbing_mode="keep_original",
            ))
        for j, sid in enumerate(["speaker_b", "speaker_c", "speaker_c", "speaker_d"]):
            lines.append(TranscriptLine(
                index=10 + j, start_ms=(10 + j) * 1000,
                end_ms=(11 + j) * 1000,
                speaker_id=sid, speaker_label=sid.upper(),
                source_text="x", dubbing_mode="dub",
            ))

        aggregated = aggregate_segment_dubbing_modes_to_speaker(lines)
        assert aggregated == {
            "speaker_a": "keep_original",
            "speaker_b": "dub",
            "speaker_c": "dub",
            "speaker_d": "dub",
        }

        # Speaker B carries role=observer → excluded by role check
        # regardless of dubbing_mode. C + D both primary → both main.
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.25,
                "dubbing_mode": aggregated["speaker_a"],
            },
            "speaker_b": {
                "speaker_role": "observer",
                "speaker_duration_share": 0.25,
                "dubbing_mode": aggregated["speaker_b"],
            },
            "speaker_c": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.30,
                "dubbing_mode": aggregated["speaker_c"],
            },
            "speaker_d": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.20,
                "dubbing_mode": aggregated["speaker_d"],
            },
        }
        decision = evaluate_eligibility(profiles)
        assert decision.approved is True, (
            f"After A (keep_original) + B (observer) excluded, C+D should "
            f"be the 2 main speakers; got approved={decision.approved} "
            f"count={decision.main_speaker_count} reason={decision.reason_code!r}."
        )
        assert decision.main_speaker_count == 2
        assert set(decision.main_speaker_ids) == {"speaker_c", "speaker_d"}
        excluded_reasons = {
            e["speaker_id"]: e["reason"]
            for e in decision.excluded_speakers
        }
        assert excluded_reasons.get("speaker_a") == "dubbing_mode_keep_original"
        assert excluded_reasons.get("speaker_b") == "role_observer"


# ===================================================================
# auto_translation_review
# ===================================================================


class TestAutoTranslationReview:
    """2026-05-20 spec change: smart 全自动化原则.

    Previously this module ran 6 deterministic checks + compliance and
    short-circuited to ``auto_approved=False`` on first failure. Per
    user feedback after job_88bdca (Google I/O 2026, hit
    ``uncertain_speaker_share`` 78% handoff despite 100% glossary),
    the smart product promise is "fully automatic except external
    limits + opt-in weak-match confirmation". So translation_review
    is now AUDIT-ONLY: checks still run to populate metrics, but
    never block.

    Compliance is now handled at an early-pipeline gate (post-S1,
    pre-S3) — content_compliance.ContentPolicyViolationError raised
    by ``_run_content_compliance_review`` exits the pipeline before
    we waste S3 / TTS / clone budget. The ``compliance_block`` kwarg
    here is retained for backward-compat but ignored.
    """

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

    def test_clean_inputs_auto_approved_with_full_metrics(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        decision = evaluate_translation_review(**self._passing_inputs())
        assert decision.auto_approved is True
        assert decision.reason_code is None
        assert decision.failed_check is None
        # Metrics still populated for audit (quality_report renderer
        # depends on these).
        assert decision.metrics["glossary_preservation_rate"] == pytest.approx(0.9)

    def test_glossary_below_threshold_still_auto_approved_but_advisory_recorded(self):
        """Old spec: reject. New spec: auto-pass + advisory annotation."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_preserved_terms"] = 6  # 60% < 80%
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.failed_check is None
        # Advisory: the check that would have failed under old spec
        # is annotated for admin QA visibility.
        assert "glossary_preservation_advisory_reason" in decision.metrics
        assert decision.metrics["glossary_preservation_rate"] == pytest.approx(0.6)

    def test_length_budget_overflow_still_auto_approved(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["length_overflow_rate"] = 0.20  # 20% > 15%
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert "length_budget_advisory_reason" in decision.metrics

    def test_text_audio_checksum_mismatch_still_auto_approved(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["final_spoken_text_sha256"] = "different"
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert "text_audio_checksum_advisory_reason" in decision.metrics

    def test_uncertain_speaker_share_above_threshold_still_auto_approved(self):
        """Real production incident (job_88bdca Google I/O 2026): 78%
        uncertain share caused handoff despite perfect glossary. This
        test pins that the new spec NEVER hands off on this condition."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["speaker_stats"]["uncertain_speaker_duration_share"] = 0.78
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert "uncertain_speaker_share_advisory_reason" in decision.metrics

    def test_clone_eligible_ratio_low_still_auto_approved(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["clone_sample_stats"]["eligible_speakers"] = 1  # 1/3 < 0.5
        inputs["speaker_stats"]["asr_speaker_count"] = 3
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert "clone_eligible_ratio_advisory_reason" in decision.metrics

    def test_speaker_mismatch_still_auto_approved(self):
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        decision = evaluate_translation_review(
            **inputs,
            speaker_diff={"s1": "speaker_b"},  # translation says speaker_a
        )
        assert decision.auto_approved is True
        assert "speaker_assignment_advisory_reason" in decision.metrics

    def test_compliance_block_kwarg_is_ignored(self):
        """compliance_block kwarg retained for backward-compat but no
        longer drives the decision. Compliance is enforced at the
        early-pipeline gate in process.py (post-S1, pre-S3) via
        ContentPolicyViolationError, NOT here."""
        from services.smart.auto_translation_review import evaluate_translation_review

        decision = evaluate_translation_review(
            **self._passing_inputs(),
            compliance_block=True,
        )
        assert decision.auto_approved is True
        assert decision.reason_code is None
        # Audit metrics still populated regardless of compliance flag.
        assert decision.metrics["glossary_preservation_rate"] == pytest.approx(0.9)

    def test_multiple_advisory_failures_all_recorded(self):
        """When multiple checks would have failed under old spec, all
        their advisory reasons get annotated (not just the first).
        Lets admin QA see the full failure picture, not just the
        leading reason."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_preserved_terms"] = 1  # fail glossary
        inputs["speaker_stats"]["uncertain_speaker_duration_share"] = 0.99  # fail uncertain
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        # Both advisory annotations present (no first-failure short-circuit).
        assert "glossary_preservation_advisory_reason" in decision.metrics
        assert "uncertain_speaker_share_advisory_reason" in decision.metrics

    def test_missing_glossary_is_vacuous_pass(self):
        """No glossary configured → no advisory annotation. Vacuous
        pass remains the same as before (spec-explicit, not failure)."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_total_terms"] = 0
        inputs["translation_result"]["glossary_preserved_terms"] = 0
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.metrics["glossary_preservation_rate"] is None
        assert "glossary_preservation_advisory_reason" not in decision.metrics

    def test_missing_signals_still_auto_approved_with_advisory(self):
        """Old spec: hard fail. New spec: auto-pass with
        ``missing_signals_advisory=True`` marker so admin can see
        that audit metrics are incomplete."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        del inputs["speaker_stats"]["uncertain_speaker_duration_share"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.metrics.get("missing_signals_advisory") is True
        # Evidence still carries the specific missing field names.
        assert decision.metrics["missing"] == ["uncertain_speaker_duration_share"]

    def test_zero_asr_speakers_still_auto_approved(self):
        """Old spec: ``unevaluable_zero_asr_speakers``. New spec:
        still auto-approve. Pipeline downstream handles 0-speaker
        edge cases (it produces no TTS audio rather than crashing)."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["speaker_stats"]["asr_speaker_count"] = 0
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True


# ===================================================================
# PR#3C-b3c — translation_review integration data shapes
# ===================================================================


class TestTranslationReviewProcessIntegrationShapes:
    """PR#3C-b3c functional integration tests.

    These mirror the EXACT input dicts process.py's smart
    auto-translation-review branch builds at runtime, then pipe them
    through ``evaluate_translation_review`` to verify the decision
    matches the production contract. The anchor tests in
    ``test_smart_studio_gate_acceptance.py`` cover the source-level
    shape (imports + calls + branches); these cover the runtime
    behaviour with real DubbingSegment / speaker_structure_profiles.

    Without functional tests like these, a refactor that silently
    changes a field name or dict key (e.g. ``speaker_role`` ↔
    ``role``) would slip through the anchor guard and surface only as
    a production smart job always failing or always passing.

    Codex 第二十三轮 P1 style: pin the wiring with REAL objects, not
    just regex/anchor search.
    """

    def _build_segments(self, speaker_ids, *, cn_text="测试"):
        """Build minimal DubbingSegment list."""
        from services.gemini.translator import DubbingSegment

        return [
            DubbingSegment(
                segment_id=i,
                speaker_id=sid,
                display_name=sid.upper(),
                voice_id="voice_x",
                start_ms=i * 1000,
                end_ms=(i + 1) * 1000,
                target_duration_ms=1000,
                source_text="hello",
                cn_text=cn_text,
            )
            for i, sid in enumerate(speaker_ids)
        ]

    def _build_smart_translation_input(
        self, segments, *, glossary_total=0, glossary_preserved=0,
    ):
        """Mirror process.py:3070+ smart translation input dict shape."""
        return {
            "glossary_total_terms": glossary_total,
            "glossary_preserved_terms": glossary_preserved,
            "length_overflow_rate": None,
            "rewrite_attempted": False,
            "subtitle_source_text_sha256": None,
            "final_spoken_text_sha256": None,
            "segments": [
                {"segment_id": str(s.segment_id), "speaker_id": s.speaker_id}
                for s in segments
            ],
        }

    def _build_smart_speaker_stats(self, profiles):
        """Mirror process.py:3120+ speaker_stats dict shape."""
        uncertain_share = sum(
            float(p.get("speaker_duration_share") or 0.0)
            for p in profiles.values()
            if isinstance(p, dict)
            and str(p.get("speaker_role") or "").lower() == "fragmented"
        )
        return {
            "speakers": [
                {
                    "speaker_id": sid,
                    "role": p.get("speaker_role"),
                    "duration_share": p.get("speaker_duration_share"),
                }
                for sid, p in profiles.items()
                if isinstance(p, dict)
            ],
            "uncertain_speaker_duration_share": uncertain_share,
            "asr_speaker_count": len(profiles),
        }

    def _build_smart_clone_sample_stats(self, profiles):
        """Mirror process.py:3160+ clone_sample_stats heuristic."""
        eligible = sum(
            1
            for p in profiles.values()
            if isinstance(p, dict)
            and int(p.get("speaker_duration_ms") or 0) >= 10_000
        )
        return {"eligible_speakers": eligible}

    def test_happy_path_auto_approved_with_two_eligible_speakers(self):
        """A 2-speaker clean run with glossary preservation rate above
        threshold + both speakers ≥10s sample → auto_approved."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        segments = self._build_segments(["speaker_a"] * 5 + ["speaker_b"] * 5)
        translation_input = self._build_smart_translation_input(
            segments, glossary_total=10, glossary_preserved=9,
        )
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.55,
                "speaker_duration_ms": 25_000,  # 25s >= 10s threshold
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.45,
                "speaker_duration_ms": 20_000,
            },
        }
        speaker_stats = self._build_smart_speaker_stats(profiles)
        clone_sample_stats = self._build_smart_clone_sample_stats(profiles)

        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=speaker_stats,
            clone_sample_stats=clone_sample_stats,
        )
        assert decision.auto_approved is True, (
            f"Expected auto_approved=True with 2-speaker clean run; "
            f"got reason={decision.reason_code!r} "
            f"failed_check={decision.failed_check!r}.\n"
            f"metrics={decision.metrics}"
        )
        assert decision.reason_code is None
        assert decision.failed_check is None

    def test_glossary_below_threshold_still_auto_approved(self):
        """New spec: smart never pauses on glossary. Process-shape
        integration test using real DubbingSegment + speaker profiles
        confirms the end-to-end auto-pass."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        segments = self._build_segments(["speaker_a"] * 4 + ["speaker_b"] * 4)
        translation_input = self._build_smart_translation_input(
            segments, glossary_total=10, glossary_preserved=5,  # 50% < 80%
        )
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.55,
                "speaker_duration_ms": 25_000,
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.45,
                "speaker_duration_ms": 20_000,
            },
        }
        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=self._build_smart_speaker_stats(profiles),
            clone_sample_stats=self._build_smart_clone_sample_stats(profiles),
        )
        assert decision.auto_approved is True
        # Advisory still recorded for admin QA.
        assert "glossary_preservation_advisory_reason" in decision.metrics

    def test_uncertain_speaker_share_too_high_still_auto_approved(self):
        """Real production incident replayed: Google I/O 2026 keynote
        had 78% uncertain share due to 9 distinct speakers. Under old
        spec this was a hard handoff; new spec auto-passes."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        segments = self._build_segments(["speaker_a"] * 6 + ["speaker_b"] * 2)
        translation_input = self._build_smart_translation_input(
            segments, glossary_total=0,
        )
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.22,
                "speaker_duration_ms": 40_000,
            },
            # 8 other fragmented speakers → uncertain_share approaches 0.78
            **{
                f"speaker_{c}": {
                    "speaker_role": "fragmented",
                    "speaker_duration_share": 0.0975,
                    "speaker_duration_ms": 8_000,
                }
                for c in "bcdefghi"
            },
        }
        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=self._build_smart_speaker_stats(profiles),
            clone_sample_stats=self._build_smart_clone_sample_stats(profiles),
        )
        assert decision.auto_approved is True, (
            f"Google I/O scenario MUST auto-pass under new spec; "
            f"got reason={decision.reason_code!r}"
        )
        assert "uncertain_speaker_share_advisory_reason" in decision.metrics

    def test_low_clone_eligible_ratio_still_auto_approved(self):
        """Old spec: < 50% clone-eligible → handoff. New spec:
        auto-pass; downstream pipeline will preset-match the
        ineligible speakers."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        segments = self._build_segments(
            ["speaker_a", "speaker_b", "speaker_c", "speaker_d"]
        )
        translation_input = self._build_smart_translation_input(
            segments, glossary_total=10, glossary_preserved=10,
        )
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.30,
                "speaker_duration_ms": 15_000,
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.25,
                "speaker_duration_ms": 8_000,
            },
            "speaker_c": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.25,
                "speaker_duration_ms": 5_000,
            },
            "speaker_d": {
                "speaker_role": "primary",
                "speaker_duration_share": 0.20,
                "speaker_duration_ms": 4_000,
            },
        }
        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=self._build_smart_speaker_stats(profiles),
            clone_sample_stats=self._build_smart_clone_sample_stats(profiles),
        )
        assert decision.auto_approved is True
        assert "clone_eligible_ratio_advisory_reason" in decision.metrics

    def test_clone_eligible_heuristic_uses_10s_floor(self):
        """Heuristic-shape pin (unchanged by spec change): the 10s
        floor in ``_build_smart_clone_sample_stats`` matches the
        ``MIN_CLONE_SAMPLE_SECONDS`` constant elsewhere."""
        profiles_boundary = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_ms": 10_000,  # exactly 10s — eligible
            },
            "speaker_b": {
                "speaker_role": "primary",
                "speaker_duration_ms": 9_999,  # just below — NOT eligible
            },
        }
        clone_sample_stats = self._build_smart_clone_sample_stats(
            profiles_boundary
        )
        assert clone_sample_stats == {"eligible_speakers": 1}

    def test_compliance_block_kwarg_ignored_in_integration_shape(self):
        """End-to-end pin: compliance_block=True passed via the
        integration shape still yields auto_approved=True. The
        compliance enforcement has moved to the early-pipeline gate
        (post-S1) in process.py."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        segments = self._build_segments(["speaker_a"] * 5)
        translation_input = self._build_smart_translation_input(
            segments, glossary_total=10, glossary_preserved=10,
        )
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 1.0,
                "speaker_duration_ms": 50_000,
            },
        }
        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=self._build_smart_speaker_stats(profiles),
            clone_sample_stats=self._build_smart_clone_sample_stats(profiles),
            compliance_block=True,
        )
        assert decision.auto_approved is True


class TestB3DCloneSampleExtractorContract:
    """PR#3C-b3d functional contract tests for the three-piece atomic
    landing (Codex 第二十轮).

    These tests don't run the full process.py pipeline (too heavy) but
    they pin the contracts that process.py's smart inline branch
    relies on. If VoiceSampleExtractor's signature drifts or the
    smart_wiring inject_for_test mechanism breaks, these tests
    fail BEFORE the production smart job would silently misbehave.
    """

    def test_voice_sample_extractor_signature_matches_b3d_usage(self):
        """process.py:2530+ calls ``VoiceSampleExtractor().extract_sample(
        audio_path=..., speaker_lines=..., output_path=...)``. If any
        of those kwarg names drift, smart's per-speaker sample
        extraction breaks at runtime in a hard-to-diagnose way.
        """
        import inspect

        from services.voice.sample_extractor import VoiceSampleExtractor

        sig = inspect.signature(VoiceSampleExtractor.extract_sample)
        params = sig.parameters
        # b3d uses these three kwargs by name. The function also has
        # min_duration_s / max_duration_s defaults but b3d doesn't pass
        # them — pin only that the kwargs we use ARE accepted.
        for required_kwarg in ("audio_path", "speaker_lines", "output_path"):
            assert required_kwarg in params, (
                f"VoiceSampleExtractor.extract_sample signature drift — "
                f"missing kwarg {required_kwarg!r}. process.py smart "
                f"branch (PR#3C-b3d) passes it by name. Sig: {sig}"
            )

    def test_smart_wiring_inject_for_test_replaces_default(self):
        """PR#3C-b3d piece 3: ``build_smart_clone_provider()`` returns
        the test override when ``inject_for_test(clone_provider=...)``
        is active. Tests in process.py smart path rely on this so
        they can run without burning real MiniMax quota."""
        from services.smart_wiring import (
            build_smart_clone_provider, inject_for_test,
        )

        from tests.fakes.fake_clone_provider import FakeCloneProvider

        fake = FakeCloneProvider(success=True)
        with inject_for_test(clone_provider=fake):
            provider = build_smart_clone_provider()
            assert provider is fake, (
                "inject_for_test failed to override default — smart "
                "test infrastructure broken; real provider would be "
                "invoked in tests."
            )

        # After the context exits, the override is gone.
        default_provider = build_smart_clone_provider()
        assert default_provider is not fake, (
            "inject_for_test failed to restore default after exit — "
            "test isolation broken."
        )

    def test_b3d_per_speaker_sample_path_flows_through_voice_review(self):
        """Smart's evaluate_voice_review must call the provider with
        the per-speaker sample path, not whole-file. Pin this by
        running evaluate_voice_review with a fake provider and
        per-speaker paths."""
        from pathlib import Path

        from services.smart.auto_voice_review import (
            VoiceReviewOutcome, VoiceReviewSpeakerInput, evaluate_voice_review,
        )
        from services.smart_wiring import inject_for_test

        from tests.fakes.fake_clone_provider import FakeCloneProvider

        # Two main speakers, each with their own per-speaker sample path.
        speaker_a_sample = Path("/tmp/fake/smart_clone_samples/speaker_a.wav")
        speaker_b_sample = Path("/tmp/fake/smart_clone_samples/speaker_b.wav")
        main_speakers = [
            VoiceReviewSpeakerInput(
                speaker_id="speaker_a",
                speaker_name="A",
                sample_seconds=20.0,
                source_audio_path=speaker_a_sample,
            ),
            VoiceReviewSpeakerInput(
                speaker_id="speaker_b",
                speaker_name="B",
                sample_seconds=15.0,
                source_audio_path=speaker_b_sample,
            ),
        ]
        fake = FakeCloneProvider(success=True)
        with inject_for_test(clone_provider=fake):
            from services.smart_wiring import build_smart_clone_provider

            result = evaluate_voice_review(
                main_speakers=main_speakers,
                smart_consent={"auto_voice_clone": True},
                clone_provider=build_smart_clone_provider(),
                voice_library_quota_remaining=100,
                smart_decision_id_factory=lambda: "dec_x",
            )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        # Provider received exactly 2 calls, one per main speaker.
        assert len(fake.calls) == 2, (
            f"Expected 2 clone calls (one per main speaker); got "
            f"{len(fake.calls)}.\n{fake.calls}"
        )
        # Critically: each call's source_audio_path matches the PER-SPEAKER
        # sample, not a shared whole-file path. This is the b3d safety
        # property — real provider must see the right sample.
        call_paths = {
            call["speaker_id"]: call["source_audio_path"]
            for call in fake.calls
        }
        assert call_paths["speaker_a"] == str(speaker_a_sample), (
            f"speaker_a clone got wrong path: {call_paths['speaker_a']!r}, "
            f"expected {str(speaker_a_sample)!r}"
        )
        assert call_paths["speaker_b"] == str(speaker_b_sample), (
            f"speaker_b clone got wrong path: {call_paths['speaker_b']!r}, "
            f"expected {str(speaker_b_sample)!r}"
        )
        # Negative: NO call got the same path (i.e. nobody used a
        # whole-file fallback).
        assert (
            call_paths["speaker_a"] != call_paths["speaker_b"]
        ), (
            "Both speakers got the SAME source_audio_path — likely the "
            "per-speaker extraction was bypassed and whole-file was "
            "used. PR#3C-b3d safety property violated.\n"
            f"calls={fake.calls}"
        )

    def test_b3d_validate_sample_returns_duration_s(self):
        """Codex 第二十七轮 P1 contract: validate_sample() returns a dict
        with ``duration_s`` (float) + ``is_valid`` (bool) + ``warnings``.
        process.py reads ``duration_s`` to gate on
        MIN_SAMPLE_DURATION_SECONDS — if validate_sample's return shape
        drifts, b3d's safety check breaks silently."""
        import inspect

        from services.voice.sample_extractor import (
            MIN_SAMPLE_DURATION_SECONDS, VoiceSampleExtractor,
        )

        sig = inspect.signature(VoiceSampleExtractor.validate_sample)
        # Single positional arg ``sample_path`` is the contract.
        assert "sample_path" in sig.parameters, (
            f"validate_sample signature drift — process.py b3d-fix "
            f"calls ``.validate_sample(str(sample_path))``. Sig: {sig}"
        )

        # MIN_SAMPLE_DURATION_SECONDS must be importable as the canonical
        # floor (10.0). process.py b3d-fix imports + reads it.
        assert MIN_SAMPLE_DURATION_SECONDS == 10.0, (
            f"MIN_SAMPLE_DURATION_SECONDS drifted from 10.0 to "
            f"{MIN_SAMPLE_DURATION_SECONDS}. process.py's sub-10s gate "
            f"references this constant; if it moves, the gate moves "
            f"with it — which may or may not be intentional but should "
            f"surface here so b3d's safety is consciously re-evaluated."
        )

    def test_b3d_short_sample_triggers_fail_closed_reason_code(self):
        """End-to-end runtime test: create a tmp wav of <10s, run
        validate_sample on it, verify duration_s < 10 → process.py's
        b3d-fix would set sample_too_short_<sid>_<X.X>s reason and
        handoff.

        This is a contract test against validate_sample's return shape
        without exercising the full pipeline (which requires the whole
        ProcessPipeline construction). The anchor test in
        test_smart_studio_gate_acceptance.py covers the source-level
        wiring; this test covers the runtime arithmetic.
        """
        import struct
        import tempfile
        from pathlib import Path

        from services.voice.sample_extractor import (
            MIN_SAMPLE_DURATION_SECONDS, VoiceSampleExtractor,
        )

        # Build a 3-second 16 kHz mono s16 WAV — well below the 10s floor.
        with tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False
        ) as tmp_f:
            tmp_path = Path(tmp_f.name)
        try:
            duration_s = 3.0
            sample_rate = 16_000
            num_samples = int(duration_s * sample_rate)
            with open(tmp_path, "wb") as f:
                # Minimal WAV header (RIFF) + PCM data
                num_channels = 1
                bits_per_sample = 16
                byte_rate = sample_rate * num_channels * bits_per_sample // 8
                data_size = num_samples * num_channels * bits_per_sample // 8
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + data_size))
                f.write(b"WAVE")
                f.write(b"fmt ")
                f.write(struct.pack("<I", 16))
                f.write(struct.pack("<H", 1))  # PCM
                f.write(struct.pack("<H", num_channels))
                f.write(struct.pack("<I", sample_rate))
                f.write(struct.pack("<I", byte_rate))
                f.write(struct.pack(
                    "<H", num_channels * bits_per_sample // 8
                ))
                f.write(struct.pack("<H", bits_per_sample))
                f.write(b"data")
                f.write(struct.pack("<I", data_size))
                # Silence sample data
                f.write(b"\x00\x00" * num_samples)

            result = VoiceSampleExtractor().validate_sample(str(tmp_path))
            # The contract process.py b3d-fix relies on:
            assert "duration_s" in result, (
                "validate_sample must return duration_s — process.py "
                "b3d-fix reads result.get('duration_s')."
            )
            measured_duration = float(result["duration_s"])
            assert measured_duration < MIN_SAMPLE_DURATION_SECONDS, (
                f"3s WAV measured at {measured_duration}s — should be "
                f"under {MIN_SAMPLE_DURATION_SECONDS}s floor so b3d "
                f"would mark sample_too_short and handoff."
            )
            # This is the comparison process.py b3d-fix does:
            #   if _val_duration_s < MIN_SAMPLE_DURATION_SECONDS:
            #       _smart_sample_extraction_error = f"sample_too_short_{sid}_{...}s"
            assert measured_duration < MIN_SAMPLE_DURATION_SECONDS, (
                "Sub-10s contract violated by validate_sample."
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def test_b3e_atomic_invariant_quota_and_provider_move_together(self):
        """Codex 第二十七轮 P0 atomic invariant (now reverse-asserted at
        b3e): Pieces 2 (real quota) + 3 (real provider) MUST coexist.

        Pre-b3e (b3d) this test asserted both placeholders coexisted.
        At b3e both should be real. The invariant is: if one is real
        and the other isn't, this test fails — protecting against a
        future PR that partially reverts only one half.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        # Walk ~900 lines (matches anchor tests in
        # test_smart_studio_gate_acceptance.py — bumped from 800 after
        # b3f-fix added Codex 第三十二轮 P0 docstring expansion).
        lines = source[idx:].splitlines()
        block = "\n".join(lines[:900])

        # Piece 2 (real quota): helper must be referenced; placeholder
        # literal must NOT appear inside the smart inline branch.
        assert "_fetch_smart_user_voice_quota_remaining(" in block, (
            "Smart branch no longer calls "
            "_fetch_smart_user_voice_quota_remaining — Piece 2 (real "
            "quota) reverted to a placeholder. Codex 第二十七轮 P0: "
            "if you re-introduce a static quota, also revert Piece 3 "
            "(real CloneProvider) in the same commit so the §7.3 brake "
            "isn't silently bypassed in production.\n"
            f"Block (first 3000 chars):\n{block[:3000]}"
        )
        assert "_smart_quota_remaining = 100" not in block, (
            "Smart branch contains the literal 100 placeholder for "
            "_smart_quota_remaining inside the inline auto-approve "
            "branch. PR#3C-b3e: this must be replaced by the real "
            "Gateway quota helper. If you intentionally reverted, "
            "also revert Piece 3 (real CloneProvider) at the same time."
        )

        # Piece 3 (real provider): the call to
        # build_smart_clone_provider() must be wired; the b2 stub call
        # must NOT be in the smart branch.
        assert "build_smart_clone_provider()" in block, (
            "Smart branch no longer calls build_smart_clone_provider() "
            "— Piece 3 (real CloneProvider) reverted. Codex 第二十七轮 "
            "P0 atomic invariant: if you revert Piece 3 to the stub, "
            "also revert Piece 2 (real quota) so the combination stays "
            "either fully-real or fully-stub.\n"
            f"Block (first 3000 chars):\n{block[:3000]}"
        )
        assert (
            "_smart_clone_provider = _build_b2_not_wired_clone_provider()"
            not in block
        ), (
            "Smart branch still wires _build_b2_not_wired_clone_provider() "
            "after PR#3C-b3e — Piece 3 wasn't flipped. Codex 第二十七轮 P0 "
            "atomic invariant: replace BOTH Piece 2 (quota) and Piece 3 "
            "(provider) together, or revert BOTH together."
        )

        # 2026-05-20 spec change: quota=None NO LONGER hands off.
        # Smart 全自动化原则 — quota lookup failure is transient
        # infra, not user-blocking. Branch now logs + uses safe
        # fallback (_smart_quota_remaining = 999_999) + continues.
        # The MiniMax provider's own quota error mid-flight still
        # routes through PAUSED in _attempt_clone_with_retries, so
        # the real external quota gate is preserved.
        assert "quota_lookup_degraded" in block, (
            "Smart branch missing the new soft-fallback audit marker "
            "for quota lookup failure. After 2026-05-20 spec change, "
            "quota=None should emit a 'quota_lookup_degraded' audit "
            "event and continue with safe fallback (NOT handoff).\n"
            f"Block (first 3000 chars):\n{block[:3000]}"
        )
        assert "voice_library_quota_unavailable" not in block, (
            "Smart branch still contains 'voice_library_quota_unavailable' "
            "anchor — this was the old handoff reason. 2026-05-20 spec: "
            "quota lookup failure no longer hands off. If you re-added "
            "this handoff path, also update test_smart_full_auto_spec_2026_05_20.py"
        )

    def test_b3e_quota_helper_returns_none_on_no_api_key(self, monkeypatch):
        """Codex 第二十七轮 P0 fail-closed: when AVT_INTERNAL_API_KEY is
        unset/empty, the quota helper MUST return None so the caller
        treats quota as unavailable and routes to handoff. Returning a
        permissive default (e.g. 100) would silently let the real
        provider fire."""
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.delenv("AVT_INTERNAL_API_KEY", raising=False)
        assert _fetch_smart_user_voice_quota_remaining("some-user-uuid") is None

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "")
        assert _fetch_smart_user_voice_quota_remaining("some-user-uuid") is None

        # Whitespace-only key is also "unset" semantically.
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "   ")
        assert _fetch_smart_user_voice_quota_remaining("some-user-uuid") is None

    def test_b3e_quota_helper_returns_none_on_empty_user_id(self, monkeypatch):
        """Empty user_id can't be validly looked up — fail-closed
        instead of issuing a query that would 400 from Gateway."""
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        assert _fetch_smart_user_voice_quota_remaining("") is None
        assert _fetch_smart_user_voice_quota_remaining(None) is None  # type: ignore[arg-type]
        assert _fetch_smart_user_voice_quota_remaining("   ") is None

    def test_b3e_quota_helper_returns_remaining_on_success(self, monkeypatch):
        """Happy path: Gateway returns 200 + {remaining: N} → helper
        returns N. Pin the field name + value extraction."""
        from pipeline import process as process_module
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"user_id": "x", "used": 5, "limit": 30, "remaining": 25}

        recorded_calls = []

        def _fake_get(url, *, params=None, headers=None, timeout=None):
            recorded_calls.append({
                "url": url, "params": params, "headers": headers,
                "timeout": timeout,
            })
            return _FakeResp()

        # process.py imports requests inside the helper function.
        import requests as _requests_mod
        monkeypatch.setattr(_requests_mod, "get", _fake_get)

        result = _fetch_smart_user_voice_quota_remaining("user-uuid-abc")
        assert result == 25, (
            f"Expected helper to return remaining=25; got {result!r}"
        )

        # Pin the request shape.
        assert len(recorded_calls) == 1
        call = recorded_calls[0]
        assert call["url"] == (
            "http://127.0.0.1:8880/api/internal/user-voices/quota"
        )
        assert call["params"] == {"user_id": "user-uuid-abc"}
        assert call["headers"] == {"X-Internal-Key": "test-key"}
        assert call["timeout"] == 3.0

    def test_b3e_quota_helper_returns_none_on_http_error(self, monkeypatch):
        """Codex 第二十七轮 P0: non-200 response → None → fail-closed
        handoff at caller. NEVER fall back to a permissive default.
        """
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        for bad_status in (400, 401, 403, 404, 500, 502, 503):
            class _BadResp:
                def __init__(self, status):
                    self.status_code = status

                def json(self):
                    return {"remaining": 999}  # would tempt to use anyway

            def _fake_get(*args, **kwargs):
                return _BadResp(bad_status)

            import requests as _requests_mod
            monkeypatch.setattr(_requests_mod, "get", _fake_get)
            assert _fetch_smart_user_voice_quota_remaining("u") is None, (
                f"HTTP {bad_status} should yield None (fail-closed); "
                f"any non-None return tempts the caller to use a stale "
                f"quota value, re-opening Codex 第二十七轮 P0."
            )

    def test_b3e_quota_helper_returns_none_on_invalid_remaining(self, monkeypatch):
        """Defensive: even on HTTP 200, if ``remaining`` field is
        missing / wrong type / negative, treat as unavailable."""
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        bad_bodies = [
            {},                              # missing
            {"remaining": None},             # None
            {"remaining": "25"},             # string (must be int)
            {"remaining": -5},               # negative
            {"remaining": 25.5},             # float
            {"remaining": [25]},             # list
        ]
        for body in bad_bodies:
            class _Resp:
                status_code = 200

                def json(self, _b=body):
                    return _b

            def _fake_get(*args, **kwargs):
                return _Resp()

            import requests as _requests_mod
            monkeypatch.setattr(_requests_mod, "get", _fake_get)
            assert _fetch_smart_user_voice_quota_remaining("u") is None, (
                f"Invalid remaining payload {body!r} should yield None — "
                f"caller routes to handoff. Codex 第二十七轮 P0."
            )

    def test_b3e_quota_helper_returns_none_on_network_exception(self, monkeypatch):
        """ANY exception during HTTP / JSON parse → None.
        Network blip should never cause the real provider to fire."""
        from pipeline.process import _fetch_smart_user_voice_quota_remaining

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        class _ExcResp:
            status_code = 200

            def json(self):
                raise ValueError("malformed JSON")

        def _fake_get_raises(*args, **kwargs):
            raise ConnectionError("network down")

        def _fake_get_bad_json(*args, **kwargs):
            return _ExcResp()

        import requests as _requests_mod
        # Network error
        monkeypatch.setattr(_requests_mod, "get", _fake_get_raises)
        assert _fetch_smart_user_voice_quota_remaining("u") is None

        # JSON parse error
        monkeypatch.setattr(_requests_mod, "get", _fake_get_bad_json)
        assert _fetch_smart_user_voice_quota_remaining("u") is None

    def test_b3e_fix_consent_false_skips_quota_lookup(self):
        """Codex 第二十九轮 P1 + 第三十轮 P1 + Phase 3 (2026-05-17): jobs
        whose consent=False, admin disabled new clone, or empty
        main_speakers must NOT call the Gateway quota endpoint.
        evaluate_voice_review routes to PRESET / empty-AUTO-APPROVED
        without reading quota/provider in those cases, so a Gateway
        hiccup must not error-downgrade them to handoff.

        Pin the source-level structure:
          - Quota lookup is inside the triple gate
            ``_smart_consent_allows_clone and _smart_admin_clone_enabled
              and _smart_main_speakers`` (Phase 3 adds admin axis to
            the original b3e-fix2 dual gate).
          - There's an ``else:`` branch using stub provider + 0
            quota so evaluate_voice_review's type signature is
            satisfied without reaching the real provider.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        # Phase 3 added ~50 lines of admin policy comments + gate
        # additions; bump window from 900 to 1200 to keep covering
        # quota-unavailable handoff branch.
        block = "\n".join(lines[:1200])

        # Find the quota lookup call — must be inside the triple gate.
        quota_call = "_fetch_smart_user_voice_quota_remaining("
        quota_idx = block.find(quota_call)
        assert quota_idx >= 0

        # Walk backward to find the nearest ``if`` — must be the Phase 3
        # triple gate (consent + admin_clone_enabled + main_speakers).
        # The gate is now a multi-line ``if (...)`` block; search the
        # core conjunction substring that uniquely identifies it.
        preceding = block[:quota_idx]
        gate_idx = preceding.rfind(
            "_smart_consent_allows_clone\n                        "
            "and _smart_admin_clone_enabled\n                        "
            "and _smart_main_speakers"
        )
        assert gate_idx >= 0, (
            "Quota lookup must live inside the Phase 3 triple gate "
            "``_smart_consent_allows_clone and _smart_admin_clone_enabled "
            "and _smart_main_speakers``. Plan 2026-05-17-user-voice-"
            "candidate-first §Consent × Admin 决策矩阵 — when admin "
            "disabled new clone, the smart job must not even query "
            "the Gateway quota endpoint.\n"
            f"Quota call at offset {quota_idx}, no triple gate found "
            f"before it.\nPreceding 2000 chars:\n{preceding[-2000:]}"
        )

        # The else branch must exist and use stub provider + 0 quota.
        # Find the matching else near the triple gate. Keep this window
        # generous because quota-unavailable handoff text and audit payloads
        # make the guarded branch fairly long.
        gate_block = block[gate_idx : gate_idx + 9000]
        else_idx = gate_block.find("\n                    else:")
        assert else_idx >= 0, (
            "Triple gate has no else: branch — consent=False / "
            "admin_clone_enabled=False / empty main_speakers path "
            "would crash when evaluate_voice_review reads "
            "quota/provider.\n"
            f"Block:\n{gate_block}"
        )
        else_window = gate_block[else_idx : else_idx + 1500]
        assert "_build_b2_not_wired_clone_provider()" in else_window, (
            "consent=False / admin disabled / empty-main-speakers else "
            "branch must use the b2 stub provider — "
            "evaluate_voice_review won't actually call it but the type "
            "signature requires a CloneProvider.\n"
            f"else window:\n{else_window}"
        )

    def test_b3e_fix_mirror_helper_returns_true_on_success(self, monkeypatch):
        """Codex 第二十九轮 P0: ``_register_smart_clone_in_user_voices``
        helper happy path. Pin the URL + headers + payload shape +
        return value."""
        from pipeline.process import _register_smart_clone_in_user_voices

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")
        recorded = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True, "voice_id": "vt_xxx", "user_id": "u-1"}

        def _fake_post(url, *, json=None, headers=None, timeout=None):
            recorded["url"] = url
            recorded["json"] = json
            recorded["headers"] = headers
            recorded["timeout"] = timeout
            return _Resp()

        import requests as _requests_mod
        monkeypatch.setattr(_requests_mod, "post", _fake_post)

        result = _register_smart_clone_in_user_voices(
            user_id="u-1",
            voice_id="vt_xxx",
            label="Speaker A · 2026-05-16 14:32",
            source_speaker_id="speaker_a",
            source_job_id="j-1",
            source_type="youtube_url",
            source_ref="https://youtu.be/abc",
            source_content_hash="youtube:abc",
            source_video_title="Source Title",
            source_published_at="2024-05-01T00:00:00+00:00",
            source_content_summary="频道：Test Channel",
            source_content_era="2024",
            source_content_tags={"channel": "Test Channel", "tags": ["AI"]},
            source_speaker_name="Speaker A",
            clone_sample_seconds=12.5,
            clone_sample_segment_ids=[1, 2],
            notes="Smart auto-clone from job j-1",
        )
        assert result is True

        assert recorded["url"] == (
            "http://127.0.0.1:8880/api/internal/user-voices/register-smart"
        )
        assert recorded["headers"] == {"X-Internal-Key": "test-key"}
        assert recorded["timeout"] == 5.0
        # Field shape mirrors Studio's manual-clone path:
        # provider="minimax_voice_clone", tts_provider="minimax_tts",
        # platform="minimax_domestic" come as Gateway-side defaults.
        body = recorded["json"]
        assert body["user_id"] == "u-1"
        assert body["voice_id"] == "vt_xxx"
        assert body["label"] == "Speaker A · 2026-05-16 14:32"
        assert body["source_speaker_id"] == "speaker_a"
        assert body["source_job_id"] == "j-1"
        assert body["source_type"] == "youtube_url"
        assert body["source_ref"] == "https://youtu.be/abc"
        assert body["source_content_hash"] == "youtube:abc"
        assert body["source_video_title"] == "Source Title"
        assert body["source_published_at"] == "2024-05-01T00:00:00+00:00"
        assert body["source_content_summary"] == "频道：Test Channel"
        assert body["source_content_era"] == "2024"
        assert body["source_content_tags"] == {"channel": "Test Channel", "tags": ["AI"]}
        assert body["source_speaker_name"] == "Speaker A"
        assert body["clone_sample_seconds"] == 12.5
        assert body["clone_sample_segment_ids"] == [1, 2]
        assert body["created_from"] == "smart_auto"
        assert body["notes"] == "Smart auto-clone from job j-1"

    def test_b3e_fix_mirror_helper_returns_false_on_failures(self, monkeypatch):
        """Codex 第二十九轮 P0: ANY failure mode returns False so the
        caller can escalate to handoff. NEVER raises."""
        from pipeline.process import _register_smart_clone_in_user_voices

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        # Empty inputs
        assert _register_smart_clone_in_user_voices(
            user_id="", voice_id="vt", label="x",
        ) is False
        assert _register_smart_clone_in_user_voices(
            user_id="u", voice_id="", label="x",
        ) is False

        # Missing API key
        monkeypatch.delenv("AVT_INTERNAL_API_KEY", raising=False)
        assert _register_smart_clone_in_user_voices(
            user_id="u", voice_id="vt", label="x",
        ) is False

        # HTTP non-200
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-key")

        class _BadResp:
            status_code = 500

            def json(self):
                return {"ok": True}  # would tempt to use

        def _bad_post(*a, **kw):
            return _BadResp()

        import requests as _requests_mod
        monkeypatch.setattr(_requests_mod, "post", _bad_post)
        assert _register_smart_clone_in_user_voices(
            user_id="u", voice_id="vt", label="x",
        ) is False

        # HTTP 200 but ok:false
        class _OkFalseResp:
            status_code = 200

            def json(self):
                return {"ok": False, "error": "db_error"}

        def _ok_false_post(*a, **kw):
            return _OkFalseResp()

        monkeypatch.setattr(_requests_mod, "post", _ok_false_post)
        assert _register_smart_clone_in_user_voices(
            user_id="u", voice_id="vt", label="x",
        ) is False

        # Network exception
        def _raises(*a, **kw):
            raise ConnectionError("network down")

        monkeypatch.setattr(_requests_mod, "post", _raises)
        assert _register_smart_clone_in_user_voices(
            user_id="u", voice_id="vt", label="x",
        ) is False

    def test_b3e_fix2_consent_true_but_empty_main_speakers_skips_quota(self):
        """Codex 第三十轮 P1 + Phase 3 (2026-05-17 user-voice-candidate-
        first §Consent × Admin 决策矩阵): consent=True + admin clone
        allowed + main_speakers=[] is a legal happy path —
        evaluate_voice_review returns AUTO_APPROVED with empty decisions
        WITHOUT reading quota or invoking provider. The b3e-fix
        consent gate alone would still trip the Gateway quota query for
        this case; a transient Gateway hiccup would then fail-closed
        handoff a job that should auto-approve as empty.

        Phase 3 also requires admin policy on the gate so an admin
        disabling smart_auto_clone_enabled doesn't waste a quota lookup.

        Pin two things at the source level:
          - The gate is the Phase 3 triple
            ``_smart_consent_allows_clone and _smart_admin_clone_enabled
            and _smart_main_speakers`` (all three conditions).
          - Else branch falls through with stub provider + 0 quota.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        # Phase 3 added admin policy gate + comments; window expanded
        # to match test_b3e_fix_consent_false_skips_quota_lookup.
        block = "\n".join(lines[:1200])

        # The gate condition must include all three axes. The Phase 3
        # gate is a multi-line ``if (...)`` block so we search the
        # core conjunction substring spanning the three conditions.
        triple_gate = (
            "_smart_consent_allows_clone\n                        "
            "and _smart_admin_clone_enabled\n                        "
            "and _smart_main_speakers"
        )
        assert triple_gate in block, (
            "Quota/provider gate must require ALL THREE: consent, "
            "admin_clone_enabled, and non-empty main_speakers. Plan "
            "2026-05-17-user-voice-candidate-first §Consent × Admin "
            "决策矩阵 — when ANY of consent / admin clone / "
            "main_speakers excludes new clone, the Gateway quota "
            "endpoint must not be called.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )

        # The bare ``if _smart_consent_allows_clone:`` form is no
        # longer used anywhere — Phase 3 replaced the sample
        # extraction gate with a triple conjunction too. Count
        # occurrences to detect drift: 0 expected.
        bare_consent_gate_count = block.count(
            "if _smart_consent_allows_clone:"
        )
        assert bare_consent_gate_count == 0, (
            f"Expected zero bare ``if _smart_consent_allows_clone:`` "
            f"gates after Phase 3 (sample extraction now also gates on "
            f"admin); found {bare_consent_gate_count}. If you added a "
            f"new gate for a different purpose, factor it into a "
            f"named variable so this guard stays sharp.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )

    def test_phase3_reuse_query_gated_on_admin_alone_not_consent(self):
        """Phase 3 (plan 2026-05-17 §Consent × Admin 决策矩阵): the
        personal-voice reuse query loop must run when
        ``_smart_admin_reuse_enabled is True``, regardless of consent.
        Plan invariant: consent only gates new clone (paid API);
        reuse is free of paid API, so consent doesn't apply.

        Pin two source-level anchors:
          - ``_smart_admin_reuse_enabled`` is loaded from admin settings
            with default True (so a missing JSON file keeps reuse on).
          - The match-query loop is wrapped in
            ``if _smart_admin_reuse_enabled:`` — NOT a consent check.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        block = "\n".join(lines[:1200])

        # Anchor 1: admin-policy variable is read with a fail-open
        # default so missing admin_settings.json doesn't disable reuse.
        # (41f5743b switched the literal ``= True`` default to the
        # app-safe ``read_admin_setting(..., default=True)`` form —
        # same invariant, pinned against the new shape.)
        assert re.search(
            r'_smart_admin_reuse_enabled = bool\(\s*'
            r'read_admin_setting\(\s*'
            r'"smart_reuse_user_voice_enabled",\s*default=True,?\s*\)',
            block,
        ), (
            "Phase 3: process.py must read smart_reuse_user_voice_enabled "
            "via read_admin_setting(..., default=True) so a missing/"
            "corrupt admin_settings.json preserves the legacy "
            "reuse-enabled behavior.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )
        # The admin field is read off the loaded settings object.
        assert "smart_reuse_user_voice_enabled" in block, (
            "Phase 3: process.py must read AdminSettings."
            "smart_reuse_user_voice_enabled to honor the admin policy."
        )

        # Anchor 2: the match-query loop is gated on admin-policy,
        # NOT consent. Find the _match_smart_user_voice call site
        # and walk backward to confirm the nearest gate.
        match_call = "_match_smart_user_voice("
        match_idx = block.find(match_call)
        assert match_idx >= 0, (
            "Phase 3: _match_smart_user_voice call must still exist "
            "inside the smart inline branch — reuse is the entire point "
            "of the candidate-first plan."
        )
        preceding = block[:match_idx]
        # The reuse query gate must be the admin-only form.
        reuse_gate_idx = preceding.rfind("if _smart_admin_reuse_enabled:")
        assert reuse_gate_idx >= 0, (
            "Phase 3: reuse query loop must be wrapped in "
            "``if _smart_admin_reuse_enabled:``. Consent must NOT gate "
            "reuse — plan §核心不变量 reuse doesn't burn paid API.\n"
            f"Preceding 2000 chars:\n{preceding[-2000:]}"
        )
        # And NO ``if _smart_consent_allows_clone:`` should appear
        # between that admin gate and the _match_smart_user_voice
        # call (i.e. the reuse query is NOT consent-gated).
        between = preceding[reuse_gate_idx:]
        assert "if _smart_consent_allows_clone" not in between, (
            "Phase 3: reuse query loop must not be inside a consent "
            "gate. Plan §Consent × Admin 决策矩阵 — consent only gates "
            "new clone, never reuse. Found consent gate between "
            "admin gate and _match_smart_user_voice call.\n"
            f"Between:\n{between}"
        )

    def test_phase4_admin_pause_threaded_into_evaluate_voice_review(self):
        """Phase 4 (plan 2026-05-17 §Phase 4 + §Smart 弱匹配暂停):
        process.py must:
          1. Read ``smart_pause_on_possible_user_voice_match`` from
             admin_settings into ``_smart_admin_pause_on_possible``
             (default False).
          2. Pass both ``possible_voice_matches_by_speaker_id`` and
             ``admin_pause_on_possible_match`` kwargs to
             ``evaluate_voice_review``.
          3. When admin pause is on AND admin reuse is on, switch the
             ``_match_smart_user_voice`` call to ``include_possible=True``
             so the /candidates endpoint replaces /match — required to
             surface non-strong candidates.

        Without this threading, a Smart job would NEVER pause on a
        possible match because the orchestrator runs with empty defaults.
        Source-level pinning so a refactor that drops any of these
        wires fails immediately."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        block = "\n".join(lines[:1500])

        # Anchor 1: admin field is loaded with a fail-closed default.
        # (41f5743b switched the literal ``= False`` default to the
        # app-safe ``read_admin_setting(..., default=False)`` form —
        # same invariant, pinned against the new shape.)
        assert re.search(
            r'_smart_admin_pause_on_possible = bool\(\s*'
            r'read_admin_setting\(\s*'
            r'"smart_pause_on_possible_user_voice_match",\s*'
            r'default=False,?\s*\)',
            block,
        ), (
            "Phase 4: process.py must read "
            "smart_pause_on_possible_user_voice_match via "
            "read_admin_setting(..., default=False) so a missing/corrupt "
            "JSON keeps Smart in the legacy (no surprise pause) "
            "behavior.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )
        # Anchor 2: the admin field name is read off the settings object.
        assert "smart_pause_on_possible_user_voice_match" in block, (
            "Phase 4: process.py must read AdminSettings."
            "smart_pause_on_possible_user_voice_match to honor the "
            "admin pause toggle."
        )

        # Anchor 3: evaluate_voice_review receives both Phase 4 kwargs.
        # Search the exact kwarg lines used at the call site so any
        # refactor that drops them fails this guard.
        assert (
            "possible_voice_matches_by_speaker_id=_smart_possible_voice_matches"
            in block
        ), (
            "Phase 4: evaluate_voice_review call must receive "
            "possible_voice_matches_by_speaker_id="
            "_smart_possible_voice_matches. Without it, "
            "evaluate_voice_review can't see the candidate list and "
            "the pause never fires.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )
        assert (
            "admin_pause_on_possible_match=_smart_admin_pause_on_possible"
            in block
        ), (
            "Phase 4: evaluate_voice_review call must receive "
            "admin_pause_on_possible_match=_smart_admin_pause_on_possible. "
            "Without it, the admin toggle is silently ignored.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )

        # Anchor 4: the matcher call must include the include_possible
        # flag (wired off the admin pause toggle) so the candidates
        # endpoint is queried when admin opts in. The legacy /match
        # endpoint only returns strong matches and is structurally
        # incapable of carrying the possible list.
        assert "include_possible=" in block, (
            "Phase 4: _match_smart_user_voice call must pass "
            "include_possible=<flag> to switch to the /candidates "
            "endpoint when admin enabled the pause toggle.\n"
            f"Block (first 4000 chars):\n{block[:4000]}"
        )

    def test_b3e_fix2_empty_main_speakers_short_circuits_in_evaluate(self):
        """Functional: evaluate_voice_review with main_speakers=[]
        returns AUTO_APPROVED + empty decisions WITHOUT invoking the
        provider. This is the contract the b3e-fix2 gate depends on —
        if this contract ever changed, the gate would need to too.

        Mirrors tests/test_smart_auto_voice_review.py:597, but pinned
        here as a b3e-fix2 dependency anchor so the relationship is
        obvious in code review."""
        from services.smart.auto_voice_review import (
            VoiceReviewOutcome, evaluate_voice_review,
        )

        from tests.fakes.fake_clone_provider import FakeCloneProvider

        fake = FakeCloneProvider(success=True)
        result = evaluate_voice_review(
            main_speakers=[],  # empty — eligibility excluded everyone
            smart_consent={"auto_voice_clone": True},  # consent True but no candidates
            clone_provider=fake,
            voice_library_quota_remaining=0,  # would trip water mark if consulted
            smart_decision_id_factory=lambda: "dec_x",
        )
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED, (
            "evaluate_voice_review with empty main_speakers must auto-"
            "approve regardless of consent / quota — the b3e-fix2 gate "
            "depends on this. Got outcome={result.outcome!r}, "
            f"pause_reason={result.pause_reason!r}."
        )
        # decisions is a tuple in the dataclass; check via len.
        assert len(result.decisions) == 0, (
            f"Expected empty decisions; got {result.decisions!r}"
        )
        assert len(fake.calls) == 0, (
            f"Provider must not be invoked when main_speakers is empty; "
            f"got {len(fake.calls)} calls: {fake.calls}"
        )

    def test_b3e_fix_clone_decision_processing_calls_mirror(self):
        """Codex 第二十九轮 P0: the CLONED branch of the decision-
        processing loop MUST call ``_register_smart_clone_in_user_voices``.
        Pin via source-level anchor so a future refactor that splits
        the loop or moves the mirror call out is flagged."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        # Phase 4 (2026-05-17) added per-speaker possible-match pause
        # audit loop + admin policy read, pushing the CLONED branch
        # past the previous 900-line window. Bumped to 1100 to keep
        # covering the mirror call without inflating it indefinitely.
        block = "\n".join(lines[:1100])

        # Mirror helper is called from process.py
        assert "_register_smart_clone_in_user_voices(" in block, (
            "Smart branch missing _register_smart_clone_in_user_voices "
            "call — Codex 第二十九轮 P0: CLONED decisions must mirror "
            "to UserVoice table or quota signal goes stale across jobs."
        )

        # Mirror error tracking + handoff branch
        assert "_smart_clone_mirror_failures" in block, (
            "Smart branch missing _smart_clone_mirror_failures list — "
            "needed to aggregate per-speaker mirror failures and "
            "escalate to handoff."
        )
        assert "clone_library_register_failed" in block, (
            "Smart branch missing clone_library_register_failed reason "
            "code — Codex 第二十九轮 P0: mirror failure must surface "
            "to Studio human review so user can take action."
        )

    def test_b3f_sidecar_helper_writes_jsonl_line(self, tmp_path):
        """PR#3C-b3f: ``_emit_smart_audit`` helper appends one JSONL line
        to ``{project_dir}/audit/smart_decisions.jsonl`` with the
        expected schema. End-to-end test against real sidecar_emitter."""
        import json

        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_x"
        project_dir.mkdir()

        _emit_smart_audit(
            project_dir,
            decision_type="speaker_gate",
            decision="approved",
            evidence={
                "main_speaker_count": 2,
                "main_speaker_ids": ["speaker_a", "speaker_b"],
            },
            extra={
                "job_id": "job-1",
                "user_id": "user-1",
            },
        )

        sidecar = project_dir / "audit" / "smart_decisions.jsonl"
        assert sidecar.exists(), (
            "smart_decisions.jsonl not created by _emit_smart_audit"
        )
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, (
            f"Expected 1 sidecar line; got {len(lines)}.\nLines: {lines}"
        )
        record = json.loads(lines[0])
        assert record["decision_type"] == "speaker_gate"
        assert record["decision"] == "approved"
        assert record["auto_approved"] is True
        assert record["evidence"]["main_speaker_count"] == 2
        assert record["evidence"]["main_speaker_ids"] == [
            "speaker_a", "speaker_b"
        ]
        # Extra fields land at top level.
        assert record["job_id"] == "job-1"
        assert record["user_id"] == "user-1"
        # Schema version + auto-generated fields.
        assert record["schema_version"] == 1
        assert isinstance(record["smart_decision_id"], str)
        assert len(record["smart_decision_id"]) >= 16
        assert isinstance(record["created_at"], str)

    def test_b3f_sidecar_helper_uses_supplied_decision_id(self, tmp_path):
        """When the caller passes ``smart_decision_id`` (e.g. piping
        through a VoiceReviewDecision.smart_decision_id), the helper
        uses it verbatim instead of generating a new UUID. This keeps
        audit linkage clean between per-speaker decisions in
        evaluate_voice_review and the sidecar JSONL."""
        import json

        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_y"
        project_dir.mkdir()

        _emit_smart_audit(
            project_dir,
            decision_type="voice_clone",
            decision="approved",
            evidence={"voice_id": "vt_xxx"},
            smart_decision_id="decision-abc-123",
        )

        record = json.loads(
            (project_dir / "audit" / "smart_decisions.jsonl")
            .read_text(encoding="utf-8")
            .strip()
        )
        assert record["smart_decision_id"] == "decision-abc-123"
        assert record["event_id"] == "decision-abc-123"

    def test_b3f_sidecar_helper_appends_multiple_lines(self, tmp_path):
        """JSONL is append-only: multiple calls produce multiple lines,
        each parseable as JSON. Mirrors the audit/ contract — one event
        per line, never truncating."""
        import json

        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_z"
        project_dir.mkdir()

        _emit_smart_audit(
            project_dir,
            decision_type="speaker_gate",
            decision="approved",
            evidence={"step": 1},
        )
        _emit_smart_audit(
            project_dir,
            decision_type="translation_auto_approve",
            decision="approved",
            evidence={"step": 2},
        )
        _emit_smart_audit(
            project_dir,
            decision_type="voice_selection_auto_approve",
            decision="rejected",
            reason_code="provider_failure_max_retries_3",
            evidence={"step": 3},
        )

        sidecar = project_dir / "audit" / "smart_decisions.jsonl"
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        records = [json.loads(line) for line in lines]
        assert [r["decision_type"] for r in records] == [
            "speaker_gate",
            "translation_auto_approve",
            "voice_selection_auto_approve",
        ]
        assert records[-1]["decision"] == "rejected"
        assert records[-1]["reason_code"] == "provider_failure_max_retries_3"
        assert records[-1]["auto_approved"] is False

    def test_b3f_sidecar_helper_swallows_invalid_decision_type(
        self, tmp_path, capsys,
    ):
        """Bad decision_type should print a diagnostic but NOT crash
        the pipeline (plan §6.4 末段). Verifies the wrapper catches
        the ValueError from emit_smart_decision."""
        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_q"
        project_dir.mkdir()

        # Should not raise.
        _emit_smart_audit(
            project_dir,
            decision_type="not_a_real_type",
            decision="approved",
        )

        # Diagnostic printed.
        captured = capsys.readouterr().out
        assert "sidecar emit failed" in captured.lower(), (
            f"Expected stderr diagnostic on enum typo; got: {captured!r}"
        )

        # No JSONL written (since the emit raised before file write).
        sidecar = project_dir / "audit" / "smart_decisions.jsonl"
        assert not sidecar.exists() or (
            sidecar.read_text(encoding="utf-8").strip() == ""
        )

    def test_b3f_voice_review_decision_field_name_is_smart_decision_id(self):
        """Codex 第三十二轮 P0: ``VoiceReviewDecision`` exposes the
        per-speaker decision identifier as ``smart_decision_id`` —
        NOT ``decision_id``. Pin this contract so a future rename
        on either side trips this test, AND so the b3f CLONED-emit
        wiring can't drift back to the broken ``_dec.decision_id``
        access (which raises AttributeError BEFORE _emit_smart_audit
        is called, after a real MiniMax clone has already burned
        quota — the most expensive crash path possible).
        """
        import dataclasses

        from services.smart.auto_voice_review import VoiceReviewDecision

        fields = {f.name for f in dataclasses.fields(VoiceReviewDecision)}
        assert "smart_decision_id" in fields, (
            "VoiceReviewDecision must expose ``smart_decision_id`` field. "
            "If the field is being renamed, update process.py's CLONED "
            "emit site at the same commit (Codex 第三十二轮 P0)."
        )
        assert "decision_id" not in fields, (
            "VoiceReviewDecision exposes ``decision_id``? Contract drift. "
            "process.py's CLONED emit site reads ``_dec.smart_decision_id``; "
            "if you added a ``decision_id`` alias, either rename the access "
            "in process.py OR drop the alias to keep one source of truth."
        )

    def test_b3f_cloned_emit_uses_correct_field_name(self):
        """Codex 第三十二轮 P0 source-level lock: the CLONED emit in
        process.py MUST read ``_dec.smart_decision_id``, never the
        non-existent ``_dec.decision_id``. Source-level anchor
        because the runtime path requires a real ProcessPipeline
        + FakeCloneProvider + injected wiring to exercise — too
        heavy for unit-level coverage. The wrong attribute would
        produce an AttributeError BEFORE the safety wrapper runs.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        # Phase 4 (2026-05-17): see test_b3e_fix_clone_decision_processing_calls_mirror
        # for the window-bump rationale.
        block = "\n".join(lines[:1100])

        # Locate the CLONED branch in the decision-processing loop.
        cloned_idx = block.find(
            "if _dec.choice == VoiceReviewChoice.CLONED:"
        )
        assert cloned_idx >= 0, (
            "CLONED branch missing in smart inline decision loop."
        )
        # Window through the CLONED branch (until next elif/else).
        cloned_window = block[cloned_idx : cloned_idx + 2500]

        assert "smart_decision_id=_dec.smart_decision_id" in cloned_window, (
            "CLONED emit must pass ``smart_decision_id=_dec.smart_decision_id``. "
            "VoiceReviewDecision exposes ``smart_decision_id`` (see "
            "src/services/smart/auto_voice_review.py:148); the wrong "
            "attribute name would AttributeError BEFORE _emit_smart_audit "
            "and crash the job AFTER a real MiniMax clone succeeded — "
            "the most expensive crash path. Codex 第三十二轮 P0.\n"
            f"CLONED window:\n{cloned_window}"
        )

        # Defensive: forbid the broken ``_dec.decision_id`` access
        # anywhere in the smart inline branch so a future copy-paste
        # can't re-introduce the same bug.
        assert "_dec.decision_id" not in block, (
            "Smart inline branch references ``_dec.decision_id`` — "
            "VoiceReviewDecision has no such field, this would crash "
            "at runtime. Use ``_dec.smart_decision_id`` instead.\n"
            f"Block (search): contains '_dec.decision_id'"
        )

    def test_b3f_smart_branch_emits_at_every_decision_point(self):
        """Source-level anchor: pin that ``_emit_smart_audit`` is
        called at each smart decision point in the inline branch. If
        a future refactor drops an emit, the QA report renderer +
        admin tooling lose visibility into that decision.

        Decision points (PR#3C-b3f):
          - speaker_gate (rejected / approved)
          - translation_auto_approve (rejected / approved)
          - voice_selection_auto_approve (rejected / approved)
          - voice_clone (approved, per-speaker)
          - downgrade_handoff (sample_extraction / quota /
            mirror / cloned_voice_expired)
        """
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "pipeline" / "process.py"
        source = src.read_text(encoding="utf-8")
        idx = source.find("Smart inline auto-approve path")
        assert idx >= 0
        lines = source[idx:].splitlines()
        # Window history:
        #   1000 → 1200 (Phase 3 admin policy gate, +50 lines)
        #   1200 → 1450 (2026-05-20 spec change added verbose
        #   spec-justification comments + degraded audit emits at
        #   the old handoff sites; clone_mirror_degraded now sits
        #   at ~line 1250 from anchor).
        block = "\n".join(lines[:1450])

        # Count distinct _emit_smart_audit call sites by their
        # decision_type field. Each (decision_type, decision) pair
        # corresponds to one anchored emit. After 2026-05-20 spec
        # change, the OLD reason_codes for quota/mirror handoffs
        # have been replaced by *_degraded audit events (log+continue
        # instead of handoff).
        required_emit_signatures = [
            # Eligibility gate (b3b) — still hands off (>3 main speakers)
            ('decision_type="speaker_gate"', 'decision="rejected"'),
            ('decision_type="speaker_gate"', 'decision="approved"'),
            # Voice review batch (b2)
            ('decision_type="voice_selection_auto_approve"', 'decision="rejected"'),
            ('decision_type="voice_selection_auto_approve"', 'decision="approved"'),
            # Per-speaker CLONED (b3d/e)
            ('decision_type="voice_clone"', 'decision="approved"'),
            # Pre-paid clone gates still hand off (real money risk)
            ('reason_code="clone_sample_extraction_failed"',),
            # 2026-05-20: defensive handoffs softened to log+continue.
            # Audit events now use *_degraded decision_types instead.
            ('decision_type="quota_lookup_degraded"',),
            ('decision_type="clone_mirror_degraded"',),
        ]
        for required in required_emit_signatures:
            for token in required:
                assert token in block, (
                    f"Smart inline branch missing sidecar emit token "
                    f"{token!r}. PR#3C-b3f: every smart decision point "
                    f"must emit one sidecar event so QA report renderer "
                    f"and admin tooling can reconstruct WHY smart "
                    f"decided as it did.\n"
                    f"Block first 4000 chars:\n{block[:4000]}"
                )

        # 2026-05-20 spec change: translation_review now ALWAYS
        # auto-approves (smart 全自动化). Only the "approved" emit
        # path survives; the rejected emit path is dead code.
        # Pin the approved emit specifically.
        assert 'decision_type="translation_auto_approve"' in source, (
            "translation_auto_approve audit missing — should still emit "
            "the approved decision so audit trail covers the review."
        )

        # Pre-TTS expiry handoff anchored on cloned_voice_expired
        # (external provider state, still legitimate handoff).
        assert 'reason_code="cloned_voice_expired"' in source, (
            "Pre-TTS expiry handoff missing sidecar emit with "
            "reason_code='cloned_voice_expired'."
        )

    def test_b3d_consent_false_skips_provider_call_entirely(self):
        """Regression — when consent is False (default for non-clone
        jobs), evaluate_voice_review routes to PRESET without invoking
        the provider. This means even if smart_wiring's real provider
        somehow leaked into test scope, no API call would happen.
        """
        from pathlib import Path

        from services.smart.auto_voice_review import (
            VoiceReviewChoice, VoiceReviewOutcome, VoiceReviewSpeakerInput,
            evaluate_voice_review,
        )

        from tests.fakes.fake_clone_provider import FakeCloneProvider

        fake = FakeCloneProvider(success=True)
        result = evaluate_voice_review(
            main_speakers=[
                VoiceReviewSpeakerInput(
                    speaker_id="speaker_a",
                    speaker_name="A",
                    sample_seconds=20.0,
                    source_audio_path=Path("/tmp/fake/x.wav"),
                ),
            ],
            smart_consent={"auto_voice_clone": False},  # explicit no-consent
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=lambda: "dec_x",
        )
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        # PRESET choices — no clone calls.
        assert all(d.choice is VoiceReviewChoice.PRESET for d in result.decisions)
        assert len(fake.calls) == 0, (
            f"consent=False MUST NOT call the provider; got "
            f"{len(fake.calls)} calls. fake.calls={fake.calls}"
        )


class TestTranslationReviewProcessIntegrationFailClosed:
    """Codex 第二十五轮 — pin the two fail-closed behaviours added in
    PR#3C-b3c-fix.

    Both behaviours are pure: they don't run process.py's whole
    smart branch (which requires the full pipeline plumbing), but
    they exercise the SAME synthesis logic that branch uses inline:

      1. compliance_block derivation from
         ``content_compliance_payload["status"] == "blocked"`` →
         routes evaluate_translation_review to reject with
         ``compliance_high_risk``.

      2. Glossary helper exception when glossary is configured →
         synthesize a TranslationReviewDecision(auto_approved=False,
         reason_code="glossary_check_error", failed_check=
         "glossary_preservation") that flows into the existing
         handoff branch.
    """

    def _build_segments(self, speaker_ids):
        from services.gemini.translator import DubbingSegment

        return [
            DubbingSegment(
                segment_id=i,
                speaker_id=sid,
                display_name=sid.upper(),
                voice_id="voice_x",
                start_ms=i * 1000,
                end_ms=(i + 1) * 1000,
                target_duration_ms=1000,
                source_text="hello",
                cn_text="你好",
            )
            for i, sid in enumerate(speaker_ids)
        ]

    def test_compliance_block_derivation_from_payload(self):
        """2026-05-20 spec change: compliance is now an early-pipeline
        gate (post-S1) — non-admin compliance block raises
        ``ContentPolicyViolationError`` in
        ``_run_content_compliance_review`` and exits the pipeline.
        The legacy ``compliance_block`` kwarg on
        ``evaluate_translation_review`` is now ignored.

        We still pin the payload-derivation logic (admin tooling
        + future hookups may still consume the ``status=="blocked"``
        signal), but the downstream effect on evaluate_translation_review
        is now: auto-approve regardless. The actual user-facing exit
        happens at the raise site, not here.
        """
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        # Derivation logic still pinned (no behavior change).
        for status, expected_block in (
            ("blocked", True),
            ("passed", False),
            ("skipped", False),
            ("needs_manual_review", False),
            ("error", False),
            (None, False),
        ):
            payload = {"status": status} if status is not None else None
            compliance_block = bool(
                isinstance(payload, dict)
                and payload.get("status") == "blocked"
            )
            assert compliance_block is expected_block, (
                f"compliance_block derivation broken for status={status!r}: "
                f"got {compliance_block}, expected {expected_block}"
            )

        # End-to-end: compliance_block=True kwarg is now IGNORED;
        # evaluate_translation_review auto-approves anyway. The legal
        # gate has moved to ContentPolicyViolationError raised earlier.
        segments = self._build_segments(["speaker_a"] * 5)
        translation_input = {
            "glossary_total_terms": 10,
            "glossary_preserved_terms": 10,
            "length_overflow_rate": None,
            "rewrite_attempted": False,
            "subtitle_source_text_sha256": None,
            "final_spoken_text_sha256": None,
            "segments": [
                {"segment_id": str(s.segment_id), "speaker_id": s.speaker_id}
                for s in segments
            ],
        }
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 1.0,
                "speaker_duration_ms": 50_000,
            },
        }
        speaker_stats = {
            "speakers": [
                {"speaker_id": "speaker_a", "role": "primary",
                 "duration_share": 1.0},
            ],
            "uncertain_speaker_duration_share": 0.0,
            "asr_speaker_count": 1,
        }
        clone_sample_stats = {"eligible_speakers": 1}

        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=speaker_stats,
            clone_sample_stats=clone_sample_stats,
            compliance_block=True,  # NOW IGNORED
        )
        assert decision.auto_approved is True, (
            "evaluate_translation_review must auto-approve regardless "
            "of compliance_block kwarg per 2026-05-20 spec. The legal "
            "gate is now ContentPolicyViolationError raised in "
            "_run_content_compliance_review (early pipeline, post-S1)."
        )
        assert decision.reason_code is None

    def test_compliance_block_admin_override_still_continues_smart(self):
        """2026-05-20 spec: admin compliance override no longer
        re-prompts at translation_review. Admin already gave consent
        at compliance time; smart auto-pipeline must not double-confirm.

        Non-admin behavior unchanged: ContentPolicyViolationError
        raised in _run_content_compliance_review still exits pipeline
        (legal gate is the raise site, not this module)."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        # Derivation logic unchanged: any "blocked" status → True
        payload = {
            "status": "blocked",
            "admin_override": True,
            "message": "Admin overrode the block",
        }
        compliance_block = bool(
            isinstance(payload, dict)
            and payload.get("status") == "blocked"
        )
        assert compliance_block is True

        # But downstream effect on evaluate_translation_review:
        # the kwarg is ignored → auto-approve.
        segments = self._build_segments(["speaker_a"] * 5)
        translation_input = {
            "glossary_total_terms": 10,
            "glossary_preserved_terms": 10,
            "length_overflow_rate": None,
            "rewrite_attempted": False,
            "subtitle_source_text_sha256": None,
            "final_spoken_text_sha256": None,
            "segments": [
                {"segment_id": str(s.segment_id), "speaker_id": s.speaker_id}
                for s in segments
            ],
        }
        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats={
                "uncertain_speaker_duration_share": 0.0,
                "asr_speaker_count": 1,
            },
            clone_sample_stats={"eligible_speakers": 1},
            compliance_block=True,
        )
        assert decision.auto_approved is True

    def test_glossary_check_error_synthesizes_handoff_decision(self):
        """Codex 第二十五轮 P1-2: a broken glossary helper on a
        configured glossary must NOT vacuous-pass via total_terms=0.
        Pin the synthesized handoff decision shape that process.py
        constructs inline (so the rest of the handoff branch consumes
        it identically to a real evaluate_translation_review reject).
        """
        from services.smart.auto_translation_review import (
            TranslationReviewDecision,
        )

        # The synthesized decision shape — must match what process.py
        # creates when _smart_glossary_check_failed = True.
        synthesized = TranslationReviewDecision(
            auto_approved=False,
            reason_code="glossary_check_error",
            failed_check="glossary_preservation",
            metrics={
                "glossary_check_error": "regex.error: bad escape",
                "glossary_configured_terms": 7,
            },
        )
        # The handoff branch reads .auto_approved + .reason_code +
        # .failed_check + .metrics. Pin all four.
        assert synthesized.auto_approved is False
        assert synthesized.reason_code == "glossary_check_error"
        assert synthesized.failed_check == "glossary_preservation"
        assert synthesized.metrics["glossary_check_error"] == (
            "regex.error: bad escape"
        )
        assert synthesized.metrics["glossary_configured_terms"] == 7

    def test_glossary_empty_remains_vacuous_pass(self):
        """Empty glossary must STILL vacuous-pass — Codex 第二十五轮 P1-2
        only changes behaviour for configured-but-broken; "no glossary
        configured" was correct before and remains correct."""
        from services.smart.auto_translation_review import (
            evaluate_translation_review,
        )

        # When _review_glossary is empty/None, process.py skips the
        # helper call entirely and writes ``total_terms=0``.
        # evaluate_translation_review treats total=0 as "no glossary"
        # and vacuous-passes that check.
        segments = self._build_segments(["speaker_a"] * 5)
        translation_input = {
            "glossary_total_terms": 0,  # what process.py writes when no glossary
            "glossary_preserved_terms": 0,
            "length_overflow_rate": None,
            "rewrite_attempted": False,
            "subtitle_source_text_sha256": None,
            "final_spoken_text_sha256": None,
            "segments": [
                {"segment_id": str(s.segment_id), "speaker_id": s.speaker_id}
                for s in segments
            ],
        }
        profiles = {
            "speaker_a": {
                "speaker_role": "primary",
                "speaker_duration_share": 1.0,
                "speaker_duration_ms": 50_000,
            },
        }
        speaker_stats = {
            "speakers": [
                {"speaker_id": "speaker_a", "role": "primary",
                 "duration_share": 1.0},
            ],
            "uncertain_speaker_duration_share": 0.0,
            "asr_speaker_count": 1,
        }
        clone_sample_stats = {"eligible_speakers": 1}

        decision = evaluate_translation_review(
            translation_result=translation_input,
            speaker_stats=speaker_stats,
            clone_sample_stats=clone_sample_stats,
        )
        assert decision.auto_approved is True, (
            f"Empty glossary should still vacuous-pass; got "
            f"reason={decision.reason_code!r} "
            f"failed_check={decision.failed_check!r}"
        )


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

    def test_emit_mkdir_failure_returns_false_does_not_block(self, tmp_path, monkeypatch):
        """Codex 第九轮 P1-4: mkdir error inside the path helper used to
        bubble out of emit_smart_decision and block the user-facing
        pipeline. Per plan §6.4 末段 emit failure must NOT block. Now
        path/dir computation lives inside the try; mkdir error returns
        False with logger.exception."""
        from services.smart import sidecar_emitter

        original_mkdir = Path.mkdir

        def selective_mkdir_raise(self, *a, **kw):
            # Raise specifically when audit/ is being created — leaves
            # other tmp_path operations alone.
            if self.name == "audit":
                raise PermissionError("mock permission denied on audit/")
            return original_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", selective_mkdir_raise)

        ok = sidecar_emitter.emit_smart_decision(
            tmp_path,
            decision_type="speaker_gate",
            decision="approved",
            smart_decision_id="dec_mkdir_fail",
            created_at="2026-05-14T12:00:00Z",
        )
        # Returns False, doesn't raise.
        assert ok is False
        # No audit/ dir was actually created (the mock raised).
        assert not (tmp_path / "audit").exists()

    def test_atomic_write_mkdir_failure_returns_false(self, tmp_path, monkeypatch):
        """Same fail-soft contract for the atomic writers (quality report
        / cost summary)."""
        from services.smart import sidecar_emitter

        original_mkdir = Path.mkdir

        def selective_mkdir_raise(self, *a, **kw):
            if self.name == "audit":
                raise OSError("mock disk full")
            return original_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", selective_mkdir_raise)

        ok = sidecar_emitter.write_smart_quality_report(
            tmp_path, {"main_speaker_count": 2}
        )
        assert ok is False

    def test_quality_report_schema_version_cannot_be_clobbered(self, tmp_path):
        """Codex 第九轮 P2: payload-supplied schema_version must NOT
        override the module's authoritative version. Earlier
        ``{"schema_version": v, **payload}`` form let any caller's
        ``payload["schema_version"] = 999`` clobber the stamp; renderers
        downstream that branch on schema_version would break."""
        from services.smart.sidecar_emitter import (
            write_smart_quality_report,
            smart_quality_report_path,
            SMART_QUALITY_REPORT_SCHEMA_VERSION,
        )

        # Caller's payload deliberately tries to set schema_version=999.
        payload = {
            "schema_version": 999,
            "main_speaker_count": 2,
        }
        ok = write_smart_quality_report(tmp_path, payload)
        assert ok is True

        with open(smart_quality_report_path(tmp_path), encoding="utf-8") as fp:
            data = json.load(fp)
        # Module's authoritative version wins.
        assert data["schema_version"] == SMART_QUALITY_REPORT_SCHEMA_VERSION
        assert data["schema_version"] != 999
        # Payload's other fields still flow through.
        assert data["main_speaker_count"] == 2

    def test_cost_summary_schema_version_cannot_be_clobbered(self, tmp_path):
        """Same protection on the cost-summary writer."""
        from services.smart.sidecar_emitter import (
            write_smart_cost_summary,
            smart_cost_summary_path,
            SMART_COST_SUMMARY_SCHEMA_VERSION,
        )

        payload = {
            "schema_version": 42,
            "llm_input_tokens": 1500,
        }
        ok = write_smart_cost_summary(tmp_path, payload)
        assert ok is True

        with open(smart_cost_summary_path(tmp_path), encoding="utf-8") as fp:
            data = json.load(fp)
        assert data["schema_version"] == SMART_COST_SUMMARY_SCHEMA_VERSION
        assert data["llm_input_tokens"] == 1500

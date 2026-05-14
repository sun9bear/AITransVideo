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
        # Codex 第九轮 P1-3: must mirror simulator format exactly —
        # "{eligible}/{asr}" with forward slash, NOT "_of_". Shadow vs
        # production diff aggregation depends on this string.
        assert decision.reason_code == "low_clone_eligible_ratio_1/3"

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
        """Glossary IS spec-defined to be optional (plan §6.2.2 step 1
        wording: 'Glossary 存在时 ≥80%'). No glossary → vacuously pass.
        This is NOT fail-open in the Codex P1-2 sense — it's spec-explicit.
        """
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["translation_result"]["glossary_total_terms"] = 0
        inputs["translation_result"]["glossary_preserved_terms"] = 0
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is True
        assert decision.metrics["glossary_preservation_rate"] is None

    def test_missing_uncertain_share_fails_closed_with_unified_reason(self):
        """Codex 第十轮 P2: missing-signal reason aligns with simulator
        (smart_shadow_sim_simulator.py:187) — single ``missing_signals``
        reason + evidence list of missing fields, NOT per-field
        unevaluable codes. Critical for shadow-vs-production reason
        aggregation."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        del inputs["speaker_stats"]["uncertain_speaker_duration_share"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "missing_signals"
        assert decision.failed_check == "missing_signals_precheck"
        # Evidence carries the specific missing field names.
        assert decision.metrics["missing"] == ["uncertain_speaker_duration_share"]

    def test_missing_clone_signals_fails_closed_with_unified_reason(self):
        """Same unified reason format for missing clone signals — and
        when multiple signals miss, evidence list grows accordingly."""
        from services.smart.auto_translation_review import evaluate_translation_review

        # Missing asr_speaker_count alone
        inputs = self._passing_inputs()
        del inputs["speaker_stats"]["asr_speaker_count"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "missing_signals"
        assert decision.metrics["missing"] == ["asr_speaker_count"]

        # Missing eligible_speakers alone
        inputs = self._passing_inputs()
        del inputs["clone_sample_stats"]["eligible_speakers"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "missing_signals"
        assert decision.metrics["missing"] == ["eligible_speakers"]

        # Multiple missing — evidence captures all of them
        inputs = self._passing_inputs()
        del inputs["speaker_stats"]["asr_speaker_count"]
        del inputs["clone_sample_stats"]["eligible_speakers"]
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "missing_signals"
        assert sorted(decision.metrics["missing"]) == [
            "asr_speaker_count", "eligible_speakers"
        ]

    def test_zero_asr_speakers_fails_closed(self):
        """Defensive: 0 ASR speakers is itself an upstream-data anomaly
        that shouldn't auto-approve. div-by-zero guard returns
        unevaluable, not silent pass."""
        from services.smart.auto_translation_review import evaluate_translation_review

        inputs = self._passing_inputs()
        inputs["speaker_stats"]["asr_speaker_count"] = 0
        decision = evaluate_translation_review(**inputs)
        assert decision.auto_approved is False
        assert decision.reason_code == "unevaluable_zero_asr_speakers"


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

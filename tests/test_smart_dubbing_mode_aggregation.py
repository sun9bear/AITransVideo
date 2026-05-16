"""Codex 第四十轮 P2.4: aggregate segment-level dubbing_mode to
speaker-level for the smart minor-speaker helper.

Discovery: ``_resolve_smart_minor_speaker_voices`` checks
``sp.get("dubbing_mode") in {keep_original, mute_or_background}``,
but ``_build_voice_selection_review_payload`` doesn't put
``dubbing_mode`` on speaker entries — it's a segment-level field.
So the helper's exclusion check NEVER fires today; non-main
``keep_original`` speakers DO get auto-matched preset voices
written to ``_speaker_voices`` (even though TTS downstream skips
their segments anyway).

eligibility_gate.py docstring already flagged this aggregation
gap as the "PR#3C integration contract" that callers must do:

  speaker_dubbing_mode = (
      "keep_original" if all(seg.dubbing_mode == "keep_original" for seg in segs)
      else "mute_or_background" if all(seg.dubbing_mode == "mute_or_background" for seg in segs)
      else "dub"
  )

Fix: add a small pure helper ``_aggregate_speaker_dubbing_modes``
that does the aggregation, then update ``_resolve_smart_minor_speaker_voices``
to consume the resulting dict (instead of reading from sp.get).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ===========================================================================
# Cycle 1 — aggregator pure function
# ===========================================================================


def _seg(speaker_id: str, dubbing_mode: str):
    """Tiny synthetic segment for tests."""
    return SimpleNamespace(speaker_id=speaker_id, dubbing_mode=dubbing_mode)


class TestSpeakerDubbingModeAggregator:

    def test_empty_segments_yields_empty_dict(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        assert _aggregate_speaker_dubbing_modes([]) == {}

    def test_speaker_with_all_keep_original_segments_is_keep_original(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            _seg("speaker_c", "keep_original"),
            _seg("speaker_c", "keep_original"),
            _seg("speaker_c", "keep_original"),
        ]
        assert _aggregate_speaker_dubbing_modes(segs) == {
            "speaker_c": "keep_original",
        }

    def test_speaker_with_all_mute_or_background_segments(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            _seg("speaker_d", "mute_or_background"),
            _seg("speaker_d", "mute_or_background"),
        ]
        assert _aggregate_speaker_dubbing_modes(segs) == {
            "speaker_d": "mute_or_background",
        }

    def test_mixed_modes_default_to_dub(self):
        """If ANY segment is dub (or any other non-skip mode), the
        speaker overall is treated as needing dubbing. This errs on
        the side of giving the speaker a voice — undershooting would
        leave audio holes."""
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            _seg("speaker_e", "keep_original"),
            _seg("speaker_e", "dub"),  # one dub flips speaker to dub
        ]
        assert _aggregate_speaker_dubbing_modes(segs) == {
            "speaker_e": "dub",
        }

    def test_all_dub_yields_dub(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [_seg("speaker_a", "dub"), _seg("speaker_a", "dub")]
        assert _aggregate_speaker_dubbing_modes(segs) == {"speaker_a": "dub"}

    def test_multiple_speakers_each_aggregated_independently(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            _seg("speaker_a", "dub"),
            _seg("speaker_a", "dub"),
            _seg("speaker_b", "keep_original"),
            _seg("speaker_b", "keep_original"),
            _seg("speaker_c", "mute_or_background"),
            _seg("speaker_d", "dub"),  # mixed → dub
            _seg("speaker_d", "keep_original"),
        ]
        result = _aggregate_speaker_dubbing_modes(segs)
        assert result == {
            "speaker_a": "dub",
            "speaker_b": "keep_original",
            "speaker_c": "mute_or_background",
            "speaker_d": "dub",
        }

    def test_segment_with_missing_dubbing_mode_treated_as_dub(self):
        """Defensive — segments built before normalization may lack
        the field. Default to 'dub' (most permissive)."""
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            SimpleNamespace(speaker_id="speaker_x"),  # no dubbing_mode attr
            _seg("speaker_x", "keep_original"),
        ]
        # mixed (missing+keep_original) → dub
        assert _aggregate_speaker_dubbing_modes(segs) == {"speaker_x": "dub"}

    def test_segment_with_missing_speaker_id_skipped(self):
        from pipeline.process import _aggregate_speaker_dubbing_modes

        segs = [
            SimpleNamespace(dubbing_mode="dub"),  # no speaker_id
            _seg("speaker_a", "dub"),
        ]
        assert _aggregate_speaker_dubbing_modes(segs) == {"speaker_a": "dub"}


# ===========================================================================
# Cycle 2 — minor helper consumes the aggregated dict
# ===========================================================================


class TestMinorHelperWithDubbingModeAggregation:

    def test_keep_original_speaker_excluded_via_aggregation_dict(self):
        """The NEW signature reads dubbing_mode_by_speaker dict,
        not sp.get('dubbing_mode'). speaker_c is keep_original but
        sp dict doesn't carry that — only the aggregation dict
        knows. Helper must consult the dict."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_c",
                    # NO dubbing_mode key — production payload shape
                    "auto_matched_voice": {"voice_id": "vt_should_skip"},
                },
            ],
            main_speaker_ids=set(),
            dubbing_mode_by_speaker={"speaker_c": "keep_original"},
        )
        assert result == {}, (
            "speaker_c is keep_original via aggregated dict; helper must "
            "skip it even though sp dict has no dubbing_mode key."
        )

    def test_mute_or_background_speaker_excluded_via_aggregation_dict(self):
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_d",
                    "auto_matched_voice": {"voice_id": "vt_should_skip"},
                },
            ],
            main_speaker_ids=set(),
            dubbing_mode_by_speaker={"speaker_d": "mute_or_background"},
        )
        assert result == {}

    def test_dub_speaker_gets_voice_via_aggregation_dict(self):
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_c",
                    "auto_matched_voice": {"voice_id": "vt_minor_voice"},
                },
            ],
            main_speaker_ids=set(),
            dubbing_mode_by_speaker={"speaker_c": "dub"},
        )
        assert result == {"speaker_c": "vt_minor_voice"}

    def test_speaker_not_in_aggregation_dict_treated_as_dub(self):
        """Defensive: if the caller forgot to put a speaker in the
        aggregation dict (e.g., new speaker created post-translation),
        default to 'dub' (give it a voice) rather than silently dropping."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_x",
                    "auto_matched_voice": {"voice_id": "vt_minor"},
                },
            ],
            main_speaker_ids=set(),
            dubbing_mode_by_speaker={},  # missing entry for speaker_x
        )
        assert result == {"speaker_x": "vt_minor"}, (
            "missing aggregation entry should default to dub (with voice), "
            "not silently skip"
        )

    def test_main_speaker_skipped_regardless_of_dubbing_mode(self):
        """Main speakers are filtered first (already handled by
        voice_review.decisions). Aggregation dict doesn't change that."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_a",
                    "auto_matched_voice": {"voice_id": "vt_a"},
                },
            ],
            main_speaker_ids={"speaker_a"},
            dubbing_mode_by_speaker={"speaker_a": "dub"},
        )
        assert result == {}

    def test_mixed_production_shape_realistic(self):
        """Realistic: speakers payload as built by
        _build_voice_selection_review_payload (no dubbing_mode on sp);
        aggregation dict built from translation_result.segments."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {  # main
                    "speaker_id": "speaker_a",
                    "auto_matched_voice": {"voice_id": "vt_a"},
                },
                {  # minor, dub → include
                    "speaker_id": "speaker_b",
                    "auto_matched_voice": {"voice_id": "vt_b_minor"},
                },
                {  # minor, keep_original → skip
                    "speaker_id": "speaker_c",
                    "auto_matched_voice": {"voice_id": "vt_c_skip"},
                },
                {  # minor, mute_or_background → skip
                    "speaker_id": "speaker_d",
                    "auto_matched_voice": {"voice_id": "vt_d_skip"},
                },
            ],
            main_speaker_ids={"speaker_a"},
            dubbing_mode_by_speaker={
                "speaker_a": "dub",
                "speaker_b": "dub",
                "speaker_c": "keep_original",
                "speaker_d": "mute_or_background",
            },
        )
        assert result == {"speaker_b": "vt_b_minor"}


# ===========================================================================
# Cycle 3 — source-anchor: call site passes the aggregation
# ===========================================================================


_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestCallSitePassesAggregation:

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_call_site_passes_dubbing_mode_by_speaker_kwarg(self):
        """The smart inline branch call to _resolve_smart_minor_speaker_voices
        must pass the dubbing_mode_by_speaker kwarg (otherwise the helper
        falls back to default-dub for all speakers — same broken
        behavior as before this fix)."""
        source = self._source()
        anchor = "_resolve_smart_minor_speaker_voices("
        # There's the def line + the call site. Find the call (skip def).
        idx = 0
        call_idx = -1
        while True:
            idx = source.find(anchor, idx)
            if idx < 0:
                break
            # Check if this is a function definition vs a call
            preceding = source[max(0, idx - 20) : idx]
            if "def " not in preceding:
                call_idx = idx
                break
            idx += 1
        assert call_idx >= 0, "call site not found"

        # The kwarg should be in the same call expression — look 600
        # chars after the open paren.
        call_window = source[call_idx : call_idx + 600]
        assert "dubbing_mode_by_speaker=" in call_window, (
            "Smart inline branch call site must pass "
            "``dubbing_mode_by_speaker=`` kwarg built from "
            "translation_result.segments via "
            "_aggregate_speaker_dubbing_modes.\n"
            f"Call window:\n{call_window[:500]}"
        )

    def test_call_site_builds_aggregation_from_segments(self):
        """The aggregation must come from translation_result.segments,
        not be hard-coded or omitted."""
        source = self._source()
        anchor = "_resolve_smart_minor_speaker_voices("
        # Find call site (skip def)
        idx = 0
        call_idx = -1
        while True:
            idx = source.find(anchor, idx)
            if idx < 0:
                break
            preceding = source[max(0, idx - 20) : idx]
            if "def " not in preceding:
                call_idx = idx
                break
            idx += 1
        assert call_idx >= 0

        # Look 800 chars BEFORE the call — the aggregation should be
        # computed just before passing the kwarg.
        pre_call = source[max(0, call_idx - 1200) : call_idx]
        assert "_aggregate_speaker_dubbing_modes(" in pre_call, (
            "Smart inline branch must call _aggregate_speaker_dubbing_modes "
            "to compute the per-speaker dubbing_mode dict from "
            "translation_result.segments before invoking the minor helper.\n"
            f"Pre-call window (last 1000 chars):\n{pre_call[-1000:]}"
        )

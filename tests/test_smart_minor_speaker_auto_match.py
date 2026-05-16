"""Smart MVP: non-main speakers get auto-matched preset voice.

Discovery (2026-05-16): the smart inline branch's voice-decision loop
only iterates ``_smart_voice_review.decisions``, which contains ONLY
main speakers (filtered by eligibility_gate). Non-main speakers
(low_share / rank > 3 / etc.) end up with no entry in
``_speaker_voices`` and downstream TTS leaves their ``segment.voice_id``
as ``None`` (process.py:7113 comment "leave voice_id as-is (auto-match)"
is aspirational — no actual auto-match runs at TTS time).

Fix: after the main-speaker decision loop, also iterate
``vs_payload.speakers`` and pull the ``auto_matched_voice``
(pre-computed by ``voice_match_resolver`` at vs_payload build time
for ALL speakers regardless of main/minor status) for each non-main,
non-keep-original speaker.

Three exclusion buckets pinned by tests:
  1. Main speakers — already handled by voice_review.decisions
  2. ``dubbing_mode in {keep_original, mute_or_background}`` — user
     explicitly kept original, do NOT inject a preset
  3. Missing/empty ``auto_matched_voice`` — degraded shape, skip

Same ``_resolve_preset_voice_id`` helper used in the main-speaker
PRESET branch (Codex 第三十七轮 P2 strict-string contract) extracts
the bare voice_id string from the dict.
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


_PROCESS_PY = _SRC / "pipeline" / "process.py"


# ===========================================================================
# Cycle 1 — pure helper behavior
# ===========================================================================


class TestMinorSpeakerVoiceResolver:

    def test_empty_speakers_yields_empty_result(self):
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[],
            main_speaker_ids=set(),
        )
        assert result == {}

    def test_only_main_speakers_yields_empty_result(self):
        """All speakers are main → nothing for the helper to do."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_a",
                    "auto_matched_voice": {"voice_id": "vt_x"},
                },
                {
                    "speaker_id": "speaker_b",
                    "auto_matched_voice": {"voice_id": "vt_y"},
                },
            ],
            main_speaker_ids={"speaker_a", "speaker_b"},
        )
        assert result == {}

    def test_non_main_speaker_with_auto_match_returns_voice_id(self):
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_a",  # main
                    "auto_matched_voice": {"voice_id": "vt_main"},
                },
                {
                    "speaker_id": "speaker_c",  # minor (low_share)
                    "auto_matched_voice": {
                        "voice_id": "vt_minor_preset",
                        "label": "Test Minor",
                    },
                },
            ],
            main_speaker_ids={"speaker_a"},
        )
        assert result == {"speaker_c": "vt_minor_preset"}

    def test_non_main_with_keep_original_skipped(self):
        """``dubbing_mode=keep_original`` is the user's explicit choice
        to keep the original audio for that speaker. MUST NOT inject a
        preset — that would override the user intent.

        Codex 第四十轮 P2.4 update: dubbing_mode is now read from the
        ``dubbing_mode_by_speaker`` dict (aggregated from segment-level
        state by ``_aggregate_speaker_dubbing_modes``), not from
        ``sp["dubbing_mode"]`` — the production vs_payload speaker
        entries don't carry the field.
        """
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_c",
                    "auto_matched_voice": {"voice_id": "vt_should_skip"},
                },
            ],
            main_speaker_ids=set(),
            dubbing_mode_by_speaker={"speaker_c": "keep_original"},
        )
        assert result == {}

    def test_non_main_with_mute_or_background_skipped(self):
        """Same as keep_original — user wants this speaker mute / merged
        into ambient track, NOT dubbed. Codex 40 P2.4: read aggregated
        dubbing_mode from the dict, not sp dict."""
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

    def test_non_main_without_auto_match_skipped(self):
        """If voice_match_resolver couldn't compute an auto_matched_voice
        (provider down / no candidates), skip — don't crash, don't
        invent a value. Downstream TTS will see voice_id=None and use
        provider default."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        # auto_matched_voice missing entirely
        r1 = _resolve_smart_minor_speaker_voices(
            speakers=[{"speaker_id": "speaker_e"}],
            main_speaker_ids=set(),
        )
        assert r1 == {}

        # auto_matched_voice = None
        r2 = _resolve_smart_minor_speaker_voices(
            speakers=[
                {"speaker_id": "speaker_e", "auto_matched_voice": None}
            ],
            main_speaker_ids=set(),
        )
        assert r2 == {}

        # auto_matched_voice = {} (empty dict)
        r3 = _resolve_smart_minor_speaker_voices(
            speakers=[
                {"speaker_id": "speaker_e", "auto_matched_voice": {}}
            ],
            main_speaker_ids=set(),
        )
        assert r3 == {}

        # auto_matched_voice has voice_id=None inside dict
        r4 = _resolve_smart_minor_speaker_voices(
            speakers=[
                {
                    "speaker_id": "speaker_e",
                    "auto_matched_voice": {"voice_id": None},
                }
            ],
            main_speaker_ids=set(),
        )
        assert r4 == {}

    def test_mixed_speakers_returns_only_dub_able_minors(self):
        """Realistic mixed scenario: 2 main, 1 keep_original, 1 minor
        with auto-match, 1 minor without auto-match. Codex 40 P2.4:
        dubbing_mode comes from the aggregated dict."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                {  # main
                    "speaker_id": "speaker_a",
                    "auto_matched_voice": {"voice_id": "vt_a"},
                },
                {  # main
                    "speaker_id": "speaker_b",
                    "auto_matched_voice": {"voice_id": "vt_b"},
                },
                {  # excluded via aggregated dict — keep original
                    "speaker_id": "speaker_c",
                    "auto_matched_voice": {"voice_id": "vt_c_skip"},
                },
                {  # minor — eligible for auto-match
                    "speaker_id": "speaker_d",
                    "auto_matched_voice": {"voice_id": "vt_d_minor"},
                },
                {  # minor — no auto-match, skip
                    "speaker_id": "speaker_e",
                    "auto_matched_voice": None,
                },
            ],
            main_speaker_ids={"speaker_a", "speaker_b"},
            dubbing_mode_by_speaker={
                "speaker_a": "dub",
                "speaker_b": "dub",
                "speaker_c": "keep_original",
                "speaker_d": "dub",
                "speaker_e": "dub",
            },
        )
        assert result == {"speaker_d": "vt_d_minor"}

    def test_handles_malformed_speaker_entries_gracefully(self):
        """Defensive — non-dict entries / missing speaker_id must not crash."""
        from pipeline.process import _resolve_smart_minor_speaker_voices

        result = _resolve_smart_minor_speaker_voices(
            speakers=[
                None,
                "not a dict",
                {"speaker_id": ""},  # empty id
                {  # valid minor
                    "speaker_id": "speaker_x",
                    "auto_matched_voice": {"voice_id": "vt_ok"},
                },
            ],
            main_speaker_ids=set(),
        )
        assert result == {"speaker_x": "vt_ok"}


# ===========================================================================
# Cycle 2 — source-anchor: helper wired in smart inline branch
# ===========================================================================


class TestMinorSpeakerWiringInProcessPy:

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_helper_called_in_smart_inline_branch(self):
        """After the main-speaker decision loop populates ``_speaker_voices``,
        the new helper must be called to fill in non-main speakers."""
        source = self._source()
        # Anchor on the main-speaker iteration start.
        anchor = "for _dec in _smart_voice_review.decisions:"
        idx = source.find(anchor)
        assert idx >= 0, "smart voice_review iteration anchor not found"

        # Look ~8000 chars after the anchor — the for-loop body itself
        # is ~4500 chars (it handles CLONED with mirror registration +
        # PRESET branches), then the helper call comes right after.
        block = source[idx : idx + 8000]
        assert "_resolve_smart_minor_speaker_voices(" in block, (
            "Smart inline branch must call _resolve_smart_minor_speaker_voices "
            "after the main-speaker decision loop. Without it, non-main "
            "speakers' segment.voice_id stays None at TTS time.\n"
            f"Block (last 1500 chars):\n{block[-1500:]}"
        )

    def test_voice_selection_auto_approve_evidence_includes_minor_count(self):
        """The batch ``voice_selection_auto_approve`` sidecar event must
        carry the minor-preset count so admin can see how many speakers
        got auto-matched preset (not just clones + main presets).

        Note: there are two ``decision_type=voice_selection_auto_approve``
        sites (handoff REJECTED and batch APPROVED). The minor count
        only makes sense on the APPROVED batch path. Anchor on the
        APPROVED-side text pattern.
        """
        source = self._source()
        # The batch APPROVED event sits right after _smart_preset_count
        # is computed — anchor on that variable name to find the right
        # event (the REJECTED handoff event doesn't compute that count).
        anchor = "_smart_preset_count = sum("
        idx = source.find(anchor)
        assert idx >= 0, "_smart_preset_count anchor not found"

        evidence_window = source[idx : idx + 1500]
        assert "minor_preset_count" in evidence_window, (
            "voice_selection_auto_approve evidence must include "
            "``minor_preset_count`` so admin can audit non-main speaker "
            "auto-match decisions.\n"
            f"Window (first 1000 chars):\n{evidence_window[:1000]}"
        )

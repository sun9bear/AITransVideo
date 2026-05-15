"""Smart inline branch PRESET-decision regression test.

P3-d E2E discovery (2026-05-15): when smart auto_voice_review picks
PRESET (e.g. quota brake fallback, low sample seconds, persona-mismatch),
process.py's smart inline branch was assigning ``_sp_entry["voice_id"]``
to the FULL ``auto_matched_voice`` value — which is a dict
``{"voice_id": str, "label": str, "match_confidence": str,
"backup_voices": [...]}`` — not the bare voice_id string.

The dict then flowed through ``_speaker_voices[speaker_id] = _sp_voice``
into downstream TTS / voice-validation code that does
``voice_id.startswith("vt_")`` → ``AttributeError: 'dict' object has
no attribute 'startswith'`` and crashed the pipeline.

This bug was latent since PR#3C-b2 (May 14, 2026) — the b2 comment
said "b3 will plug voice_match_resolver explicitly for the
resolution; b2 trusts auto_matched_voice". b3 never landed the
extraction. v8 happy-path E2E (P3-b-fix) succeeded because quota
allowed CLONED branch; P3-d E2E hit PRESET fallback and exposed the
bug.

This test pins that the PRESET branch correctly extracts the
``voice_id`` STRING from the dict.
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestPresetVoiceIdStringExtraction:
    """Source-level regression check: PRESET branch must NOT assign
    auto_matched_voice (dict) directly to _sp_entry["voice_id"]."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_preset_branch_extracts_voice_id_string_from_dict(self):
        """The PRESET branch (after voice_review auto-approve) must
        extract the ``voice_id`` STRING from the auto_matched_voice
        DICT. Anti-pattern to forbid:

            _sp_entry["voice_id"] = _sp_entry.get("auto_matched_voice") or ""

        because auto_matched_voice is built by _auto_match_for_provider
        as a dict ``{"voice_id": ..., "label": ..., ...}``.

        Correct shape (any equivalent works):

            _auto = _sp_entry.get("auto_matched_voice")
            if isinstance(_auto, dict):
                _sp_entry["voice_id"] = _auto.get("voice_id") or ""
            elif isinstance(_auto, str):
                _sp_entry["voice_id"] = _auto
            else:
                _sp_entry["voice_id"] = ""
        """
        source = self._source()

        # Locate the PRESET branch (anchor on VoiceReviewChoice.PRESET
        # comparison line which sits inside the for-loop iterating
        # _smart_voice_review.decisions).
        preset_marker = "_dec.choice == VoiceReviewChoice.PRESET"
        idx = source.find(preset_marker)
        assert idx >= 0, (
            "PRESET branch marker not found — process.py shape changed."
        )

        # Inspect ~1500 chars after the marker (the branch body —
        # bumped from 600 to fit the post-fix comment block + the
        # isinstance(dict/str) extraction).
        body = source[idx : idx + 1500]

        # Forbid the broken anti-pattern (whitespace-flexible regex check).
        import re
        broken_pattern = re.compile(
            r'_sp_entry\["voice_id"\]\s*=\s*\(\s*'
            r'_sp_entry\.get\("auto_matched_voice"\)\s*or\s*""\s*\)',
            re.DOTALL,
        )
        assert not broken_pattern.search(body), (
            "PRESET branch assigns _sp_entry['voice_id'] directly to "
            "auto_matched_voice (a dict). Pipeline crashes downstream "
            "with 'dict' object has no attribute 'startswith'. "
            "Extract the voice_id string from the dict instead.\n"
            f"PRESET branch body:\n{body}"
        )

        # Require an extraction pattern that handles dict + str + None.
        # Acceptable signal: presence of ``isinstance`` near the
        # auto_matched_voice access (proves caller defends against the
        # dict shape).
        has_extraction = (
            "isinstance" in body
            and "auto_matched_voice" in body
        )
        assert has_extraction, (
            "PRESET branch must use isinstance() to defensively extract "
            "voice_id from auto_matched_voice (which can be a dict or "
            "None). Without the type check the dict flows into "
            "_speaker_voices and crashes downstream.\n"
            f"PRESET branch body:\n{body}"
        )

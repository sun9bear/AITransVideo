"""Tests for ``DubbingSegment.tts_input_cn_text`` (2026-05-04 P0a).

The field records the EXACT text fed to the TTS engine for the audio
currently at ``aligned_audio_path``. Its purpose is to detect drift when
a user edits ``cn_text`` via the studio editor without regenerating TTS:
``cn_text != tts_input_cn_text`` ⇒ subtitle text won't match audio.

Test coverage:
- Field default is empty string.
- Aligner snapshots ``tts_input_cn_text`` alongside ``first_pass_cn_text``.
- Post-TTS rewrite re-stamps ``tts_input_cn_text`` (overwrite),
  but preserves ``first_pass_cn_text`` (the first-pass guardrail).
- editor/segments.json round-trip preserves the field.
- ``accept_draft_tts`` re-stamps when the user accepts a per-segment
  re-TTS draft.
- ``regenerate_all_dirty_segments`` re-stamps on every re-synthesized
  segment, leaves ``accepted`` segments untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make ``src/`` importable when pytest runs from the repo root. The package
# guard mirrors what other test files in this repo do.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# A1: dataclass field default
# ---------------------------------------------------------------------------


def test_dubbing_segment_has_tts_input_cn_text_default_empty():
    """A freshly-constructed DubbingSegment has ``tts_input_cn_text == ""``.

    Empty default means: until alignment runs, we have no claim about which
    text produced the audio. Downstream consumers must treat empty as
    "unknown" — never assume "in sync" implicitly.
    """
    from services.gemini.translator import DubbingSegment
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello", cn_text="你好",
    )
    assert seg.tts_input_cn_text == ""

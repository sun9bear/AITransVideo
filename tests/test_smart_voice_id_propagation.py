"""Smart auto-approve voice_id propagation guard (2026-05-16 P0).

== Production incident ==

job_887984b649684645a05b5fccbc71d6be (admin smart, 2026-05-16 12:24 UTC,
124-segment Musk interview). Pipeline ran to completion:

  - smart_decisions.jsonl reported: REUSED vt_speaker_a_1778930206296 +
    vt_speaker_b_1778930225912 (clone library hit by same-content-hash).
  - Per-speaker user_voices DB rows correctly created/looked up.
  - voice_clone metering: billing_policy=
    ``reuse_existing_user_voice_no_clone_charge``, reuse=True.

But the actual TTS dispatch told a different story:

  - segments.json: 124/124 segments had ``voice_id="auto"``.
  - TTS metering: 176 calls with voice_id="auto", selected_voice fell
    through to PRESET (Chinese_radio_reporter_vv1 / radio_host_male /
    storyteller_vv2 / Wise_Woman).
  - User reported "感觉并没有克隆音色, 但是个人音色库又自动克隆了音色".

== Root cause ==

The Studio voice-selection path at process.py:3107-3108 propagates the
selected voice IDs into ``voice_id_a`` / ``voice_id_b`` local vars
before the S3 translate call::

    voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)
    voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)

The smart auto-approve branch (process.py:3974-4332) populates
``_speaker_voices`` correctly via
``_apply_smart_reused_voice_decision`` / the CLONED branch /
the PRESET branch, but it NEVER updates ``voice_id_a`` /
``voice_id_b`` from that dict. Translator.translate() then receives
the unchanged initial values (``"auto"`` for smart jobs that didn't
specify voices up-front), stamps every segment with
``voice_id="auto"``, and the TTS dispatcher's auto-match fallback
picks preset voices.

For >2-speaker jobs the bug was less visible because translator
gets ``speaker_voices=_speaker_voices`` (line ~4633) which IS
populated. But the 2-speaker default path used by 99% of smart
jobs hits ``speaker_voices=None`` and only uses ``voice_id`` /
``voice_id_b`` — which were never updated.

== Fix ==

Add the same voice_id_a/voice_id_b propagation at the end of the
smart auto-approve branch (right before the fall-through to S4-probe
Phase 2 / S3 translate), mirroring the Studio path at line 3107.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestSmartAutoApprovePropagatesVoiceIds:
    """Pin the voice_id_a/voice_id_b propagation in the smart auto-approve
    branch so the bug from job_887984b... cannot recur silently."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_smart_auto_approve_block_assigns_voice_id_a_from_speaker_voices(self):
        """The smart auto-approve branch must reassign ``voice_id_a``
        from ``_speaker_voices["speaker_a"]`` so the cloned/reused
        voice decision actually reaches translator.translate()."""
        source = self._source()
        # Anchor: the [S2.5] auto-approve print line is unique to the
        # smart auto-approve branch.
        anchor = '[S2.5] Smart 自动批准 voice_selection_review'
        anchor_idx = source.find(anchor)
        assert anchor_idx >= 0, "smart auto-approve anchor not found"

        # Look in the 3000 chars AFTER the anchor (the fix lives between
        # the print and the fall-through comment; the explanatory comment
        # block above the fix is intentionally verbose).
        post = source[anchor_idx : anchor_idx + 3000]
        assert 'voice_id_a = _speaker_voices.get("speaker_a"' in post, (
            "Smart auto-approve must propagate _speaker_voices['speaker_a'] "
            "into the voice_id_a local before falling through to S3 translate. "
            "Without this, translator.translate() stamps voice_id='auto' on "
            "every segment and TTS falls back to preset voices "
            "(production incident 2026-05-16: job_887984b...). "
            f"Anchor + 3000 chars:\n{post[:3000]}"
        )
        assert 'voice_id_b = _speaker_voices.get("speaker_b"' in post, (
            "Smart auto-approve must also propagate "
            "_speaker_voices['speaker_b'] for the 2-speaker default path "
            "(translator uses voice_id_b kwarg, not the speaker_voices dict "
            "which is only passed when effective_speakers > 2). "
            f"Anchor + 3000 chars:\n{post[:3000]}"
        )

    def test_propagation_appears_after_minor_speaker_assignments(self):
        """The propagation must happen AFTER ``_speaker_voices`` is fully
        populated (after minor speaker assignments at line ~4140-4162)
        and BEFORE the fall-through. If placed too early, minor-speaker
        assignments wouldn't be visible (not relevant for 2-speaker but
        important for >2-speaker correctness)."""
        source = self._source()
        # Find the smart auto-approve print anchor
        anchor = '[S2.5] Smart 自动批准 voice_selection_review'
        anchor_idx = source.find(anchor)
        # Find the fall-through marker
        fallthrough = "# Fall through to next pipeline stage"
        fallthrough_idx = source.find(fallthrough, anchor_idx)
        assert fallthrough_idx > anchor_idx, (
            "fall-through marker must come after the auto-approve print"
        )
        between = source[anchor_idx:fallthrough_idx]
        assert (
            'voice_id_a = _speaker_voices.get("speaker_a"' in between
            and 'voice_id_b = _speaker_voices.get("speaker_b"' in between
        ), (
            "voice_id_a/voice_id_b propagation must live between the "
            "[S2.5] auto-approve print and the 'Fall through to next "
            "pipeline stage' comment so it runs on the happy-path before "
            "S4-probe Phase 2 and S3 translate. Between window:\n"
            f"{between[:800]}"
        )

    def test_propagation_pattern_matches_studio_path(self):
        """Studio path uses an identical pattern at line ~3107. The smart
        path's update must match exactly so future maintenance touches
        both sites consistently."""
        source = self._source()
        # Both should use the same getter pattern with voice_id_a/_b
        # fallback to current value
        for marker in (
            'voice_id_a = _speaker_voices.get("speaker_a", voice_id_a)',
            'voice_id_b = _speaker_voices.get("speaker_b", voice_id_b)',
        ):
            count = source.count(marker)
            assert count >= 2, (
                f"Expected at least 2 occurrences of `{marker}` (one in "
                f"Studio approved-payload path at line ~3107-3108, one in "
                f"the new smart auto-approve fix). Found {count}.\n"
                f"If the Studio site moved or was rewritten, update this "
                f"guard with the new pattern."
            )

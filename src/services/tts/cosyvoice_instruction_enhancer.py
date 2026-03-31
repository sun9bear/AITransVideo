"""CosyVoice instruction enhancer — wraps selector with optional instruction generation.

B1: instruction feature is gated OFF via static flag. Returns selector result
with instruction=None. Extension point for future Instruct support once the
DashScope endpoint enables it for the active model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from services.tts.cosyvoice_voice_selector import VoiceMatchResult, select_voice_match

# Static feature gate — no runtime probe, no env var check.
# Flip to True when the DashScope endpoint supports instruction
# for the active model (cosyvoice-v3-flash) on the deployed endpoint.
INSTRUCT_ENABLED: Final[bool] = False

# Voices known to have Instruct capability metadata in the official catalog.
# This set is informational only; actual instruction generation is gated by
# INSTRUCT_ENABLED regardless of voice membership.
_INSTRUCT_CAPABLE_VOICES: Final[frozenset[str]] = frozenset({
    "longanyang",
    "longanhuan",
    "longhuhu_v3",
})


@dataclass(frozen=True, slots=True)
class EnhancedVoiceResult:
    """Voice selection result with optional instruction metadata."""

    voice_id: str
    match_reason: str
    match_score: float
    match_confidence: str  # "high" | "medium" | "low"
    backup_voices: tuple[str, ...]
    instruction: str | None  # Always None when INSTRUCT_ENABLED is False
    instruct_supported: bool  # Whether the selected voice has Instruct capability


def enhance_voice_selection(
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
    *,
    is_childlike: bool = False,
) -> EnhancedVoiceResult:
    """Select a voice and optionally generate an instruction string.

    Delegates to select_voice_match() for the base voice selection, then
    wraps the result with instruction metadata. In B1, instruction is
    always None (INSTRUCT_ENABLED=False).
    """
    match = select_voice_match(
        gender=gender,
        age_group=age_group,
        persona_style=persona_style,
        energy_level=energy_level,
        is_childlike=is_childlike,
    )

    instruct_supported = match.voice_id in _INSTRUCT_CAPABLE_VOICES
    instruction: str | None = None

    # Future: when INSTRUCT_ENABLED is True and instruct_supported:
    #   instruction = _generate_instruction(match.voice_id, persona_style, energy_level)

    return EnhancedVoiceResult(
        voice_id=match.voice_id,
        match_reason=match.match_reason,
        match_score=match.match_score,
        match_confidence=match.match_confidence,
        backup_voices=match.backup_voices,
        instruction=instruction,
        instruct_supported=instruct_supported,
    )

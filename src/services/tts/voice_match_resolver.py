"""Unified voice match resolver — dispatches to provider-specific matchers.

Entry point: ``resolve_voice_match(request)``

Current dispatch table:
- ``mode="manual"``  → bypass matcher, return explicit voice directly
- ``volcengine``     → ``volcengine_voice_selector.select_volcengine_voice_match()``
- ``cosyvoice``      → NOT connected this round (uses its own direct path)
- unknown provider   → raises ``UnsupportedProviderError``

CosyVoice keeps its existing ``cosyvoice_voice_selector`` + ``cosyvoice_instruction_enhancer``
call chain in ``tts_generator._generate_one_cosyvoice()``.  It will be migrated to this
resolver in a future round.
"""

from __future__ import annotations

import logging

from services.tts.voice_match_types import VoiceMatchRequest, VoiceMatchResult

logger = logging.getLogger(__name__)


class UnsupportedProviderError(Exception):
    """Raised when the resolver receives a provider it cannot dispatch."""


def resolve_voice_match(request: VoiceMatchRequest) -> VoiceMatchResult:
    """Resolve a voice match request to a concrete voice ID.

    Parameters
    ----------
    request:
        A ``VoiceMatchRequest`` describing the provider, demographics,
        and mode.

    Returns
    -------
    VoiceMatchResult
        The selected voice with match metadata.

    Raises
    ------
    UnsupportedProviderError
        If ``request.tts_provider`` is not supported by this resolver.
    """

    # --- Manual mode: bypass all matchers ---
    if request.mode == "manual" and request.explicit_voice_id:
        logger.info(
            "[VoiceResolver] manual mode → %s (provider=%s)",
            request.explicit_voice_id, request.tts_provider,
        )
        return VoiceMatchResult(
            voice_id=request.explicit_voice_id,
            match_reason="manual_selection",
            match_score=1.0,
            match_confidence="high",
            backup_voices=(),
        )

    # --- Provider dispatch ---
    if request.tts_provider == "volcengine":
        return _dispatch_volcengine(request)

    # Future: cosyvoice dispatch will go here.  For now CosyVoice uses its
    # own direct path in tts_generator._generate_one_cosyvoice().

    raise UnsupportedProviderError(
        f"Voice match resolver does not support provider {request.tts_provider!r}. "
        f"Supported: volcengine (this round). CosyVoice uses its own direct path."
    )


def _dispatch_volcengine(request: VoiceMatchRequest) -> VoiceMatchResult:
    """Dispatch to the VolcEngine voice selector.

    Imports lazily to avoid circular dependencies and to keep the selector
    as an independent module.
    """
    from services.tts.volcengine_voice_selector import select_volcengine_voice_match

    return select_volcengine_voice_match(
        resource_id=request.resource_id or "",
        gender=request.gender,
        age_group=request.age_group,
        persona_style=request.persona_style,
        energy_level=request.energy_level,
    )

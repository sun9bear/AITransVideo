"""Unified voice match resolver — dispatches to provider-specific matchers.

Entry point: ``resolve_voice_match(request)``

Dispatch table:
- ``mode="manual"``  → bypass matcher, return explicit voice directly
- ``volcengine``     → ``volcengine_voice_selector.select_volcengine_voice_match()``
- ``cosyvoice``      → ``cosyvoice_voice_selector.select_cosyvoice_voice_match()``
- unknown provider   → raises ``UnsupportedProviderError``
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

    if request.tts_provider == "cosyvoice":
        return _dispatch_cosyvoice(request)

    raise UnsupportedProviderError(
        f"Voice match resolver does not support provider {request.tts_provider!r}. "
        f"Supported: volcengine, cosyvoice."
    )


def _dispatch_volcengine(request: VoiceMatchRequest) -> VoiceMatchResult:
    """Dispatch to the VolcEngine voice selector."""
    from services.tts.volcengine_voice_selector import select_volcengine_voice_match

    return select_volcengine_voice_match(
        resource_id=request.resource_id or "",
        gender=request.gender,
        age_group=request.age_group,
        persona_style=request.persona_style,
        energy_level=request.energy_level,
    )


def _dispatch_cosyvoice(request: VoiceMatchRequest) -> VoiceMatchResult:
    """Dispatch to the CosyVoice voice selector."""
    from services.tts.cosyvoice_voice_selector import (
        infer_is_childlike,
        select_cosyvoice_voice_match,
    )

    # Infer childlike from demographics + voice_description
    is_childlike = infer_is_childlike(
        request.age_group or "",
        request.voice_description or "",
    )

    return select_cosyvoice_voice_match(
        gender=request.gender,
        age_group=request.age_group,
        persona_style=request.persona_style,
        energy_level=request.energy_level,
        is_childlike=is_childlike,
    )

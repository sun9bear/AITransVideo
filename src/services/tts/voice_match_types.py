"""Shared types for cross-provider voice matching.

These types define the contract between the Generator layer and
provider-specific voice selectors (VolcEngine, CosyVoice, etc.).

The **resolver** (``voice_match_resolver.py``) accepts a ``VoiceMatchRequest``
and dispatches to the appropriate provider matcher, returning a unified
``VoiceMatchResult``.

Current status (this round):
- VolcEngine: fully connected via resolver
- CosyVoice: keeps its own ``cosyvoice_voice_selector.VoiceMatchResult``
  and direct call path — not yet migrated to this shared layer
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceMatchRequest:
    """Input to the shared voice matcher.

    Attributes
    ----------
    tts_provider:
        Logical provider name (``"volcengine"``, ``"cosyvoice"``, etc.).
    resource_id:
        Provider-specific resource identifier.  For VolcEngine this is
        ``"seed-tts-1.0"`` or ``"seed-tts-2.0"``.  May be *None* for
        providers that don't use this concept.
    mode:
        ``"auto"`` — run demographic matching.
        ``"manual"`` — caller has an explicit voice; matcher is bypassed.
    gender:
        ``"male"`` / ``"female"`` / ``"child"`` / *None*.
    age_group:
        ``"young"`` / ``"middle"`` / ``"elderly"`` / *None*.
    persona_style:
        E.g. ``"professional"``, ``"warm"``, ``"serious"``, ``"energetic"``.
    energy_level:
        ``"low"`` / ``"medium"`` / ``"high"`` / *None*.
    voice_description:
        Free-text description from the LLM review.
    explicit_voice_id:
        When ``mode="manual"``, this is the user-selected voice ID.
        The resolver returns it directly without running any matcher.
    """

    tts_provider: str
    resource_id: str | None = None
    mode: str = "auto"  # "auto" | "manual"
    gender: str | None = None
    age_group: str | None = None
    persona_style: str | None = None
    energy_level: str | None = None
    voice_description: str | None = None
    explicit_voice_id: str | None = None
    target_language: str | None = None
    # Task 2: target Chinese hanzi/sec (source_english_words_per_second × 1.8).
    # None → speed dimension disabled.  Upstream callers that don't know the
    # source speaker's rate should leave this as None; the reranker gracefully
    # degrades.
    target_chars_per_second: float | None = None


@dataclass(frozen=True, slots=True)
class VoiceMatchResult:
    """Output from the shared voice matcher.

    Field semantics are intentionally identical to
    ``cosyvoice_voice_selector.VoiceMatchResult`` so that a future
    migration is a drop-in replacement.
    """

    voice_id: str
    match_reason: str
    match_score: float
    match_confidence: str  # "high" | "medium" | "low"
    backup_voices: tuple[str, ...] = ()

"""Shared voice reranker — provider-agnostic multi-dimensional scoring.

Extracted from volcengine_voice_selector.py so that VolcEngine, CosyVoice,
and future providers (MiniMax, etc.) share one scoring pipeline.

Weight distribution (total 1.0, tuned for video dubbing):

  Catalog tags (0.40):
    age_group       0.22  — age perception is the strongest dubbing cue
    persona_style   0.18  — semantic style match

  Profile labels (0.60):
    pitch_level     0.20  — #1 perceptual dimension for listeners
    maturity        0.10  — acoustic age verification (cross-checks catalog)
    energy_level    0.10  — speaker energy alignment
    delivery_style  0.08  — correlated with persona; kept low to avoid double-counting
    childlike       0.07  — child-voice detection
    texture_tags    0.05  — voice timbre match
"""

from __future__ import annotations

import logging
import time
from typing import Final

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Age-group normalisation
# ---------------------------------------------------------------------------
_AGE_ELDERLY: Final[frozenset[str]] = frozenset({"elderly", "old", "senior"})
_AGE_YOUNG: Final[frozenset[str]] = frozenset({"young", "youth"})
_AGE_MIDDLE: Final[frozenset[str]] = frozenset({"middle", "adult", "mature"})


def resolve_age_bucket(age_group: str | None) -> str:
    """Normalise free-form age_group to one of young/middle/elderly/''."""
    age = (age_group or "").lower().strip()
    if age in _AGE_ELDERLY:
        return "elderly"
    if age in _AGE_YOUNG:
        return "young"
    if age in _AGE_MIDDLE:
        return "middle"
    return ""


# ---------------------------------------------------------------------------
# Scoring mapping tables (provider-agnostic)
# ---------------------------------------------------------------------------

MATURITY_MAP: Final[dict[str, str]] = {
    "young": "young", "youth": "young",
    "middle": "adult", "adult": "adult", "mature": "adult",
    "elderly": "elder", "old": "elder", "senior": "elder",
    "child": "child",
}

GENDER_PITCH: Final[dict[str, set[str]]] = {
    "female": {"mid", "high"},
    "male": {"low", "mid"},
    "child": {"high"},
}

PERSONA_TEXTURE: Final[dict[str, set[str]]] = {
    "warm": {"soft", "magnetic"},
    "professional": {"steady", "crisp"},
    "serious": {"steady", "magnetic"},
    "energetic": {"crisp", "bright"},
    "cute": {"soft", "airy"},
    "neutral": set(),
}

PERSONA_DELIVERY: Final[dict[str, set[str]]] = {
    "professional": {"narration"},
    "serious": {"narration"},
    "warm": {"companion", "storyteller"},
    "energetic": {"storyteller", "narration"},
    "cute": {"companion"},
    "neutral": {"narration"},
}

PERSONA_ADJACENT: Final[dict[str, set[str]]] = {
    "professional": {"serious", "neutral"},
    "serious": {"professional", "neutral"},
    "warm": {"cute", "neutral"},
    "energetic": {"warm", "neutral"},
    "cute": {"warm", "energetic"},
    "neutral": {"professional", "warm"},
}

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

# Catalog tags
W_AGE_EXACT: Final[float] = 0.22
W_AGE_BUCKET: Final[float] = 0.10
W_PERSONA_EXACT: Final[float] = 0.18
W_PERSONA_ADJACENT: Final[float] = 0.07

# Profile labels
W_PITCH: Final[float] = 0.20
W_MATURITY: Final[float] = 0.10
W_ENERGY: Final[float] = 0.10
W_DELIVERY: Final[float] = 0.08
W_CHILDLIKE: Final[float] = 0.07
W_TEXTURE: Final[float] = 0.05

# Confidence thresholds
CONFIDENCE_HIGH: Final[float] = 0.45
CONFIDENCE_MEDIUM: Final[float] = 0.25


# ---------------------------------------------------------------------------
# Core scoring function
# ---------------------------------------------------------------------------

def combined_rerank(
    candidates: list[dict],
    profiles: dict[str, dict],
    *,
    gender: str,
    age_bucket: str,
    persona: str,
    energy: str,
) -> list[tuple[str, float]]:
    """Score and rank voice candidates using multi-dimensional matching.

    Parameters
    ----------
    candidates:
        Voice dicts with at least ``voice_id``, and optionally
        ``age_group``, ``persona_style``, ``energy_level``.
    profiles:
        ``voice_id → profile_dict`` with profile fields
        (``pitch_level``, ``maturity``, ``childlike``, ``texture_tags``,
        ``delivery_style``, ``energy_level``).
    gender:
        Normalised speaker gender (``male`` / ``female`` / ``child``).
    age_bucket:
        Normalised age bucket (``young`` / ``middle`` / ``elderly`` / ``""``).
    persona:
        Normalised persona style (``professional`` / ``warm`` / …).
    energy:
        Normalised energy level (``low`` / ``medium`` / ``high`` / ``""``).

    Returns
    -------
    list[tuple[str, float]]
        ``(voice_id, score)`` sorted descending.  Empty when *candidates*
        is empty.
    """
    if not candidates:
        return []

    expected_maturity = MATURITY_MAP.get(age_bucket or "", "adult")
    preferred_pitch = GENDER_PITCH.get(gender, {"mid"})
    preferred_texture = PERSONA_TEXTURE.get(persona, set())
    preferred_delivery = PERSONA_DELIVERY.get(persona, set())

    scored: list[tuple[str, float]] = []
    for v in candidates:
        vid = v["voice_id"]
        score = 0.0
        p = profiles.get(vid, {})

        # --- Catalog tag scoring (0.40) ---
        v_age = str(v.get("age_group", "") or "").lower().strip()
        v_persona = str(v.get("persona_style", "") or "").lower().strip()

        # Age match: exact catalog > same maturity bucket
        if age_bucket and v_age == age_bucket:
            score += W_AGE_EXACT
        elif age_bucket and v_age and MATURITY_MAP.get(v_age) == expected_maturity:
            score += W_AGE_BUCKET

        # Persona match: exact > adjacent
        if persona and v_persona == persona:
            score += W_PERSONA_EXACT
        elif persona and v_persona and v_persona in PERSONA_ADJACENT.get(persona, set()):
            score += W_PERSONA_ADJACENT

        # --- Profile label scoring (0.60) ---
        # Pitch (0.20)
        if p.get("pitch_level") in preferred_pitch:
            score += W_PITCH

        # Maturity (0.10)
        if p.get("maturity") == expected_maturity:
            score += W_MATURITY

        # Energy (0.10) — profile energy, fallback to catalog energy
        p_energy = str(p.get("energy_level", "") or v.get("energy_level", "") or "").lower().strip()
        if energy and p_energy == energy:
            score += W_ENERGY

        # Delivery style (0.08)
        p_delivery = str(p.get("delivery_style", "") or "").lower().strip()
        if preferred_delivery and p_delivery in preferred_delivery:
            score += W_DELIVERY

        # Childlike (0.07)
        is_child = age_bucket == "child" or gender == "child"
        if p.get("childlike") is not None and p["childlike"] == is_child:
            score += W_CHILDLIKE

        # Texture (0.05)
        voice_textures = set(p.get("texture_tags") or [])
        if preferred_texture and voice_textures & preferred_texture:
            score += W_TEXTURE

        scored.append((vid, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def score_to_confidence(score: float) -> str:
    """Map a combined_rerank score to a confidence label."""
    if score >= CONFIDENCE_HIGH:
        return "high"
    if score >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Profile loader (Gateway internal API, provider-agnostic)
# ---------------------------------------------------------------------------

_profile_cache: dict[str, tuple[dict[str, dict], float]] = {}
_PROFILE_CACHE_TTL = 120.0  # seconds
_GATEWAY_URL = "http://127.0.0.1:8880/api/internal/voice-catalog"


def load_profiles(
    provider: str,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
) -> dict[str, dict]:
    """Load voice profiles from Gateway internal API (with TTL cache).

    Works for any provider — VolcEngine, CosyVoice, MiniMax, etc.
    Returns ``{voice_id: profile_dict}`` for voices that have at least
    one profile dimension populated.
    """
    cache_key = f"{provider}:{resource_id or ''}:{endpoint_mode or ''}"
    cached = _profile_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _PROFILE_CACHE_TTL:
        return cached[0]

    try:
        params: dict[str, str] = {"provider": provider}
        if resource_id:
            params["resource_id"] = resource_id
        if endpoint_mode:
            params["endpoint_mode"] = endpoint_mode

        resp = requests.get(_GATEWAY_URL, params=params, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()

        profiles: dict[str, dict] = {}
        for v in data.get("voices", []):
            # Include any voice with at least one reranker-relevant field
            if (v.get("maturity") or v.get("pitch_level") or v.get("childlike") is not None
                    or v.get("delivery_style") or v.get("texture_tags") or v.get("energy_level")):
                profiles[v["voice_id"]] = v

        _profile_cache[cache_key] = (profiles, time.time())
        return profiles
    except Exception as exc:
        logger.debug("profile load failed (provider=%s): %s", provider, exc)
        existing = _profile_cache.get(cache_key)
        return existing[0] if existing else {}

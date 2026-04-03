"""VolcEngine (豆包) B1 baseline voice selector.

Matches speaker demographics to the best voice in the VolcEngine catalog.
Designed to mirror the CosyVoice B1 matcher style:

1. Style override: (gender, age_bucket, persona_style) → specific voice
2. Base map: gender + age_bucket → default for that bucket
3. Gender-only: gender → default for gender
4. Fallback: resource default safe voice

All results are constrained to a single resource_id — the selector
NEVER returns a voice belonging to a different resource.
"""

from __future__ import annotations

import logging
from typing import Final

from services.tts.voice_match_types import VoiceMatchResult
from services.tts.volcengine_voice_catalog import (
    get_default_voice_id,
    get_voices_for_resource,
)
from services.tts.volcengine_tts_provider import RESOURCE_ID_1_0, RESOURCE_ID_2_0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Age-group normalisation (same aliases as CosyVoice selector)
# ---------------------------------------------------------------------------
_AGE_ELDERLY: Final[frozenset[str]] = frozenset({"elderly", "old", "senior"})
_AGE_YOUNG: Final[frozenset[str]] = frozenset({"young", "youth"})
_AGE_MIDDLE: Final[frozenset[str]] = frozenset({"middle", "adult", "mature"})


def _resolve_age_bucket(age_group: str | None) -> str:
    age = (age_group or "").lower().strip()
    if age in _AGE_ELDERLY:
        return "elderly"
    if age in _AGE_YOUNG:
        return "young"
    if age in _AGE_MIDDLE:
        return "middle"
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_volcengine_voice_match(
    *,
    resource_id: str,
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
) -> VoiceMatchResult:
    """Select the best VolcEngine voice for the given demographics.

    Resolution order (B1 baseline):
    1. Try (gender, age_bucket, persona_style) exact match in pool
    2. Try (gender, age_bucket) match
    3. Try gender-only match
    4. Fallback to resource default safe voice

    Parameters
    ----------
    resource_id:
        ``"seed-tts-1.0"`` or ``"seed-tts-2.0"``.
    gender:
        ``"male"`` / ``"female"`` / ``"child"`` / *None*.
    age_group:
        ``"young"`` / ``"middle"`` / ``"elderly"`` / *None*.
    persona_style:
        E.g. ``"professional"``, ``"warm"``, ``"serious"``, ``"energetic"``.
    energy_level:
        ``"low"`` / ``"medium"`` / ``"high"`` — used as tiebreaker.
    """
    pool = get_voices_for_resource(resource_id)
    default_voice = get_default_voice_id(resource_id)

    if not gender:
        logger.info("[VolcEngine-matcher] No gender, fallback=%s (resource=%s)", default_voice, resource_id)
        return VoiceMatchResult(
            voice_id=default_voice,
            match_reason=f"fallback(no_gender,resource={resource_id})",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    g = gender.lower().strip()
    age_bucket = _resolve_age_bucket(age_group)
    persona = (persona_style or "").lower().strip()

    # --- 1. Style override: gender + age_bucket + persona_style ---
    if persona and age_bucket:
        candidates = [
            v for v in pool
            if v["gender"] == g
            and _resolve_age_bucket(str(v.get("age_group", ""))) == age_bucket
            and (v.get("persona_style", "") or "").lower().strip() == persona
        ]
        if candidates:
            voice = candidates[0]
            backups = _pick_backups(pool, g, voice["voice_id"])
            best, remaining = _try_rerank_with_profiles(
                voice["voice_id"], backups, g, age_bucket or "", persona, resource_id=resource_id,
            )
            logger.info(
                "[VolcEngine-matcher] style_override: %s (gender=%s, age=%s, persona=%s, resource=%s)",
                best, g, age_bucket, persona, resource_id,
            )
            return VoiceMatchResult(
                voice_id=best,
                match_reason=f"style_override({g},{age_bucket},{persona})",
                match_score=0.85,
                match_confidence="high",
                backup_voices=remaining,
            )

    # --- 2. Base map: gender + age_bucket ---
    if age_bucket:
        candidates = [
            v for v in pool
            if v["gender"] == g
            and _resolve_age_bucket(str(v.get("age_group", ""))) == age_bucket
        ]
        if candidates:
            voice = candidates[0]
            backups = _pick_backups(pool, g, voice["voice_id"])
            best, remaining = _try_rerank_with_profiles(
                voice["voice_id"], backups, g, age_bucket, resource_id=resource_id,
            )
            logger.info(
                "[VolcEngine-matcher] base_age: %s (gender=%s, age=%s, resource=%s)",
                best, g, age_bucket, resource_id,
            )
            return VoiceMatchResult(
                voice_id=best,
                match_reason=f"base_age({g},{age_bucket})",
                match_score=0.60,
                match_confidence="medium",
                backup_voices=remaining,
            )

    # --- 3. Gender-only ---
    candidates = [v for v in pool if v["gender"] == g]
    if candidates:
        voice = candidates[0]
        backups = _pick_backups(pool, g, voice["voice_id"])
        best, remaining = _try_rerank_with_profiles(
            voice["voice_id"], backups, g, "", resource_id=resource_id,
        )
        logger.info(
            "[VolcEngine-matcher] gender_only: %s (gender=%s, resource=%s)",
            best, g, resource_id,
        )
        return VoiceMatchResult(
            voice_id=best,
            match_reason=f"gender_only({g})",
            match_score=0.40,
            match_confidence="low",
            backup_voices=remaining,
        )

    # --- 4. Ultimate fallback ---
    logger.info(
        "[VolcEngine-matcher] fallback: %s (gender=%s unrecognized, resource=%s)",
        default_voice, g, resource_id,
    )
    return VoiceMatchResult(
        voice_id=default_voice,
        match_reason=f"fallback(unrecognized_gender={g},resource={resource_id})",
        match_score=0.20,
        match_confidence="low",
        backup_voices=(),
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# B2 Profile-based rerank — activated in Phase 4 via Gateway voice_labels DB.
# Uses 4 dimensions: maturity (0.30), childlike (0.20), pitch (0.30),
# texture_tags (0.20).
# ---------------------------------------------------------------------------

import logging as _logging
import time as _time

import requests as _requests

_rerank_logger = _logging.getLogger(__name__)

# Cache for voice profiles: resource_id → {voice_id: profile_dict}
_profile_cache: dict[str, tuple[dict[str, dict], float]] = {}
_PROFILE_CACHE_TTL = 120.0  # seconds

_MATURITY_MAP = {
    "young": "young", "youth": "young",
    "middle": "adult", "adult": "adult", "mature": "adult",
    "elderly": "elder", "old": "elder", "senior": "elder",
    "child": "child",
}

_GENDER_PITCH = {
    "female": {"mid", "high"},
    "male": {"low", "mid"},
    "child": {"high"},
}

_PERSONA_TEXTURE = {
    "warm": {"soft", "magnetic"},
    "professional": {"steady", "crisp"},
    "serious": {"steady", "magnetic"},
    "energetic": {"crisp", "bright"},
    "cute": {"soft", "airy"},
    "neutral": set(),
}


def _load_profiles(resource_id: str) -> dict[str, dict]:
    """Load voice profiles from Gateway internal API (with cache)."""
    cached = _profile_cache.get(resource_id)
    if cached and (_time.time() - cached[1]) < _PROFILE_CACHE_TTL:
        return cached[0]

    try:
        resp = _requests.get(
            "http://127.0.0.1:8880/api/internal/voice-catalog",
            params={"provider": "volcengine", "resource_id": resource_id},
            timeout=3.0,
        )
        resp.raise_for_status()
        data = resp.json()

        # Build profile lookup from voices that have label data
        profiles: dict[str, dict] = {}
        for v in data.get("voices", []):
            # Only include voices with at least one profile dimension
            if v.get("maturity") or v.get("pitch_level") or v.get("childlike") is not None:
                profiles[v["voice_id"]] = v

        _profile_cache[resource_id] = (profiles, _time.time())
        return profiles
    except Exception as exc:
        _rerank_logger.debug("profile load failed: %s", exc)
        return _profile_cache.get(resource_id, ({}, 0))[0]


def _try_rerank_with_profiles(
    primary_voice: str,
    backup_voices: tuple[str, ...],
    speaker_gender: str,
    speaker_age: str,
    speaker_persona: str = "",
    resource_id: str = "seed-tts-1.0",
) -> tuple[str, tuple[str, ...]]:
    """Rerank primary + backup voices using profile data (when available).

    Scoring (4 dimensions, total 1.0):
    - Maturity match:  0.30
    - Childlike match: 0.20
    - Pitch preference: 0.30
    - Texture match:   0.20

    Returns (best_voice, remaining_backups).
    If no profiles available → returns original unchanged.
    """
    profiles = _load_profiles(resource_id)
    if not profiles:
        return primary_voice, backup_voices

    candidates = [primary_voice, *backup_voices]
    scored: list[tuple[str, float]] = []

    expected_maturity = _MATURITY_MAP.get(speaker_age, "adult")
    preferred_pitch = _GENDER_PITCH.get(speaker_gender, {"mid"})
    preferred_texture = _PERSONA_TEXTURE.get(speaker_persona, set())

    for vid in candidates:
        p = profiles.get(vid)
        if not p:
            scored.append((vid, 0.0))
            continue

        score = 0.0

        # Maturity match (0.30)
        if p.get("maturity") == expected_maturity:
            score += 0.30

        # Childlike match (0.20)
        is_child_speaker = speaker_age in ("child",) or speaker_gender == "child"
        if p.get("childlike") is not None:
            if p["childlike"] == is_child_speaker:
                score += 0.20

        # Pitch preference (0.30)
        if p.get("pitch_level") in preferred_pitch:
            score += 0.30

        # Texture match (0.20)
        voice_textures = set(p.get("texture_tags") or [])
        if preferred_texture and voice_textures & preferred_texture:
            score += 0.20

        scored.append((vid, score))

    if not scored:
        return primary_voice, backup_voices

    scored.sort(key=lambda x: x[1], reverse=True)
    best = scored[0][0]
    remaining = tuple(vid for vid, _ in scored[1:])

    if best != primary_voice:
        _rerank_logger.info(
            "[rerank] %s → %s (score %.2f vs %.2f)",
            primary_voice, best, scored[0][1],
            next((s for v, s in scored if v == primary_voice), 0),
        )

    return best, remaining


def _pick_backups(
    pool: list[dict],
    gender: str,
    primary_voice_id: str,
    *,
    max_count: int = 2,
) -> tuple[str, ...]:
    """Pick up to *max_count* backup voices of the same gender, excluding primary."""
    same_gender = [
        v["voice_id"] for v in pool
        if v["gender"] == gender and v["voice_id"] != primary_voice_id
    ]
    return tuple(same_gender[:max_count])

"""VolcEngine (豆包) voice selector — uses shared combined_rerank scoring.

VolcEngine-specific logic:
- resource_id scoping (seed-tts-1.0 / seed-tts-2.0)
- ICL_zh_ language preference filter
- Child pool expansion with childlike=true voices

Scoring is delegated to ``voice_reranker.combined_rerank``.
"""

from __future__ import annotations

import logging

from services.tts.voice_match_types import VoiceMatchResult
from services.tts.voice_reranker import (
    combined_rerank,
    load_profiles,
    resolve_age_bucket,
    score_to_confidence,
)
from services.tts.volcengine_voice_catalog import (
    get_default_voice_id,
    get_voices_for_resource,
)

logger = logging.getLogger(__name__)


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
    target_chars_per_second: float | None = None,
) -> VoiceMatchResult:
    """Select the best VolcEngine voice using shared combined_rerank.

    Flow:
    1. Filter pool by gender (male / female / child)
    2. Expand child pool if < 3 voices (add childlike=true from other genders)
    3. Prefer Chinese voices (ICL_zh_ prefix)
    4. Score ALL candidates via combined_rerank
    5. Return top-scored voice + remaining as backups
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
    age_bucket = resolve_age_bucket(age_group)
    persona = (persona_style or "").lower().strip()
    energy = (energy_level or "").lower().strip()

    # --- Step 1: Gender filter ---
    candidates = [v for v in pool if v["gender"] == g]

    # Child expansion: if child pool too small, add childlike=true from other genders
    if g == "child" and len(candidates) < 3:
        profiles = load_profiles("volcengine", resource_id=resource_id)
        childlike_extras = [
            v for v in pool
            if v["gender"] != "child"
            and v["voice_id"] in profiles
            and profiles[v["voice_id"]].get("childlike") is True
        ]
        candidates.extend(childlike_extras)
        if childlike_extras:
            logger.info(
                "[VolcEngine-matcher] child pool expanded: %d child + %d childlike",
                len(candidates) - len(childlike_extras), len(childlike_extras),
            )

    # --- Step 1b: Language filter — prefer Chinese voices ---
    zh_candidates = [v for v in candidates if v["voice_id"].startswith("ICL_zh_")]
    if zh_candidates:
        candidates = zh_candidates

    if not candidates:
        logger.info(
            "[VolcEngine-matcher] fallback: %s (gender=%s no candidates, resource=%s)",
            default_voice, g, resource_id,
        )
        return VoiceMatchResult(
            voice_id=default_voice,
            match_reason=f"fallback(no_candidates,gender={g},resource={resource_id})",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    # --- Step 2: Combined scoring via shared reranker ---
    profiles = load_profiles("volcengine", resource_id=resource_id)
    scored = combined_rerank(
        candidates, profiles,
        gender=g, age_bucket=age_bucket, persona=persona, energy=energy,
        target_chars_per_second=target_chars_per_second,
    )

    best_vid = scored[0][0]
    best_score = scored[0][1]
    remaining = tuple(vid for vid, _ in scored[1:6])
    confidence = score_to_confidence(best_score)

    logger.info(
        "[VolcEngine-matcher] combined_rerank: %s (score=%.2f, gender=%s, age=%s, persona=%s, "
        "energy=%s, pool=%d, resource=%s, confidence=%s)",
        best_vid, best_score, g, age_bucket, persona, energy,
        len(candidates), resource_id, confidence,
    )
    return VoiceMatchResult(
        voice_id=best_vid,
        match_reason=f"combined_rerank({g},pool={len(candidates)})",
        match_score=best_score,
        match_confidence=confidence,
        backup_voices=remaining,
    )

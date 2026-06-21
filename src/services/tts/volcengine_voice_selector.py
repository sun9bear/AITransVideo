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

# PR-E slice 1 (re-CodeX P1): VolcEngine voice_id language families. Chinese clones
# are ``ICL_zh_*``; English seed-tts voices are ``en_*`` (e.g. en_male_tim_uranus_bigtts)
# and rarely ``ICL_en_*``. The catalog ``language`` field is the robust signal when
# present (MiniMax-style localized "英语" included for safety).
_VOLC_LANG_PREFIXES: dict[str, tuple[str, ...]] = {"en": ("en_", "ICL_en_")}
_VOLC_LANG_META: dict[str, tuple[str, ...]] = {"en": ("en", "英语", "english")}


def _volc_voice_matches_target(voice: dict, lang_code: str) -> bool:
    """Whether a VolcEngine catalog voice serves the target language.

    zh keeps the EXACT legacy ``ICL_zh_`` prefix test (byte-identical — no language
    metadata widening). Non-zh matches the language's voice_id prefix families or the
    catalog ``language`` metadata.
    """
    vid = str(voice.get("voice_id", "") or "")
    if lang_code == "zh":
        return vid.startswith("ICL_zh_")
    prefixes = _VOLC_LANG_PREFIXES.get(lang_code, (f"{lang_code}_", f"ICL_{lang_code}_"))
    if any(vid.startswith(p) for p in prefixes):
        return True
    meta = _VOLC_LANG_META.get(lang_code, (lang_code,))
    return str(voice.get("language", "") or "").strip().lower() in meta


def select_volcengine_voice_match(
    *,
    resource_id: str,
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
    target_chars_per_second: float | None = None,
    target_language: str | None = None,
) -> VoiceMatchResult:
    """Select the best VolcEngine voice using shared combined_rerank.

    Flow:
    1. Filter pool by gender (male / female / child)
    2. Expand child pool if < 3 voices (add childlike=true from other genders)
    3. Prefer Chinese voices (ICL_zh_ prefix)
    4. Score ALL candidates via combined_rerank
    5. Return top-scored voice + remaining as backups
    """
    pool = get_voices_for_resource(resource_id, target_language=target_language)
    _lang_code = (target_language or "zh-CN").split("-")[0].lower()
    default_voice = get_default_voice_id(resource_id)
    # PR-E re-CodeX P2: the resource default is a Chinese voice. For a non-zh target
    # derive the no-gender fallback from the target-language pool so an English dub
    # without reviewer gender isn't voiced in Chinese. zh keeps the legacy default.
    if _lang_code != "zh":
        default_voice = next(
            (v["voice_id"] for v in pool if _volc_voice_matches_target(v, _lang_code)),
            default_voice,
        )

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

    # --- Step 1b: Language filter — prefer voices in the TARGET language ---
    # zh → the legacy ICL_zh_ family (byte-identical); en → the en_ / ICL_en_ seed-tts
    # families + catalog language metadata (re-CodeX P1 — ICL_en_ alone matched nothing,
    # leaking zh voices). Falls back to the full candidate set when nothing matches.
    lang_candidates = [v for v in candidates if _volc_voice_matches_target(v, _lang_code)]
    if _lang_code != "zh" and not lang_candidates:
        # re-CodeX P2 (fail-closed, v3 D): a non-zh target with NO target-language
        # candidate must NOT be scored against the Chinese pool — that would voice an
        # English dub in Chinese. Return the target-aware default (an en voice if the
        # pool has any) with a clear low-confidence reason. The matchable migration +
        # get_fallback_provider keep VolcEngine out of the en path upstream; this is the
        # last-line defense. zh is unaffected (it intentionally falls back to all below).
        logger.warning(
            "[VolcEngine-matcher] no %s candidates (gender=%s, resource=%s); failing "
            "closed to %s instead of a Chinese voice",
            _lang_code, g, resource_id, default_voice,
        )
        return VoiceMatchResult(
            voice_id=default_voice,
            match_reason=f"fail_closed(no_{_lang_code}_voice,resource={resource_id})",
            match_score=0.15,
            match_confidence="low",
            backup_voices=(),
        )
    if lang_candidates:
        candidates = lang_candidates

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

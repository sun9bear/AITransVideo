"""MiniMax voice selector — uses shared combined_rerank scoring.

MiniMax-specific logic:
- Target language pre-filter (41 languages, must narrow before scoring)
- Default to Chinese (普通话) if no target language specified
- Large pool (~604 voices) — language filter is critical for performance

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

logger = logging.getLogger(__name__)

FALLBACK_VOICE = "Wise_Woman"
FALLBACK_VOICE_MALE = "Calm_Man"

# Language display name → normalized key for matching
_LANGUAGE_ALIASES: dict[str, str] = {
    "zh": "中文-普通话", "chinese": "中文-普通话", "mandarin": "中文-普通话",
    "zh-cn": "中文-普通话", "zh-hans": "中文-普通话",
    "cantonese": "中文-粤语", "yue": "中文-粤语", "zh-hk": "中文-粤语",
    "en": "英语", "english": "英语",
    "ja": "日语", "japanese": "日语",
    "ko": "韩语", "korean": "韩语",
    "es": "西班牙语", "spanish": "西班牙语",
    "pt": "葡萄牙语", "portuguese": "葡萄牙语",
    "fr": "法语", "french": "法语",
    "de": "德语", "german": "德语",
    "ru": "俄语", "russian": "俄语",
    "it": "意大利语", "italian": "意大利语",
    "id": "印尼语", "indonesian": "印尼语",
    "th": "泰语", "thai": "泰语",
    "vi": "越南语", "vietnamese": "越南语",
    "ar": "阿拉伯语", "arabic": "阿拉伯语",
    "hi": "印地语", "hindi": "印地语",
    "tr": "土耳其语", "turkish": "土耳其语",
}


def _resolve_language(target_language: str | None) -> str:
    """Normalize target language to MiniMax catalog language name."""
    if not target_language:
        return "中文-普通话"
    raw = target_language.strip().lower()
    # Try alias lookup first
    if raw in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[raw]
    # Try direct match (already in catalog format like "中文-普通话")
    return target_language.strip()


def _load_minimax_pool() -> list[dict]:
    """Load matchable MiniMax voices from Gateway (with static fallback)."""
    try:
        import os
        import requests
        key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
        headers = {"X-Internal-Key": key} if key else {}
        resp = requests.get(
            "http://127.0.0.1:8880/api/internal/voice-catalog",
            params={"provider": "minimax"},
            timeout=3.0,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json().get("voices", [])
    except Exception:
        # Static fallback: load from JSON catalog
        return _static_fallback()


def _static_fallback() -> list[dict]:
    """Load from the exported JSON catalog when Gateway is unavailable."""
    import json
    from pathlib import Path
    path = Path(__file__).parent / "minimax_voice_catalog_604.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def select_minimax_voice_match(
    *,
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
    target_language: str | None = None,
    target_chars_per_second: float | None = None,
) -> VoiceMatchResult:
    """Select the best MiniMax voice using shared combined_rerank.

    Flow:
    1. Load voice pool from Gateway (or static fallback)
    2. Filter by target language (critical for 41-language pool)
    3. Filter by gender
    4. Score via combined_rerank
    5. Return top-scored voice + backups
    """
    if not gender:
        fallback = FALLBACK_VOICE
        logger.info("[MiniMax-matcher] No gender, fallback=%s", fallback)
        return VoiceMatchResult(
            voice_id=fallback,
            match_reason="fallback(no_gender)",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    g = gender.lower().strip()
    age_bucket = resolve_age_bucket(age_group)
    persona = (persona_style or "").lower().strip()
    energy = (energy_level or "").lower().strip()
    lang = _resolve_language(target_language)

    # --- Step 1: Load pool ---
    pool = _load_minimax_pool()

    # --- Step 2: Language filter (critical) ---
    lang_pool = [v for v in pool if v.get("language") == lang]
    if not lang_pool:
        # Broaden: try parent language (e.g. "中文-粤语" → any "中文-*")
        lang_prefix = lang.split("-")[0] if "-" in lang else lang
        lang_pool = [v for v in pool if v.get("language", "").startswith(lang_prefix)]
    if not lang_pool:
        # Ultimate fallback: default Chinese pool
        lang_pool = [v for v in pool if v.get("language") == "中文-普通话"]

    # --- Step 3: Gender filter ---
    candidates = [v for v in lang_pool if v.get("gender") == g]

    if not candidates:
        fallback = FALLBACK_VOICE_MALE if g == "male" else FALLBACK_VOICE
        logger.info(
            "[MiniMax-matcher] no candidates for gender=%s, lang=%s, fallback=%s",
            g, lang, fallback,
        )
        return VoiceMatchResult(
            voice_id=fallback,
            match_reason=f"fallback(no_candidates,gender={g},lang={lang})",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    # --- Step 4: Check if pool has structured catalog tags ---
    has_catalog_tags = any(v.get("age_group") or v.get("persona_style") for v in candidates[:5])

    if not has_catalog_tags:
        # Static fallback — traits are in flat format, not reranker-compatible.
        # Use basic heuristic: pick first voice matching gender.
        best = candidates[0]
        logger.info(
            "[MiniMax-matcher] static fallback scoring, best=%s (gender=%s, lang=%s)",
            best["voice_id"], g, lang,
        )
        return VoiceMatchResult(
            voice_id=best["voice_id"],
            match_reason=f"static_fallback({g},lang={lang})",
            match_score=0.30,
            match_confidence="low",
            backup_voices=tuple(v["voice_id"] for v in candidates[1:6]),
        )

    # --- Step 5: Combined scoring via shared reranker ---
    profiles = load_profiles("minimax")
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
        "[MiniMax-matcher] combined_rerank: %s (score=%.2f, gender=%s, age=%s, persona=%s, "
        "energy=%s, lang=%s, pool=%d, confidence=%s)",
        best_vid, best_score, g, age_bucket, persona, energy,
        lang, len(candidates), confidence,
    )
    return VoiceMatchResult(
        voice_id=best_vid,
        match_reason=f"combined_rerank({g},lang={lang},pool={len(candidates)})",
        match_score=best_score,
        match_confidence=confidence,
        backup_voices=remaining,
    )

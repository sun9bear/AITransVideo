"""CosyVoice v3 voice selector — shared combined_rerank + legacy fallback.

All voice IDs are official cosyvoice-v3-flash presets from:
https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list

Two public APIs:
- ``select_cosyvoice_voice_match()``  — NEW: shared combined_rerank via Gateway profiles
- ``select_voice_match()``            — LEGACY: hardcoded _BASE_MAP + B2 rerank (kept as fallback)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from services.tts.voice_reranker import resolve_age_bucket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persona / energy normalizer — keyword-based, deterministic
# ---------------------------------------------------------------------------

_PERSONA_RULES: list[tuple[str, list[str]]] = [
    ("professional", ["主持", "采访", "专业", "财经", "清晰", "知性", "记者", "播音", "新闻", "理性"]),
    ("warm",         ["温和", "柔和", "亲切", "从容", "温暖", "治愈", "居家", "暖"]),
    ("serious",      ["睿智", "沉稳", "严肃", "低沉", "深沉", "沧桑", "磁性", "理智"]),
    ("energetic",    ["元气", "活泼", "轻快", "热情", "高亢", "阳光", "欢脱", "激情"]),
]

_ENERGY_RULES: list[tuple[str, list[str]]] = [
    ("low",    ["缓慢", "沉稳", "从容", "低沉", "沧桑", "深沉"]),
    ("high",   ["活泼", "轻快", "热情", "高亢", "激情", "欢脱", "元气"]),
    ("medium", ["适中", "平稳", "清晰", "专业", "知性", "温和"]),
]


def infer_persona_style(text: str) -> str:
    """Infer persona_style from voice_description / style text."""
    if not text:
        return ""
    for style, keywords in _PERSONA_RULES:
        for kw in keywords:
            if kw in text:
                return style
    return ""


def infer_energy_level(text: str) -> str:
    """Infer energy_level from voice_description / style text."""
    if not text:
        return ""
    for level, keywords in _ENERGY_RULES:
        for kw in keywords:
            if kw in text:
                return level
    return ""


_CHILDLIKE_KEYWORDS: list[str] = [
    "童声", "儿童", "小朋友", "小孩", "少儿", "幼儿",
    "小男孩", "小女孩", "男孩", "女孩", "boy", "girl", "child", "kid",
]


def infer_is_childlike(age_group: str, voice_description: str) -> bool:
    """Infer whether the speaker is a child based on age_group and voice_description."""
    if (age_group or "").lower().strip() in ("child", "kid", "children"):
        return True
    desc = (voice_description or "").lower()
    for kw in _CHILDLIKE_KEYWORDS:
        if kw in desc:
            return True
    return False


# ---------------------------------------------------------------------------
# CosyVoice v3-flash voice map — all IDs verified against official docs
# ---------------------------------------------------------------------------

# Base buckets (gender + age)
_BASE_MAP: dict[str, str] = {
    "male":           "longanyang",       # 龙安洋，阳光大男孩，20-30
    "female":         "longanhuan",       # 龙安欢，欢脱元气女，20-30
    "child":          "longhuhu_v3",      # 龙呼呼，天真烂漫女童，6-10
    "male_elderly":   "longlaobo_v3",     # 龙老伯，饱经沧桑老年男，60+
    "male_young":     "longanyang",       # 复用龙安洋
    "male_middle":    "longanzhi_v3",     # 龙安智，睿智轻熟男声，25-45
    "female_young":   "longanhuan",       # 复用龙安欢
    "female_middle":  "longyingjing_v3",  # 龙应静，沉稳从容女，25-35
    "female_elderly": "longlaoyi_v3",     # 龙老姨，饱经世事老年女，60+
    "child_young":    "longhuhu_v3",      # 龙呼呼，童声标杆
    "child_middle":   "longjielidou_v3",  # 龙杰力豆，阳光顽皮男童 age 10
}

# Style micro-tuning: (gender, age_bucket, persona_style) -> voice override
_STYLE_OVERRIDES: dict[tuple[str, str, str], str] = {
    ("female", "middle", "professional"): "longyingjing_v3",   # 沉稳从容 — 主持人
    ("female", "middle", "warm"):         "longanwen_v3",      # 优雅知性 — 温暖
    ("female", "middle", "energetic"):    "longanhuan",        # 欢脱元气 — 活泼
    ("female", "middle", "serious"):      "longxiaoxia_v3",    # 沉稳权威 — 严肃
    ("male",   "middle", "serious"):      "longanzhi_v3",      # 睿智沉稳 — 严肃
    ("male",   "middle", "professional"): "longanzhi_v3",      # 睿智轻熟 — 专业
    ("male",   "middle", "warm"):         "longanyun_v3",      # 居家暖男 — 温暖
    ("male",   "young",  "energetic"):    "longanyang",        # 阳光大男孩
    ("male",   "young",  "serious"):      "longcheng_v3",      # 睿智少年
}

FALLBACK_VOICE = "longanyang"


def select_voice(
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
) -> str:
    """Select a CosyVoice v3 preset voice.

    Resolution order:
    1. Try (gender, age_bucket, persona_style) in style overrides
    2. Fall back to (gender + age_bucket) base map
    3. Fall back to gender-only base map
    4. Fall back to FALLBACK_VOICE
    """
    if not gender:
        logger.info("[CosyVoice] no gender, fallback=%s", FALLBACK_VOICE)
        return FALLBACK_VOICE

    g = gender.lower().strip()
    persona = (persona_style or "").lower().strip()
    energy = (energy_level or "").lower().strip()

    # Resolve age bucket (shared single source — voice_reranker, DRY-04)
    age_bucket = resolve_age_bucket(age_group)

    # 1. Try style override
    if persona and age_bucket:
        key = (g, age_bucket, persona)
        if key in _STYLE_OVERRIDES:
            voice = _STYLE_OVERRIDES[key]
            logger.info(
                "[CosyVoice] voice=%s, gender=%s, age=%s, persona=%s, energy=%s (style override)",
                voice, g, age_bucket, persona, energy,
            )
            return voice

    # 2. Try base map with age
    if age_bucket:
        base_key = f"{g}_{age_bucket}"
        if base_key in _BASE_MAP:
            voice = _BASE_MAP[base_key]
            logger.info(
                "[CosyVoice] voice=%s, gender=%s, age=%s, persona=%s, energy=%s (base+age)",
                voice, g, age_bucket, persona, energy,
            )
            return voice

    # 3. Gender-only fallback
    if g in _BASE_MAP:
        voice = _BASE_MAP[g]
        logger.info(
            "[CosyVoice] voice=%s, gender=%s, age=%s, persona=%s, energy=%s (gender-only)",
            voice, g, age_bucket or "(none)", persona or "(none)", energy or "(none)",
        )
        return voice

    # 4. Ultimate fallback
    logger.info("[CosyVoice] unrecognized gender=%r, fallback=%s", g, FALLBACK_VOICE)
    return FALLBACK_VOICE


# ---------------------------------------------------------------------------
# Structured match result — B1 baseline matcher
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VoiceMatchResult:
    """Structured output from select_voice_match()."""

    voice_id: str
    match_reason: str
    match_score: float
    match_confidence: str  # "high" | "medium" | "low"
    backup_voices: tuple[str, ...]


# ---------------------------------------------------------------------------
# B2 profile-based reranking (high-confidence dimensions only)
# ---------------------------------------------------------------------------

# Pitch expectations by gender
_PITCH_PREFERENCE: dict[str, tuple[str, ...]] = {
    "male": ("low", "mid"),
    "female": ("mid", "high"),
    "child": ("high",),
}

# Texture associations by persona_style
_TEXTURE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "serious": ("steady", "magnetic"),
    "professional": ("steady", "crisp"),
    "warm": ("soft",),
    "energetic": ("crisp",),
}


def _rerank_with_profiles(
    primary_voice: str,
    backup_voices: tuple[str, ...],
    speaker_gender: str,
    speaker_age: str,
    speaker_persona: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Rerank primary + backup voices using B2 profile data.

    Only uses high-confidence dimensions: pitch_level, texture_tags, maturity, childlike.
    warmth/authority/intimacy/delivery_style are NOT used (A/B test showed convergence).

    Returns (best_voice, remaining_backups).
    If no profile data available → returns original unchanged.
    """
    from services.tts.cosyvoice_voice_profile_catalog import get_voice_profile

    candidates = [primary_voice, *backup_voices]
    scored: list[tuple[str, float]] = []

    for vid in candidates:
        profile = get_voice_profile(vid)
        if profile is None:
            continue

        score = 0.0
        p = profile.primary
        s = profile.secondary

        # Maturity match (0.3): age_group alignment
        age = speaker_age.lower().strip() if speaker_age else ""
        maturity_map = {"young": "young", "middle": "adult", "elderly": "elder", "child": "child"}
        expected_maturity = maturity_map.get(age, "")
        if expected_maturity and s.maturity == expected_maturity:
            score += 0.3
        elif expected_maturity:
            # Partial credit for adjacent
            score += 0.1

        # Childlike match (0.2)
        is_child = speaker_gender.lower().strip() == "child" if speaker_gender else False
        if s.childlike == is_child:
            score += 0.2

        # Pitch match (0.3): gender-appropriate pitch
        g = speaker_gender.lower().strip() if speaker_gender else ""
        preferred_pitches = _PITCH_PREFERENCE.get(g, ("mid",))
        if p.pitch_level in preferred_pitches:
            score += 0.3
        else:
            score += 0.1  # partial

        # Texture match (0.2): persona_style alignment
        persona = speaker_persona.lower().strip() if speaker_persona else ""
        preferred_textures = _TEXTURE_PREFERENCE.get(persona, ())
        if preferred_textures and any(t in s.texture_tags for t in preferred_textures):
            score += 0.2
        elif not preferred_textures:
            score += 0.1  # no preference → neutral credit

        scored.append((vid, score))

    if not scored:
        return primary_voice, backup_voices

    scored.sort(key=lambda x: x[1], reverse=True)
    best = scored[0][0]
    remaining = tuple(vid for vid, _ in scored[1:] if vid != best)
    return best, remaining


def _get_endpoint_safe_pool() -> list[dict[str, str | bool]]:
    """Get the voice pool filtered by current runtime endpoint availability."""
    from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode
    from services.tts.cosyvoice_voice_catalog import list_endpoint_available_voices
    return list_endpoint_available_voices(get_runtime_endpoint_mode())


def _pick_backup_voices(
    primary_voice: str,
    gender: str,
    age_bucket: str,
    *,
    max_count: int = 2,
) -> tuple[str, ...]:
    """Pick up to max_count backup voices from the endpoint-safe pool."""
    pool = _get_endpoint_safe_pool()
    # Filter same gender, exclude primary
    candidates = [
        v for v in pool
        if v["gender"] == gender and v["voice_id"] != primary_voice
    ]
    if not candidates:
        return ()

    # Prefer voices in the base map / style overrides (known-good voices)
    known_good = set(_BASE_MAP.values()) | {v for v in _STYLE_OVERRIDES.values()}
    preferred = [v for v in candidates if v["voice_id"] in known_good]
    rest = [v for v in candidates if v["voice_id"] not in known_good]
    ordered = preferred + rest
    return tuple(v["voice_id"] for v in ordered[:max_count])


def select_voice_match(
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
    *,
    is_childlike: bool = False,
) -> VoiceMatchResult:
    """Select a CosyVoice v3 preset voice with structured match metadata.

    Uses the same resolution logic as select_voice() but returns a
    VoiceMatchResult with match_score, match_confidence, and backup_voices.

    When *is_childlike* is True, the effective gender is forced to "child"
    regardless of the original gender value.
    """
    effective_gender = gender
    if is_childlike:
        effective_gender = "child"

    if not effective_gender:
        return VoiceMatchResult(
            voice_id=FALLBACK_VOICE,
            match_reason="fallback(no_gender)",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    g = effective_gender.lower().strip()
    age_bucket = resolve_age_bucket(age_group)
    persona = (persona_style or "").lower().strip()

    def _ensure_available(voice: str, reason: str, score: float, confidence: str) -> VoiceMatchResult:
        """Check endpoint availability, apply B2 rerank for low/medium confidence."""
        from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode, is_voice_available
        mode = get_runtime_endpoint_mode()

        if not is_voice_available(voice, mode):
            pool = _get_endpoint_safe_pool()
            same_gender = [v for v in pool if v["gender"] == g]
            voice = same_gender[0]["voice_id"] if same_gender else FALLBACK_VOICE
            reason = f"endpoint_fallback({reason}->{voice},mode={mode})"
            score = max(score - 0.15, 0.20)
            confidence = "low" if confidence == "high" else confidence

        backups = _pick_backup_voices(voice, g, age_bucket)

        # B2 rerank: only for low/medium confidence with backup options
        if confidence in ("low", "medium") and backups:
            best, remaining = _rerank_with_profiles(
                voice, backups, g, age_bucket, persona,
            )
            if best != voice:
                reason = f"{reason}+reranked({voice}->{best})"
                score = min(score + 0.05, 1.0)
                voice = best
                backups = remaining

        return VoiceMatchResult(
            voice_id=voice,
            match_reason=reason,
            match_score=score,
            match_confidence=confidence,
            backup_voices=backups,
        )

    # 1. Style override
    if persona and age_bucket:
        key = (g, age_bucket, persona)
        if key in _STYLE_OVERRIDES:
            voice = _STYLE_OVERRIDES[key]
            return _ensure_available(voice, f"style_override({g},{age_bucket},{persona})", 0.85, "high")

    # 2. Base map with age
    if age_bucket:
        base_key = f"{g}_{age_bucket}"
        if base_key in _BASE_MAP:
            voice = _BASE_MAP[base_key]
            return _ensure_available(voice, f"base_age({g},{age_bucket})", 0.60, "medium")

    # 3. Gender-only
    if g in _BASE_MAP:
        voice = _BASE_MAP[g]
        return _ensure_available(voice, f"gender_only({g})", 0.40, "low")

    # 4. Ultimate fallback
    return VoiceMatchResult(
        voice_id=FALLBACK_VOICE,
        match_reason=f"fallback(unrecognized_gender={g})",
        match_score=0.20,
        match_confidence="low",
        backup_voices=(),
    )


# ---------------------------------------------------------------------------
# NEW: Shared combined_rerank scoring via Gateway profiles
# ---------------------------------------------------------------------------

from services.tts.voice_match_types import VoiceMatchResult as SharedVoiceMatchResult


def select_cosyvoice_voice_match(
    *,
    gender: str | None,
    age_group: str | None = None,
    persona_style: str | None = None,
    energy_level: str | None = None,
    is_childlike: bool = False,
    target_chars_per_second: float | None = None,
    target_language: str | None = None,
) -> SharedVoiceMatchResult:
    """Select the best CosyVoice voice using shared combined_rerank.

    Flow:
    1. Load matchable voice pool from Gateway (with static fallback)
    2. Filter by endpoint availability (international / mainland)
    3. Filter by gender (with childlike override)
    4. Score ALL candidates via combined_rerank (same as VolcEngine)
    5. Return top-scored voice + backups

    Falls back to legacy ``select_voice_match()`` when Gateway pool
    is empty or scoring produces no results.
    """
    from services.tts.cosyvoice_endpoint_config import (
        get_runtime_endpoint_mode,
        is_voice_available,
    )
    from services.tts.cosyvoice_voice_catalog import list_matchable_cosyvoice_voices
    from services.tts.voice_reranker import (
        combined_rerank,
        load_profiles,
        score_to_confidence,
    )

    # PR-E re-CodeX P2: CosyVoice is Chinese-only — fail closed for ANY non-zh target
    # BEFORE the no-gender / pool paths (those would otherwise return the Chinese
    # FALLBACK_VOICE with a non-fail_closed reason, which the TTS caller would synthesize
    # as wrong-language audio). Routing (get_fallback_provider) + the matchable migration
    # keep en away from CosyVoice; this is the last-line defense. zh unchanged.
    if target_language and target_language not in ("zh-CN", "zh"):
        logger.warning(
            "[CosyVoice-matcher] non-zh target_language=%s reached CosyVoice (Chinese-only); "
            "failing closed (check routing/matchable, PR-E)",
            target_language,
        )
        return SharedVoiceMatchResult(
            voice_id=FALLBACK_VOICE,
            match_reason=f"fail_closed(cosyvoice_zh_only,target={target_language})",
            match_score=0.15,
            match_confidence="low",
            backup_voices=(),
        )

    effective_gender = "child" if is_childlike else gender
    if not effective_gender:
        logger.info("[CosyVoice-matcher] No gender, fallback=%s", FALLBACK_VOICE)
        return SharedVoiceMatchResult(
            voice_id=FALLBACK_VOICE,
            match_reason="fallback(no_gender)",
            match_score=0.20,
            match_confidence="low",
            backup_voices=(),
        )

    g = effective_gender.lower().strip()
    age_bucket = resolve_age_bucket(age_group)
    persona = (persona_style or "").lower().strip()
    energy = (energy_level or "").lower().strip()

    # --- Step 1: Load voice pool ---
    endpoint_mode = get_runtime_endpoint_mode()
    pool = list_matchable_cosyvoice_voices(target_language=target_language)

    # --- Step 1b: Detect static fallback ---
    # Static catalog entries lack Gateway-provided demographic tags
    # (age_group, persona_style, energy_level).  When that happens the
    # combined_rerank catalog-tag scoring is meaningless, so we fall
    # back to the legacy deterministic matcher.
    _has_catalog_tags = any(v.get("age_group") or v.get("persona_style") for v in pool[:5])
    if not _has_catalog_tags:
        logger.info(
            "[CosyVoice-matcher] static fallback detected (no catalog tags), using legacy matcher",
        )
        legacy = select_voice_match(
            gender, age_group=age_group, persona_style=persona_style,
            energy_level=energy_level, is_childlike=is_childlike,
        )
        return SharedVoiceMatchResult(
            voice_id=legacy.voice_id,
            match_reason=f"legacy_fallback({legacy.match_reason})",
            match_score=legacy.match_score,
            match_confidence=legacy.match_confidence,
            backup_voices=legacy.backup_voices,
        )

    # --- Step 2: Endpoint availability filter ---
    pool = [v for v in pool if is_voice_available(str(v["voice_id"]), endpoint_mode)]

    # --- Step 3: Gender filter ---
    candidates = [v for v in pool if v.get("gender") == g]

    # Child expansion: if pool is tiny, add childlike=true from other genders
    if g == "child" and len(candidates) < 3:
        profiles = load_profiles("cosyvoice", endpoint_mode=endpoint_mode)
        childlike_extras = [
            v for v in pool
            if v.get("gender") != "child"
            and v["voice_id"] in profiles
            and profiles[v["voice_id"]].get("childlike") is True
        ]
        candidates.extend(childlike_extras)

    if not candidates:
        logger.info(
            "[CosyVoice-matcher] no candidates for gender=%s, mode=%s, falling back to legacy",
            g, endpoint_mode,
        )
        legacy = select_voice_match(
            gender, age_group=age_group, persona_style=persona_style,
            energy_level=energy_level, is_childlike=is_childlike,
        )
        return SharedVoiceMatchResult(
            voice_id=legacy.voice_id,
            match_reason=f"legacy_fallback({legacy.match_reason})",
            match_score=legacy.match_score,
            match_confidence=legacy.match_confidence,
            backup_voices=legacy.backup_voices,
        )

    # --- Step 4: Combined scoring via shared reranker ---
    profiles = load_profiles("cosyvoice", endpoint_mode=endpoint_mode)
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
        "[CosyVoice-matcher] combined_rerank: %s (score=%.2f, gender=%s, age=%s, persona=%s, "
        "energy=%s, pool=%d, mode=%s, confidence=%s)",
        best_vid, best_score, g, age_bucket, persona, energy,
        len(candidates), endpoint_mode, confidence,
    )
    return SharedVoiceMatchResult(
        voice_id=best_vid,
        match_reason=f"combined_rerank({g},pool={len(candidates)})",
        match_score=best_score,
        match_confidence=confidence,
        backup_voices=remaining,
    )

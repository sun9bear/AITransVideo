"""Shared voice reranker — provider-agnostic multi-dimensional scoring.

Extracted from volcengine_voice_selector.py so that VolcEngine, CosyVoice,
and future providers (MiniMax, etc.) share one scoring pipeline.

IMPORTANT — gender is already hard-filtered upstream (inside each provider
selector, e.g. ``minimax_voice_selector.py``).  The reranker therefore
scores within a same-gender candidate pool; new dimensions added here
cannot cause gender mismatch.

Weight distribution (total ~1.0, tuned for video dubbing):

  Catalog tags (0.40):
    age_group       0.22  — age perception is the strongest dubbing cue
    persona_style   0.18  — semantic style match

  Profile labels (0.50):
    pitch_level     0.20  — #1 perceptual dimension for listeners
    maturity        0.10  — acoustic age verification (cross-checks catalog)
    energy_level    0.10  — speaker energy alignment
    delivery_style  0.06  — correlated with persona; kept low to avoid double-counting
    childlike       0.04  — child-voice detection
    texture_tags    0.03  — voice timbre match

  Speech rate (adaptive 0.05–0.30) — Task 2 (2026-04-14):
    speed_match     dyn   — match voice chars_per_second against target.
                            Weight scales with how far the speaker's target
                            deviates from the library-average baseline:
                              < ±10% → 0.05 (let persona dominate)
                              ±10–35% → linear 0.05 → 0.30
                              > ±35% → 0.30 (extreme speakers like Munger)
                            NULL-safe: voices without calibration get 0.
                            target=None disables the dimension entirely.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
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
W_DELIVERY: Final[float] = 0.06   # was 0.08 — gave 0.02 to W_SPEED
W_CHILDLIKE: Final[float] = 0.04  # was 0.07 — gave 0.03 to W_SPEED
W_TEXTURE: Final[float] = 0.03    # was 0.05 — gave 0.02 to W_SPEED

# Speech rate match (Task 2 — adaptive, scales with speaker deviation)
W_SPEED: Final[float] = 0.10                 # legacy constant (default mid-band)
W_SPEED_MIN: Final[float] = 0.05             # weight at deviation ≤ 10%
W_SPEED_MAX: Final[float] = 0.30             # weight at deviation ≥ 35%
W_SPEED_BASELINE_CPS: Final[float] = 4.20    # library-average chars/sec
                                             # (mean across 173 calibrated voices,
                                             #  ≈ "neutral Chinese listening pace")
W_SPEED_DEVIATION_LOW: Final[float] = 0.10   # below this → minimal weight
W_SPEED_DEVIATION_HIGH: Final[float] = 0.35  # above this → maximal weight


_VOICE_MATCH_SPEED_DIMENSION_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")


def is_voice_match_speed_dimension_enabled() -> bool:
    """CodeX P2-4: gate W_SPEED behind admin flag for canary rollout.

    Defaults to False so the reranker falls back to the legacy 8-dimension
    score until ops verifies the speed-dimension behaviour on real jobs.
    """
    try:
        if _VOICE_MATCH_SPEED_DIMENSION_SETTINGS_PATH.exists():
            import json
            with _VOICE_MATCH_SPEED_DIMENSION_SETTINGS_PATH.open(encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return bool(cfg.get("voice_match_speed_dimension_enabled", False))
    except Exception:
        pass
    return False


def compute_w_speed(target_chars_per_second: float | None) -> tuple[float, float]:
    """Adaptive W_SPEED: scale with how extreme the target speech rate is.

    Speakers within ±10% of the library baseline (≈ 4.20 cps) get only a
    light speed bonus — persona/age dimensions should dominate.  Extreme
    speakers (Charlie Munger ~2.7 cps, fast podcasts ~6.3 cps) get the full
    weight, so the few rare slow/fast voices in the catalog can rise to the
    top of the recommendation list.

    Returns
    -------
    (w_speed, deviation)
        ``w_speed`` is the actual weight to apply; ``deviation`` is the
        absolute relative gap between target and baseline (used for logging).
        Both are 0 when the target is missing/invalid OR when the admin
        feature flag voice_match_speed_dimension_enabled is False (CodeX P2-4).
    """
    # CodeX P2-4: respect the admin gate before doing any speed math.
    if not is_voice_match_speed_dimension_enabled():
        return 0.0, 0.0
    if target_chars_per_second is None or target_chars_per_second <= 0:
        return 0.0, 0.0
    if W_SPEED_BASELINE_CPS <= 0:
        return W_SPEED_MIN, 0.0
    deviation = abs(target_chars_per_second - W_SPEED_BASELINE_CPS) / W_SPEED_BASELINE_CPS
    if deviation <= W_SPEED_DEVIATION_LOW:
        return W_SPEED_MIN, deviation
    if deviation >= W_SPEED_DEVIATION_HIGH:
        return W_SPEED_MAX, deviation
    span = W_SPEED_DEVIATION_HIGH - W_SPEED_DEVIATION_LOW
    weight_span = W_SPEED_MAX - W_SPEED_MIN
    progress = (deviation - W_SPEED_DEVIATION_LOW) / span
    return W_SPEED_MIN + progress * weight_span, deviation


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
    target_chars_per_second: float | None = None,
) -> list[tuple[str, float]]:
    """Score and rank voice candidates using multi-dimensional matching.

    Parameters
    ----------
    candidates:
        Voice dicts with at least ``voice_id``, and optionally
        ``age_group``, ``persona_style``, ``energy_level``,
        ``chars_per_second``.
    profiles:
        ``voice_id → profile_dict`` with profile fields
        (``pitch_level``, ``maturity``, ``childlike``, ``texture_tags``,
        ``delivery_style``, ``energy_level``).
    gender:
        Normalised speaker gender (``male`` / ``female`` / ``child``).
        Caller **must** have pre-filtered *candidates* to this gender —
        the reranker will not reject cross-gender voices.
    age_bucket:
        Normalised age bucket (``young`` / ``middle`` / ``elderly`` / ``""``).
    persona:
        Normalised persona style (``professional`` / ``warm`` / …).
    energy:
        Normalised energy level (``low`` / ``medium`` / ``high`` / ``""``).
    target_chars_per_second:
        Target speech rate in Chinese hanzi per second, typically
        ``source_english_words_per_second × 1.8``.  When *None* the
        speed dimension is disabled (all candidates score 0 on it).
        Candidates without calibration (``chars_per_second`` is *None*)
        also score 0 — no penalty, just no bonus.

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
    effective_w_speed, speed_deviation = compute_w_speed(target_chars_per_second)
    if effective_w_speed > 0:
        logger.info(
            "[reranker] adaptive W_SPEED=%.3f (target=%.2f cps, baseline=%.2f, deviation=%.1f%%)",
            effective_w_speed, target_chars_per_second or 0.0,
            W_SPEED_BASELINE_CPS, speed_deviation * 100,
        )

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

        # Texture (W_TEXTURE)
        voice_textures = set(p.get("texture_tags") or [])
        if preferred_texture and voice_textures & preferred_texture:
            score += W_TEXTURE

        # Speed match (adaptive W_SPEED) — Task 2
        # Target comes from source speaker's English words/sec × 1.8.
        # The weight itself adapts to how extreme the target is — see
        # `compute_w_speed` above.  Graceful degradation: if either target
        # or voice CPS is missing, this dimension contributes 0.
        if effective_w_speed > 0:
            v_cps = v.get("chars_per_second")
            if isinstance(v_cps, (int, float)) and v_cps > 0:
                diff_ratio = min(
                    1.0,
                    abs(float(v_cps) - (target_chars_per_second or 0.0)) / (target_chars_per_second or 1.0),
                )
                score += effective_w_speed * (1.0 - diff_ratio)

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


def _internal_headers() -> dict[str, str]:
    """Build X-Internal-Key header for /api/internal/* calls (T4).

    Reads from env at call time (not import time) so the key can be set
    by orchestration after this module is first imported.
    """
    import os
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    return {"X-Internal-Key": key} if key else {}


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

        resp = requests.get(_GATEWAY_URL, params=params, timeout=3.0, headers=_internal_headers())
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

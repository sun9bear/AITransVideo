"""B2 voice-side profile catalog — offline Gemini-labeled voice characteristics.

These labels describe the *target CosyVoice voice* (voice-side),
distinct from B1 speaker-profile fields which describe the *source speaker*.

Not connected to production routing. Used by offline profiling tools
and future reranking integration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------

VALID_PITCH_LEVELS: Final[frozenset[str]] = frozenset({"low", "mid", "high"})
VALID_THREE_LEVELS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})
VALID_MATURITY: Final[frozenset[str]] = frozenset({"child", "young", "adult", "elder"})
VALID_DELIVERY_STYLES: Final[frozenset[str]] = frozenset({
    "narration", "assistant", "customer_service",
    "companion", "explainer", "storyteller",
})
VALID_TEXTURE_TAGS: Final[frozenset[str]] = frozenset({
    "soft", "crisp", "magnetic", "husky", "airy", "steady",
})


# ---------------------------------------------------------------------------
# Profile dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VoicePrimaryProfile:
    """Primary rerank labels (4 dimensions, used for tie-break sorting)."""

    pitch_level: str  # "low" | "mid" | "high"
    warmth: str       # "low" | "medium" | "high"
    authority: str    # "low" | "medium" | "high"
    intimacy: str     # "low" | "medium" | "high"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.pitch_level not in VALID_PITCH_LEVELS:
            errors.append(f"pitch_level={self.pitch_level!r} not in {sorted(VALID_PITCH_LEVELS)}")
        for name in ("warmth", "authority", "intimacy"):
            val = getattr(self, name)
            if val not in VALID_THREE_LEVELS:
                errors.append(f"{name}={val!r} not in {sorted(VALID_THREE_LEVELS)}")
        return errors


@dataclass(frozen=True, slots=True)
class VoiceSecondaryProfile:
    """Secondary consistency labels (6 dimensions, auxiliary validation)."""

    energy_level: str               # "low" | "medium" | "high"
    brightness: str                 # "low" | "medium" | "high"
    maturity: str                   # "child" | "young" | "adult" | "elder"
    delivery_style: str             # one of VALID_DELIVERY_STYLES
    texture_tags: tuple[str, ...]   # 1-3 from VALID_TEXTURE_TAGS
    childlike: bool

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name in ("energy_level", "brightness"):
            val = getattr(self, name)
            if val not in VALID_THREE_LEVELS:
                errors.append(f"{name}={val!r} not in {sorted(VALID_THREE_LEVELS)}")
        if self.maturity not in VALID_MATURITY:
            errors.append(f"maturity={self.maturity!r} not in {sorted(VALID_MATURITY)}")
        if self.delivery_style not in VALID_DELIVERY_STYLES:
            errors.append(f"delivery_style={self.delivery_style!r} not in {sorted(VALID_DELIVERY_STYLES)}")
        if not self.texture_tags or len(self.texture_tags) > 3:
            errors.append(f"texture_tags must have 1-3 entries, got {len(self.texture_tags)}")
        for tag in self.texture_tags:
            if tag not in VALID_TEXTURE_TAGS:
                errors.append(f"texture_tag={tag!r} not in {sorted(VALID_TEXTURE_TAGS)}")
        return errors


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """Complete voice-side profile for a CosyVoice builtin voice."""

    voice_id: str
    primary: VoicePrimaryProfile
    secondary: VoiceSecondaryProfile
    labeled_at: str  # ISO 8601 timestamp
    labeled_by: str  # "gemini-2.5-flash" | "manual" | etc.

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.voice_id:
            errors.append("voice_id is empty")
        errors.extend(self.primary.validate())
        errors.extend(self.secondary.validate())
        return errors


# ---------------------------------------------------------------------------
# Profile catalog (initially empty — populated by offline profiling tools)
# ---------------------------------------------------------------------------

_VOICE_PROFILES: dict[str, VoiceProfile] = {}
_PROFILES_LOADED: bool = False
PROFILES_JSON_PATH: Path = Path("/opt/aivideotrans/data/b2_voice_profiles_final.json")


def _ensure_profiles_loaded() -> None:
    """Lazy-load profiles from JSON on first access."""
    global _PROFILES_LOADED
    if _PROFILES_LOADED:
        return
    _PROFILES_LOADED = True
    if PROFILES_JSON_PATH.exists():
        try:
            data = json.loads(PROFILES_JSON_PATH.read_text(encoding="utf-8"))
            load_profiles_from_dict(data)
        except Exception:
            pass


def get_voice_profile(voice_id: str) -> VoiceProfile | None:
    """Look up the offline profile for a voice. Returns None if not profiled."""
    _ensure_profiles_loaded()
    return _VOICE_PROFILES.get(voice_id)


def list_profiled_voices() -> list[str]:
    """Return voice_ids that have offline profiles."""
    _ensure_profiles_loaded()
    return sorted(_VOICE_PROFILES.keys())


def load_profiles_from_dict(data: dict[str, dict]) -> int:
    """Load profiles from a serialized dict (e.g., from JSON file).

    Returns the number of profiles loaded.
    """
    count = 0
    for voice_id, entry in data.items():
        primary_data = entry.get("primary", {})
        secondary_data = entry.get("secondary", {})
        texture_tags = secondary_data.get("texture_tags", ())
        if isinstance(texture_tags, list):
            texture_tags = tuple(texture_tags)
        raw_childlike = secondary_data.get("childlike", False)
        if isinstance(raw_childlike, bool):
            childlike_val = raw_childlike
        elif isinstance(raw_childlike, str):
            childlike_val = raw_childlike.lower().strip() in ("true", "1", "yes")
        else:
            childlike_val = bool(raw_childlike)
        profile = VoiceProfile(
            voice_id=voice_id,
            primary=VoicePrimaryProfile(
                pitch_level=str(primary_data.get("pitch_level", "")),
                warmth=str(primary_data.get("warmth", "")),
                authority=str(primary_data.get("authority", "")),
                intimacy=str(primary_data.get("intimacy", "")),
            ),
            secondary=VoiceSecondaryProfile(
                energy_level=str(secondary_data.get("energy_level", "")),
                brightness=str(secondary_data.get("brightness", "")),
                maturity=str(secondary_data.get("maturity", "")),
                delivery_style=str(secondary_data.get("delivery_style", "")),
                texture_tags=texture_tags,
                childlike=childlike_val,
            ),
            labeled_at=str(entry.get("labeled_at", "")),
            labeled_by=str(entry.get("labeled_by", "")),
        )
        _VOICE_PROFILES[voice_id] = profile
        count += 1
    return count

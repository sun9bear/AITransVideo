"""Voice speed catalog loader — query pre-calibrated chars/sec from Gateway.

Part of the translation-duration-alignment plan Phase 1. Fetches
`chars_per_second` and `chars_per_second_by_model` values (populated by
`calibrate_voice_speeds.py`) from Gateway's internal voice-catalog API.

Used by the pipeline to skip probe-TTS calibration when the selected
voice already has a catalog entry. Falls back to probe-TTS calibration
when no entry exists (cloned voices, new voices, API failures, etc.).

Cache semantics: TTL-backed, provider+resource+endpoint keyed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


_GATEWAY_URL = "http://127.0.0.1:8880/api/internal/voice-catalog"
_CACHE_TTL_SECONDS = 120.0

# cache_key -> ({voice_id: speed_entry}, timestamp)
_speed_cache: dict[str, tuple[dict[str, dict], float]] = {}


def load_speed_catalog(
    provider: str,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
) -> dict[str, dict]:
    """Return ``{voice_id: {"chars_per_second": float, "by_model": dict}}``.

    Only includes voices with a non-null ``chars_per_second`` value.
    Returns empty dict on failure (graceful degradation).
    """
    cache_key = f"{provider}:{resource_id or ''}:{endpoint_mode or ''}"
    cached = _speed_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
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

        out: dict[str, dict] = {}
        for v in data.get("voices", []):
            cps = v.get("chars_per_second")
            if cps is None:
                continue
            out[v["voice_id"]] = {
                "chars_per_second": float(cps),
                "chars_per_second_by_model": v.get("chars_per_second_by_model") or {},
                "speed_calibrated_at": v.get("speed_calibrated_at"),
            }

        _speed_cache[cache_key] = (out, time.time())
        return out
    except Exception as exc:
        logger.debug(
            "speed catalog load failed (provider=%s resource=%s mode=%s): %s",
            provider, resource_id, endpoint_mode, exc,
        )
        existing = _speed_cache.get(cache_key)
        return existing[0] if existing else {}


def resolve_chars_per_second(
    voice_id: str,
    *,
    provider: str,
    tts_model: str | None = None,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
) -> float | None:
    """Return the best chars/sec value for a given (voice_id, provider, model).

    Priority:
      1. catalog ``chars_per_second_by_model[tts_model]`` — exact model match
      2. catalog ``chars_per_second`` scalar — fallback average
      3. None — no catalog entry (caller falls back to probe or default)

    The caller is responsible for providing the correct ``resource_id`` /
    ``endpoint_mode`` so the catalog loader filters to the right voice set
    (e.g. VolcEngine 1.0 vs 2.0, CosyVoice mainland vs international).
    """
    catalog = load_speed_catalog(provider, resource_id, endpoint_mode)
    entry = catalog.get(voice_id)
    if entry is None:
        return None

    by_model = entry.get("chars_per_second_by_model") or {}
    if tts_model and tts_model in by_model:
        try:
            return float(by_model[tts_model])
        except (TypeError, ValueError):
            pass

    cps = entry.get("chars_per_second")
    if cps is not None:
        try:
            return float(cps)
        except (TypeError, ValueError):
            pass
    return None


def lookup_per_speaker(
    speaker_voices: dict[str, str],
    *,
    default_provider: str,
    speaker_providers: Optional[dict[str, str]] = None,
    tts_model: str | None = None,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
) -> tuple[float | None, dict[str, float]]:
    """Batch-resolve chars/sec for each speaker.

    ``speaker_voices`` maps ``speaker_id -> voice_id``. Voices that
    evaluate to falsy or equal "auto" are skipped (they mean the
    downstream matcher will pick a voice at TTS time, so we cannot
    look up their speed yet — the caller must fall back to probe).

    ``speaker_providers`` (optional) maps ``speaker_id -> provider``;
    when a speaker has its own provider override (multi-engine jobs),
    that provider is used instead of ``default_provider``.

    Returns ``(global_cps, {speaker_id: cps})``. ``global_cps`` is the
    arithmetic mean of the per-speaker values and can be used as the
    job-level fallback when a segment has no speaker-specific entry.

    If NONE of the speakers have a catalog entry, returns ``(None, {})``.
    The caller should then fall back to probe-TTS calibration.
    """
    per_speaker: dict[str, float] = {}
    sproviders = speaker_providers or {}

    for speaker_id, voice_id in speaker_voices.items():
        if not voice_id or voice_id == "auto":
            continue
        provider = sproviders.get(speaker_id) or default_provider
        cps = resolve_chars_per_second(
            voice_id,
            provider=provider,
            tts_model=tts_model,
            resource_id=resource_id,
            endpoint_mode=endpoint_mode,
        )
        if cps is not None:
            per_speaker[speaker_id] = cps

    if not per_speaker:
        return None, {}

    global_cps = round(sum(per_speaker.values()) / len(per_speaker), 4)
    return global_cps, per_speaker

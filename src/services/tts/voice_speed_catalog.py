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
_USER_VOICES_GATEWAY_URL = "http://127.0.0.1:8880/api/internal/user-voices/by-voice-ids"
_CACHE_TTL_SECONDS = 120.0

# Sanity bounds live in a sibling zero-dependency module so the gateway
# calibrator can import them without pulling in `requests`. Re-exported
# here for backwards-compatibility with existing imports.
from services.tts.voice_speed_bounds import MAX_VALID_CPS, MIN_VALID_CPS  # noqa: F401

# Two caches: one per voice_catalog query, one per user_voices query.
# Keys are query-specific (see _fetch_voices_cps callers).
_speed_cache: dict[str, tuple[dict[str, dict], float]] = {}
_user_voices_cache: dict[str, tuple[dict[str, dict], float]] = {}


def _fetch_voices_cps(
    url: str,
    params: dict,
    cache: dict[str, tuple[dict[str, dict], float]],
    cache_key: str,
) -> dict[str, dict]:
    """Shared fetch-with-ttl-cache helper for both voice_catalog and
    user_voices lookups. Response shape is identical between them:
    ``{voice_id: {chars_per_second, by_model, calibrated_at}}``.

    Caching policy: **only non-empty results are cached**. An empty
    response means "no calibrated voices found yet" — which, on the
    user_voices side, flips the moment the user presses "测试语速".
    Negative-caching that state would keep the pipeline blind to the
    freshly-written cps for up to _CACHE_TTL_SECONDS, forcing the next
    re-run to still fall back to probe. The HTTP cost of re-checking
    a cache miss is trivial (the gateway query is indexed) compared
    to the user-visible bug of a silently-stale calibration.
    """
    cached = cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        resp = requests.get(url, params=params, timeout=3.0)
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
        if out:
            cache[cache_key] = (out, time.time())
        else:
            # Miss — drop any stale entry so a previously-cached non-empty
            # result can't shadow a subsequent miss.
            cache.pop(cache_key, None)
        return out
    except Exception as exc:
        logger.debug("speed cps fetch failed (url=%s key=%s): %s", url, cache_key, exc)
        # Fall back to the last successful result on network/5xx errors
        # (distinct from caching a miss: we only reuse something that
        # actually was populated before).
        existing = cache.get(cache_key)
        return existing[0] if existing else {}


def load_speed_catalog(
    provider: str,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
) -> dict[str, dict]:
    """Return ``{voice_id: {"chars_per_second": float, "by_model": dict}}``
    for the system voice catalog. Only includes voices with a non-null
    ``chars_per_second`` value. Returns empty dict on failure.
    """
    params: dict[str, str] = {"provider": provider}
    if resource_id:
        params["resource_id"] = resource_id
    if endpoint_mode:
        params["endpoint_mode"] = endpoint_mode
    cache_key = f"{provider}:{resource_id or ''}:{endpoint_mode or ''}"
    return _fetch_voices_cps(_GATEWAY_URL, params, _speed_cache, cache_key)


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


def load_user_voice_speeds(
    voice_ids: list[str],
    *,
    user_id: str | None,
) -> dict[str, dict]:
    """Look up calibrated cps for cloned (user_voices) voices.

    Returns the same shape as :func:`load_speed_catalog`. Only voices
    the user has calibrated (via "测试语速") come back; uncalibrated
    clones are absent so the caller treats them the same as a
    voice_catalog miss.

    ``user_id`` is required: the gateway endpoint scopes by it to
    prevent cross-user cps leakage (two users can own different voices
    with the same voice_id since uniqueness is ``(user_id, voice_id)``).
    When ``user_id`` is None (unscoped / legacy callers), we skip the
    lookup entirely so the caller falls back to probe — this is safer
    than silently returning the first matching row.
    """
    if not voice_ids or not user_id:
        return {}
    # Cache key carries user_id so concurrent jobs for different users
    # don't share each other's entries.
    cache_key = f"{user_id}:{','.join(sorted(voice_ids))}"
    return _fetch_voices_cps(
        _USER_VOICES_GATEWAY_URL,
        {"voice_ids": ",".join(voice_ids), "user_id": user_id},
        _user_voices_cache,
        cache_key,
    )


def lookup_per_speaker(
    speaker_voices: dict[str, str],
    *,
    default_provider: str,
    speaker_providers: Optional[dict[str, str]] = None,
    tts_model: str | None = None,
    resource_id: str | None = None,
    endpoint_mode: str | None = None,
    user_id: str | None = None,
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

    Voices not in voice_catalog (typically cloned voices) get a
    second-chance lookup in user_voices. ``user_id`` must be provided
    for that fallback to run; without it the cloned-voice path is
    skipped (not silently unscoped) and the caller falls back to probe.
    """
    per_speaker: dict[str, float] = {}
    sproviders = speaker_providers or {}

    # First pass: voice_catalog (system voices). Track misses so we can
    # batch-check them against user_voices in one round-trip.
    catalog_misses: dict[str, str] = {}  # speaker_id -> voice_id
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
        else:
            catalog_misses[speaker_id] = voice_id

    # Second pass: user_voices (cloned voices). Requires user_id to avoid
    # cross-user leakage; callers without user context skip this path.
    if catalog_misses and user_id:
        user_voice_speeds = load_user_voice_speeds(
            list(catalog_misses.values()),
            user_id=user_id,
        )
        for speaker_id, voice_id in catalog_misses.items():
            entry = user_voice_speeds.get(voice_id)
            if not entry:
                continue
            # Same "by_model first, scalar fallback" priority as catalog.
            by_model = entry.get("chars_per_second_by_model") or {}
            chosen: float | None = None
            if tts_model and tts_model in by_model:
                try:
                    chosen = float(by_model[tts_model])
                except (TypeError, ValueError):
                    chosen = None
            if chosen is None:
                raw = entry.get("chars_per_second")
                if raw is not None:
                    try:
                        chosen = float(raw)
                    except (TypeError, ValueError):
                        chosen = None
            if chosen is not None:
                per_speaker[speaker_id] = chosen

    if not per_speaker:
        return None, {}

    global_cps = round(sum(per_speaker.values()) / len(per_speaker), 4)
    return global_cps, per_speaker

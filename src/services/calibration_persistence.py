"""Probe TTS calibration persistence.

The pipeline calibrates each speaker's chars-per-second by running a
small probe TTS pass (S4-probe). Without persisting that result, every
re-entry into the pipeline (e.g. user approves translation_review and
the worker re-runs from cache) loses the cps and falls back to the
4.5 default. For cloned voices that ARE NOT in voice_catalog, this is
catastrophic:

  - Pre-TTS rewrite uses 4.5 cps to estimate audio duration, gets a
    too-short estimate (cloned voices typically run ~3.3 cps), thinks
    the segment is undershoot, calls LLM to lengthen the text.
  - Real TTS then uses the actual ~3.3 cps voice on the lengthened text,
    producing audio that is now overshoot.
  - S5 rewrite kicks in to shorten, ping-pongs for 2-3 rounds.
  - Phase 2 per-segment speed never fires because chars_per_second is
    None, falling through to "missing_inputs" -> 1.0.

This module persists the probe result to ``audio/probe_calibration.json``
and provides a load helper that validates the saved voice_ids still
match the current selection (so a re-clone or re-pick invalidates the
cache automatically).

The file lives next to the audio cache because:
  1. It's job-scoped (one calibration per audio source).
  2. The audio dir already exists by the time probe runs.
  3. It's a bind-mount, so persistence survives container restarts.

Schema (v1):
    {
      "version": 1,
      "global_chars_per_second": 3.34,
      "chars_per_second_by_speaker": {"speaker_a": 3.34, "speaker_b": 4.12},
      "speaker_voice_ids": {"speaker_a": "vt_speaker_a_xxx", "speaker_b": "..."},
      "calibrated_at": "2026-04-15T11:32:15.252+00:00"
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CALIBRATION_FILENAME = "probe_calibration.json"
CALIBRATION_SCHEMA_VERSION = 1


def persist_probe_calibration(
    audio_dir: Path | str,
    *,
    cps_global: float,
    cps_by_speaker: dict[str, float] | None,
    speaker_voices: dict[str, str] | None = None,
) -> None:
    """Write probe calibration result to ``audio/probe_calibration.json``.

    Best-effort: errors are logged and swallowed (calibration is a
    nice-to-have cache, never a hard dependency).

    Parameters
    ----------
    audio_dir:
        Job's audio dir (typically ``project_dir/audio``).
    cps_global:
        Probe-derived global chars/sec.
    cps_by_speaker:
        Per-speaker chars/sec dict from probe; empty/None is allowed (means
        only the global value will be useful on cache-hit reload).
    speaker_voices:
        Maps speaker_id -> voice_id at probe time. Used at load time to
        invalidate the cache if the user later re-clones / re-picks a
        voice for a speaker.
    """
    audio_path = Path(audio_dir)
    try:
        audio_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[probe_calibration] cannot create audio dir %s: %s", audio_path, exc)
        return

    if cps_global is None or cps_global <= 0:
        # Don't persist garbage; better to fall through to 4.5 default
        # next time than to lock in a bad value.
        return

    payload: dict = {
        "version": CALIBRATION_SCHEMA_VERSION,
        "global_chars_per_second": float(cps_global),
        "chars_per_second_by_speaker": {
            str(k): float(v)
            for k, v in (cps_by_speaker or {}).items()
            if v is not None and v > 0
        },
        "speaker_voice_ids": {
            str(k): str(v)
            for k, v in (speaker_voices or {}).items()
            if v
        },
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = audio_path / CALIBRATION_FILENAME
    try:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[probe_calibration] write failed at %s: %s", out_path, exc)


def load_probe_calibration(
    audio_dir: Path | str,
    *,
    expected_voices: dict[str, str] | None = None,
) -> tuple[float | None, dict[str, float]]:
    """Load probe calibration from disk if valid for the current selection.

    Returns ``(None, {})`` when:
      - the file doesn't exist,
      - parsing fails,
      - any expected speaker's voice_id no longer matches the saved one
        (means user re-cloned / re-picked, so old cps is stale).

    Otherwise returns ``(global_cps, by_speaker_cps_dict)``.
    """
    audio_path = Path(audio_dir)
    cache_path = audio_path / CALIBRATION_FILENAME
    if not cache_path.exists():
        return None, {}

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[probe_calibration] read failed at %s: %s", cache_path, exc)
        return None, {}

    if not isinstance(data, dict):
        return None, {}

    # Version check — future-proof. If we ever change schema, bump
    # CALIBRATION_SCHEMA_VERSION and don't load older versions.
    saved_version = data.get("version")
    if saved_version != CALIBRATION_SCHEMA_VERSION:
        logger.info(
            "[probe_calibration] discarding cache (version mismatch saved=%s expected=%s)",
            saved_version, CALIBRATION_SCHEMA_VERSION,
        )
        return None, {}

    # Voice ID validation — if the user changed a speaker's voice since
    # the cache was written, the cps for that speaker is no longer valid
    # and the whole cache is conservatively invalidated. (Per-speaker
    # partial validity would be nicer but is not needed for current scope.)
    saved_voices = data.get("speaker_voice_ids") or {}
    if expected_voices:
        for sid, expected_vid in expected_voices.items():
            saved_vid = saved_voices.get(sid)
            if saved_vid is None:
                # Speaker exists now but didn't at probe time — caller
                # should re-probe to pick up cps for the new speaker.
                logger.info("[probe_calibration] new speaker %s not in cache, invalidating", sid)
                return None, {}
            if expected_vid and saved_vid != expected_vid:
                logger.info(
                    "[probe_calibration] voice change for %s (saved=%s now=%s), invalidating",
                    sid, saved_vid, expected_vid,
                )
                return None, {}

    raw_global = data.get("global_chars_per_second")
    try:
        global_cps = float(raw_global) if raw_global is not None else None
    except (TypeError, ValueError):
        global_cps = None
    if global_cps is None or global_cps <= 0:
        return None, {}

    raw_by_speaker = data.get("chars_per_second_by_speaker") or {}
    by_speaker: dict[str, float] = {}
    if isinstance(raw_by_speaker, dict):
        for k, v in raw_by_speaker.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                by_speaker[str(k)] = fv

    return global_cps, by_speaker

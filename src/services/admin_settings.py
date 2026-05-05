"""Shared reader for ``admin_settings.json``.

Phase D-1 of 2026-05-04-subtitle-audio-sync-plan.

The aligner had a private reader for ``force_dsp_alignment`` since
Phase 2; with the Whisper subtitle-alignment rollout adding 4 more
fields, the read logic is factored here so:

  - all readers pick up admin changes mid-process (fresh read every
    call; no module-level caching to invalidate)
  - defaults are consistent: read failure / missing field / corrupt
    JSON / non-dict root all degrade to the caller's default. We
    NEVER raise in production — admin settings are advisory.
  - new fields can be added with a one-line addition to a typed
    settings dataclass below.

Settings file location:

  ``{AIVIDEOTRANS_CONFIG_DIR}/admin_settings.json``

Default ``AIVIDEOTRANS_CONFIG_DIR`` (in container) is
``/opt/aivideotrans/config``. The file is bind-mounted from the host
so admin can edit it without rebuilding the container; readers see
the new value on the next call.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DIR = "/opt/aivideotrans/config"


def _admin_settings_path() -> Path:
    """Compute current settings file path. Re-evaluates env each call so
    test ``monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", ...)`` works
    without re-importing the module."""
    return Path(
        os.environ.get("AIVIDEOTRANS_CONFIG_DIR", _DEFAULT_CONFIG_DIR)
    ) / "admin_settings.json"


# ---------------------------------------------------------------------------
# Generic reader
# ---------------------------------------------------------------------------


T = TypeVar("T")


def read_admin_setting(key: str, *, default: T) -> T:
    """Read ``admin_settings.json[key]`` or return ``default``.

    Defensive contract: ANY failure (file missing, IO error, JSON parse
    error, root is not a dict, key absent) returns ``default`` — never
    raises. Admin settings are advisory; production code that relies
    on them must always have a safe fallback.

    No type coercion: if the file says ``"true"`` (string) but caller
    asked for ``default=False`` (bool), we return ``"true"`` verbatim
    rather than guessing. Use ``read_whisper_alignment_settings()``
    for typed/validated whisper-specific reads.
    """
    path = _admin_settings_path()
    try:
        if not path.is_file():
            return default
        with path.open(encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return default
        if key not in cfg:
            return default
        return cfg[key]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("admin_settings: read failed for %s (%s); using default", key, exc)
        return default


# ---------------------------------------------------------------------------
# Typed / validated reader for the Whisper alignment field group
# ---------------------------------------------------------------------------

# Whitelisted enum values. Anything outside the whitelist falls back to
# the field's default — admin typo cannot put the system into an
# undefined state.
_VALID_TRIGGERS = frozenset({"deliverable", "publish", "manual"})
_VALID_MODELS = frozenset({"tiny", "base", "small", "medium", "large-v3"})


@dataclass(slots=True, frozen=True)
class WhisperAlignmentSettings:
    """Validated snapshot of the whisper-alignment admin fields.

    Defaults reflect the safe production state (D-1 ship):
      - ``enabled=False`` — admin policy off; even if env capability
        is on, the cue pipeline still uses proportional cues.
      - ``trigger="deliverable"`` — when admin enables, only run
        whisper at deliverable time (剪映 草稿 / materials_pack with
        subtitles), NOT every publish.
      - ``skip_cache=False`` — cache-aware; same WAV bytes never
        re-transcribed unless admin force-refreshes.
      - ``model="small"`` — ~466MB / ~3× realtime / good Chinese ASR.
    """

    enabled: bool = False
    trigger: str = "deliverable"
    skip_cache: bool = False
    model: str = "small"


def read_whisper_alignment_settings() -> WhisperAlignmentSettings:
    """Snapshot the four whisper_alignment_* fields as a typed
    structure with enum validation.

    Single read of the file — atomic relative to admin edits. Every
    call hits disk (no caching) so admin changes propagate without
    process restart.
    """
    path = _admin_settings_path()
    try:
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return _parse_whisper_settings(cfg)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug(
            "admin_settings: whisper read failed (%s); using all defaults", exc,
        )
    return WhisperAlignmentSettings()


def _strict_bool(value, *, default: bool) -> bool:
    """CodeX P2 (2026-05-05): defensive bool parser.

    Plain ``bool(value)`` is unsafe for hand-edited admin_settings.json
    because Python's truthy semantics treat ``"false"``, ``"0"`` and
    similar non-empty strings as ``True``. For the Whisper alignment
    fields — which gate a high-cost subprocess pipeline — that turns
    a JSON typo into "Whisper silently enabled across all tenants".

    Strict contract: only real Python ``bool`` values pass through; any
    other type (str, int, None, dict, list, ...) falls back to the
    field's default. The Pydantic admin UI always writes real bools, so
    only hand-edits hit this branch.

    Note: bool is a subclass of int in Python, so ``isinstance(True, int)``
    is True. Order the check ``isinstance(value, bool)`` BEFORE any int
    check elsewhere; here the contract is simple — match bool, else default.
    """
    if isinstance(value, bool):
        return value
    return default


def _parse_whisper_settings(cfg: dict) -> WhisperAlignmentSettings:
    """Apply per-field defaulting + enum validation."""
    enabled = _strict_bool(cfg.get("whisper_alignment_enabled"), default=False)
    skip_cache = _strict_bool(cfg.get("whisper_alignment_skip_cache"), default=False)

    trigger = str(cfg.get("whisper_alignment_trigger") or "").strip()
    if trigger not in _VALID_TRIGGERS:
        trigger = "deliverable"

    model = str(cfg.get("whisper_alignment_model") or "").strip()
    if model not in _VALID_MODELS:
        model = "small"

    return WhisperAlignmentSettings(
        enabled=enabled,
        trigger=trigger,
        skip_cache=skip_cache,
        model=model,
    )


__all__ = [
    "read_admin_setting",
    "read_whisper_alignment_settings",
    "WhisperAlignmentSettings",
]

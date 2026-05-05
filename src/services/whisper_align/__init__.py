"""Subprocess wrapper for faster-whisper word-timestamp transcription.

Phase C of 2026-05-04-subtitle-audio-sync-plan.

The ``run_whisper_subprocess`` wrapper spawns ``services.whisper_align.runner``
in a fresh Python child process, which loads the model, transcribes one
WAV, prints word-timestamps as JSON to stdout, then exits. The model
(~1.5GB RAM peak for ``small``/INT8) lives only in the child's memory
and is reclaimed on subprocess exit — the long-lived parent (Job-API,
runner, cue pipeline) never carries the footprint.

C5 cache (2026-05-04): ``run_whisper_subprocess_cached`` is a thin
content-hash-keyed wrapper around the bare runner. Avoids re-spawning
the subprocess when a WAV's bytes haven't changed (common case during
edit-commit when most segments are untouched). Cache is per-WAV file
on disk; cleanup follows the project_dir lifecycle automatically. The
bare ``run_whisper_subprocess`` stays cache-free so existing callers
(C4 smoke tests, C1 unit tests) are unaffected.

Hard constraints (CodeX guardrails for Phase C):
- Parent NEVER imports faster_whisper. The package may not even be
  installed in environments that don't enable the feature flag.
- Caller is responsible for the fallback contract: any failure from
  these wrappers (RuntimeError, JSON decode error, TimeoutExpired) must
  be caught and treated as "this block didn't get aligned, use the
  proportional layout instead." cue_pipeline does this in C3.
- Cache is advisory, never required: corrupt cache → re-run; cache
  write failure → still return the fresh result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SEC = 600        # 10 min — sized for ~30 min audio on small/INT8
_DEFAULT_MODEL = "small"          # ~466MB on disk, ~1.5GB peak RAM, good CN accuracy
_DEFAULT_LANGUAGE = "zh"

# The src/ directory containing the ``services`` package. Computed once at
# import time so the subprocess env injection below knows where to point
# PYTHONPATH. ``__file__`` resolves to .../src/services/whisper_align/__init__.py;
# parents[2] backs up to .../src/.
_SRC_ROOT = str(Path(__file__).resolve().parents[2])


def _build_subprocess_env() -> dict[str, str]:
    """Construct an env dict for ``subprocess.run`` that puts the project's
    src/ directory on PYTHONPATH so the child's ``python -m
    services.whisper_align.runner`` resolves the package.

    CodeX P1 (2026-05-04): without this, the parent's in-process
    ``sys.path`` augmentation (pytest conftest, container entrypoint,
    ``main.py`` startup) does NOT propagate to ``subprocess.run`` —
    the child Python sees a fresh import path that lacks src/, fails
    with ``ModuleNotFoundError: No module named 'services'``, and the
    cue pipeline silently falls back to proportional layout. End
    result: with ``AVT_WHISPER_ALIGN_ENABLED=1`` set, whisper would
    appear to work (no crash) but never actually align anything.

    Preserves any pre-existing parent PYTHONPATH entries by prepending
    src/ rather than overwriting — keeps the child's dependency
    resolution intact.
    """
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    if existing:
        env["PYTHONPATH"] = f"{_SRC_ROOT}{os.pathsep}{existing}"
    else:
        env["PYTHONPATH"] = _SRC_ROOT
    return env


def run_whisper_subprocess(
    wav_path: str,
    *,
    language: str = _DEFAULT_LANGUAGE,
    model: str = _DEFAULT_MODEL,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> list[dict]:
    """Run faster-whisper in a fresh subprocess; return list of word dicts.

    Each returned dict has ``start_ms``, ``end_ms``, ``text``. Times are
    in the WAV's local frame (caller adds segment offset to map to
    project timeline). An empty list means "transcription succeeded but
    no words were detected" — caller's fallback path decides what to do.

    Raises:
        RuntimeError: subprocess exited non-zero. Stderr context is
            included (truncated to 500 chars) for triage.
        ValueError / json.JSONDecodeError: stdout was not valid JSON.
            Should be exceptional (runner always emits JSON or fails) —
            still surfaced rather than silently returning empty.
        subprocess.TimeoutExpired: subprocess didn't complete within
            ``timeout_sec``. Propagated unchanged so callers can treat
            it as a hung/stuck job (often a sign of disk thrashing).
    """
    cmd = [
        sys.executable, "-m", "services.whisper_align.runner",
        "--wav", str(wav_path),
        "--language", language,
        "--model", model,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        encoding="utf-8",
        env=_build_subprocess_env(),
    )
    if proc.returncode != 0:
        # Truncate stderr to keep error message readable in logs.
        stderr_excerpt = (proc.stderr or "")[:500]
        raise RuntimeError(
            f"whisper subprocess failed (rc={proc.returncode}): {stderr_excerpt}"
        )
    payload = json.loads(proc.stdout)
    return list(payload.get("words", []))


# ---------------------------------------------------------------------------
# C5: content-hash cache wrapper
# ---------------------------------------------------------------------------
#
# Cache lives next to the WAV: ``{wav_path}.whisper_{model}_{lang}.json``.
# Per-WAV file (not project-level dir) keeps cleanup automatic — when the
# project_dir is deleted, its caches go with it. The cache key is the
# WAV's content hash plus model + language; same path with different bytes
# (e.g. user re-generated TTS for that segment) invalidates automatically.

_CACHE_FILE_VERSION = 1  # bump if schema changes


def _hash_wav_bytes(wav_path: str) -> str:
    """SHA-256 of the WAV file's contents. Read in a single pass; for
    typical per-segment WAVs (≤ a few MB) this is well under 100ms."""
    h = hashlib.sha256()
    with open(wav_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path_for(wav_path: str, *, model: str, language: str) -> Path:
    """Cache file location: same dir as the WAV, suffix carries model+lang
    so different invocations don't share state. The hash is verified
    INSIDE the cache file (not in the filename) so re-synthesizing the
    WAV at the same path doesn't leave orphaned cache files behind."""
    p = Path(wav_path)
    return p.with_name(f"{p.name}.whisper_{model}_{language}.json")


def run_whisper_subprocess_cached(
    wav_path: str,
    *,
    language: str = _DEFAULT_LANGUAGE,
    model: str = _DEFAULT_MODEL,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> list[dict]:
    """Cache-aware variant of ``run_whisper_subprocess``.

    Cache hit when:
      - cache file exists at ``_cache_path_for(...)``
      - cache JSON parses cleanly
      - cache's ``content_hash`` matches the WAV's current bytes
      - cache's ``version`` matches ``_CACHE_FILE_VERSION``

    Otherwise: run the subprocess fresh, write cache, return result.

    Cache I/O failures are advisory — corrupt cache, missing read perms,
    or a write failure all degrade to "compute fresh, return result".
    The transcribe call itself surfaces normally on subprocess error.
    """
    cache_path = _cache_path_for(wav_path, model=model, language=language)

    # Lookup
    try:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("version") == _CACHE_FILE_VERSION
                and isinstance(cached.get("words"), list)
            ):
                # Validate hash to detect "WAV bytes changed but cache
                # file is stale" — cheaper than running whisper.
                if cached.get("content_hash") == _hash_wav_bytes(wav_path):
                    return list(cached["words"])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Corrupt cache: log at debug, fall through to fresh run.
        logger.debug(
            "whisper-align cache: ignoring unreadable cache at %s (%s)",
            cache_path, exc,
        )

    # Cache miss / corrupt / hash mismatch → run fresh
    words = run_whisper_subprocess(
        wav_path, language=language, model=model, timeout_sec=timeout_sec,
    )

    # Persist (best-effort; never let cache I/O break the caller).
    try:
        payload = {
            "version": _CACHE_FILE_VERSION,
            "content_hash": _hash_wav_bytes(wav_path),
            "model": model,
            "language": language,
            "words": words,
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "whisper-align cache: write failed at %s (%s); result still "
            "returned to caller", cache_path, exc,
        )

    return words


__all__ = [
    "run_whisper_subprocess",
    "run_whisper_subprocess_cached",
]

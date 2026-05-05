"""Subprocess wrapper for faster-whisper word-timestamp transcription.

Phase C of 2026-05-04-subtitle-audio-sync-plan.

The ``run_whisper_subprocess`` wrapper spawns ``services.whisper_align.runner``
in a fresh Python child process, which loads the model, transcribes one
WAV, prints word-timestamps as JSON to stdout, then exits. The model
(~1.5GB RAM peak for ``small``/INT8) lives only in the child's memory
and is reclaimed on subprocess exit — the long-lived parent (Job-API,
runner, cue pipeline) never carries the footprint.

Hard constraints (CodeX guardrails for Phase C):
- Parent NEVER imports faster_whisper. The package may not even be
  installed in environments that don't enable the feature flag.
- Caller is responsible for the fallback contract: any failure from
  this wrapper (RuntimeError, JSON decode error, TimeoutExpired) must
  be caught and treated as "this block didn't get aligned, use the
  proportional layout instead." cue_pipeline does this in C3.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SEC = 600        # 10 min — sized for ~30 min audio on small/INT8
_DEFAULT_MODEL = "small"          # ~466MB on disk, ~1.5GB peak RAM, good CN accuracy
_DEFAULT_LANGUAGE = "zh"


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
    )
    if proc.returncode != 0:
        # Truncate stderr to keep error message readable in logs.
        stderr_excerpt = (proc.stderr or "")[:500]
        raise RuntimeError(
            f"whisper subprocess failed (rc={proc.returncode}): {stderr_excerpt}"
        )
    payload = json.loads(proc.stdout)
    return list(payload.get("words", []))


__all__ = ["run_whisper_subprocess"]

"""Factory for the real ``SegmentTTSCaller`` used by the post-edit flow.

Wiring rules (CLAUDE.md paid-API policy):

1. This factory is the **only** production entry point for paid TTS
   provider calls from the editing layer. It is imported exclusively by
   ``main.run_job_api_command`` — never by alignment / output / commit
   pipeline modules, and never from a ``try/except`` fallback path.
   The AST guard in ``tests/test_phase1_guards.py`` enforces this.

2. The returned caller retries the SAME provider that the segment was
   originally generated with (``segment.tts_provider``). It does NOT
   fall back to a different provider on exhaustion: a user who picked
   MiniMax for a specific voice expects their re-synthesis to keep the
   same voice, and silently switching providers would change audio
   character AND shift billing to another account.

3. Retries are bounded and short-backoff (default 3 retries with
   1s/2s/4s sleep). The pipeline ``_generate_one_with_backoff`` uses a
   much slower schedule (5s → 60s + 5-minute cooldown) which is fine
   for batch publish but would feel hung under an interactive
   "重新合成" spinner.

The factory is called once at JobService construction time
(see ``main.run_job_api_command``). TTSGenerator is created lazily on
the first user-triggered call so that starting the Job API does not
require full TTS credentials in place (for local dev, starting job-api
without ``AUTODUB_TTS_API_KEY`` is common; the first regenerate click
will surface the credential error to the user instead of crashing
startup).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from services.gemini.translator import DubbingSegment
from services.jobs.editing_tts import SegmentTTSCaller
from services.tts.tts_generator import (
    TTSConfig,
    TTSGenerationError,
    TTSGenerator,
    load_tts_config,
)

logger = logging.getLogger(__name__)

__all__ = ["build_real_segment_tts_caller"]

# Caller-level retry policy. Short and bounded — users are watching the
# "重合成中..." spinner; pipeline-style 5-minute cooldowns would feel hung.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0)


def build_real_segment_tts_caller(
    *,
    config: TTSConfig | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    backoff_schedule: tuple[float, ...] = _DEFAULT_BACKOFF_SCHEDULE,
) -> SegmentTTSCaller:
    """Build a ``SegmentTTSCaller`` that re-synthesises a single segment
    using the segment's own ``tts_provider`` + ``voice_id``.

    Parameters
    ----------
    config:
        Optional ``TTSConfig`` override. ``None`` (the usual wiring) means
        ``load_tts_config()`` — i.e. read env vars — on first call.
    max_retries:
        Additional attempts after the first failure. Total attempts =
        max_retries + 1.
    backoff_schedule:
        Seconds to wait before each retry. Indexed by attempt number;
        attempts beyond the list reuse the last value.

    The returned caller matches the ``SegmentTTSCaller`` signature:
    ``(segment_dict: dict, output_path: Path) -> None``.
    """
    # Closure state — lazy TTSGenerator instantiation so that the Job API
    # can start even when TTS credentials aren't configured (first user
    # click then surfaces the credential error in a toast, which is more
    # actionable than a silent boot failure).
    cached: dict[str, TTSGenerator] = {}
    dubbing_fields = {f.name for f in fields(DubbingSegment)}

    def _get_generator() -> TTSGenerator:
        if "instance" not in cached:
            tts_config = config if config is not None else load_tts_config()
            cached["instance"] = TTSGenerator(tts_config)
        return cached["instance"]

    def _caller(segment_dict: dict[str, Any], output_path: Path) -> None:
        # Coerce editing-layer dict into a DubbingSegment. The editing
        # baseline (editor/segments.json) carries every DubbingSegment field
        # verbatim, but lazy-seeded / legacy tasks may hold extra keys from
        # translation/segments.json. Drop anything not on the dataclass.
        kwargs: dict[str, Any] = {k: v for k, v in segment_dict.items() if k in dubbing_fields}

        sid = kwargs.get("segment_id")
        if sid is None:
            raise ValueError("segment dict lacks 'segment_id' — cannot regenerate TTS")
        if not isinstance(sid, int):
            # editor_baseline normalises segment_id to str; DubbingSegment
            # expects int (legacy pipeline contract). Cast back here.
            try:
                kwargs["segment_id"] = int(str(sid))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"segment_id must be int-castable, got {sid!r}"
                ) from exc

        try:
            ds = DubbingSegment(**kwargs)
        except TypeError as exc:
            # Missing a required dataclass field (speaker_id / start_ms / ...)
            raise RuntimeError(
                f"segment dict is missing required field for re-TTS: {exc}"
            ) from exc

        # Pass segment.tts_provider verbatim — _generate_one prioritises
        # segment.tts_provider over the explicit `provider` kwarg anyway,
        # but we send it explicitly so the dispatch is traceable from this
        # call site.
        provider = ds.tts_provider or None
        generator = _get_generator()

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                # Each attempt gets a fresh temp dir so a half-written wav
                # from a failed attempt can't fool the next one via
                # is_valid_output().
                with tempfile.TemporaryDirectory(prefix="segregen_") as tmpdir:
                    result = generator._generate_one(
                        ds, tmpdir, provider=provider,
                    )
                    src_wav = Path(result.audio_path)
                    if not src_wav.is_file():
                        raise RuntimeError(
                            f"TTS provider returned path that does not exist: {src_wav}"
                        )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src_wav, output_path)
                if attempt > 0:
                    logger.info(
                        "segment_regenerate: segment %s succeeded on attempt %d/%d",
                        ds.segment_id, attempt + 1, max_retries + 1,
                    )
                return
            except (TTSGenerationError, RuntimeError, OSError) as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                wait = backoff_schedule[min(attempt, len(backoff_schedule) - 1)]
                logger.warning(
                    "segment_regenerate: segment %s attempt %d/%d failed (%s); "
                    "retrying in %.1fs",
                    ds.segment_id, attempt + 1, max_retries + 1,
                    exc.__class__.__name__, wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"segment TTS re-generation failed after {max_retries + 1} attempts "
            f"on provider={provider or '<default>'}: {last_exc}"
        ) from last_exc

    return _caller

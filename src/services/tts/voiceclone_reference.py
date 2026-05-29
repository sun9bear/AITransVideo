"""Per-speaker reference clip extraction for MiMo voiceclone (free-tier Phase 1,
plan 2026-05-29).

Cuts a short (3-5s) clean reference clip per speaker from the demucs-separated
``speech_for_asr.wav`` and persists it as a job artifact. Mirrors the selection
approach of ``transcript_reviewer._extract_speaker_audio_clips`` but: source is
the CLEAN vocal track (not the mixed ``original.wav``), target is 3-5s, output
is WAV (not opus), and clips persist (not in transient ``.review_tmp``).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REF_DIR_NAME = "voiceclone_ref"  # convention: job_dir/audio/voiceclone_ref/{speaker}.wav


def pick_reference_window(
    segments: list[dict], speaker_id: str, *, min_s: float = 3.0, max_s: float = 5.0
) -> tuple[int, int] | None:
    """Pick the reference window ``(start_ms, end_ms)`` for *speaker_id*.

    Takes the speaker's longest single utterance; caps it to *max_s*; returns
    ``None`` if even the longest is shorter than *min_s* (caller skips speaker).
    """
    spans = [
        (int(s["start_ms"]), int(s["end_ms"]))
        for s in segments
        if s.get("speaker_id") == speaker_id
        and int(s.get("end_ms", 0)) > int(s.get("start_ms", 0))
    ]
    if not spans:
        return None
    start_ms, end_ms = max(spans, key=lambda u: u[1] - u[0])
    if (end_ms - start_ms) / 1000.0 < min_s:
        return None
    if (end_ms - start_ms) / 1000.0 > max_s:
        end_ms = start_ms + int(max_s * 1000)
    return start_ms, end_ms


def extract_speaker_references(
    segments: list[dict],
    speech_audio_path: str | Path,
    out_dir: str | Path,
    *,
    min_s: float = 3.0,
    max_s: float = 5.0,
    sample_rate: int = 24000,
) -> dict[str, Path]:
    """Cut a reference WAV per speaker from *speech_audio_path* into *out_dir*.

    Returns ``{speaker_id: clip_path}``. Speakers with no usable window
    (longest utterance < *min_s*) or whose ffmpeg cut fails are skipped.
    """
    speech_audio_path = Path(speech_audio_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    speakers = sorted({s.get("speaker_id") for s in segments if s.get("speaker_id")})
    refs: dict[str, Path] = {}
    for spk in speakers:
        win = pick_reference_window(segments, spk, min_s=min_s, max_s=max_s)
        if win is None:
            logger.warning("[voiceclone-ref] no usable window for speaker %s", spk)
            continue
        start_ms, end_ms = win
        clip = out_dir / f"{spk}.wav"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_ms / 1000:.3f}",
            "-i", str(speech_audio_path),
            "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-ac", "1", "-ar", str(sample_rate),
            str(clip),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            if clip.exists() and clip.stat().st_size > 0:
                refs[spk] = clip
            else:
                logger.warning("[voiceclone-ref] empty clip for %s", spk)
        except Exception as exc:  # noqa: BLE001 — best-effort; skip speaker on failure
            logger.warning("[voiceclone-ref] ffmpeg failed for %s: %s", spk, exc)
    return refs

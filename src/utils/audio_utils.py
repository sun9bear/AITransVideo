"""Low-memory audio utilities backed by ffprobe / ffmpeg subprocesses.

These helpers avoid loading entire audio files into memory (unlike pydub's
AudioSegment) and should be preferred for duration queries and simple
format conversions.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class AudioProbeError(Exception):
    pass


def measure_duration_ms(path: str | Path) -> int:
    """Return the duration of an audio file in milliseconds using *ffprobe*.

    Raises ``AudioProbeError`` if ffprobe is not available or the file cannot
    be read.
    """
    resolved = str(Path(path).resolve(strict=False))
    try:
        # P1-14 (audit 2026-05-07): 30s timeout to prevent worker threads
        # hanging on hostile or network-mounted audio sources
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "format=duration",
                "-of", "json",
                resolved,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise AudioProbeError("ffprobe not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioProbeError(
            f"ffprobe timed out after {exc.timeout}s for {resolved}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AudioProbeError(
            f"ffprobe failed for {resolved}: {(exc.stderr or '').strip()}"
        ) from exc

    try:
        payload = json.loads(result.stdout)
        duration_seconds = float(payload["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AudioProbeError(
            f"Failed to parse ffprobe output for {resolved}"
        ) from exc

    return int(round(duration_seconds * 1000))


def convert_audio(
    input_path: str | Path,
    output_path: str | Path,
    *,
    channels: int | None = None,
    sample_rate: int | None = None,
    bitrate: str | None = None,
    output_format: str | None = None,
    sample_width: int | None = None,
) -> None:
    """Convert an audio file via *ffmpeg* subprocess (streaming, <10 MB memory).

    Parameters
    ----------
    input_path:
        Source audio file.
    output_path:
        Destination path.  Created / overwritten with ``-y``.
    channels:
        Number of output channels (``-ac``).
    sample_rate:
        Output sample rate in Hz (``-ar``).
    bitrate:
        Output bitrate string, e.g. ``"64k"`` (``-b:a``).
    output_format:
        Explicit output format (``-f``), e.g. ``"mp3"``.  When *None*, ffmpeg
        infers from the file extension.
    sample_width:
        Sample width in bytes (2 = pcm_s16le, 3 = pcm_s24le, 4 = pcm_s32le).
        Only meaningful for raw PCM / WAV output.
    """
    cmd: list[str] = ["ffmpeg", "-i", str(Path(input_path).resolve(strict=False))]

    if sample_width is not None:
        codec_map = {2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}
        codec = codec_map.get(sample_width)
        if codec:
            cmd.extend(["-acodec", codec])

    if channels is not None:
        cmd.extend(["-ac", str(channels)])
    if sample_rate is not None:
        cmd.extend(["-ar", str(sample_rate)])
    if bitrate is not None:
        cmd.extend(["-b:a", bitrate])
    if output_format is not None:
        cmd.extend(["-f", output_format])

    cmd.extend([str(Path(output_path).resolve(strict=False)), "-y"])

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise AudioProbeError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise AudioProbeError(
            f"ffmpeg conversion failed: {(exc.stderr or b'').decode('utf-8', errors='replace').strip()}"
        ) from exc

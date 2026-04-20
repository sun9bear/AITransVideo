from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SPEECH_FRAME_RATE = 16_000


class AudioSeparationError(Exception):
    pass


@dataclass(slots=True)
class AudioSeparationResult:
    source_audio_path: str
    speech_audio_path: str
    ambient_audio_path: str
    reused_cache: bool


class AudioStemSeparator:
    """Create a speech-focused stem for ASR plus an ambient stem for manual reuse.

    2026-04-20 memory-safe rewrite: the earlier pydub implementation loaded
    the entire source into a PCM buffer and cloned it for each per-channel
    operation (`apply_gain`, `overlay`, `invert_phase`). For a 100-minute
    stereo 44.1 kHz input (~1 GB of raw PCM) this ballooned to 6+ GB RSS,
    hitting the 7.6 GB container limit and getting OOM-killed.

    The new implementation streams through ffmpeg's ``pan`` filter in C,
    with O(1) python memory regardless of input length. Semantics are
    preserved to within ~0.2% (-6 dB pydub gain ≈ 0.501 vs ffmpeg's
    0.5 coefficient — inaudible for ASR / ambient purposes):

    Stereo input
        speech  = 0.5 * FL + 0.5 * FR          → mono, 16 kHz, s16le
        ambient = (0.5FL - 0.5FR, 0.5FR - 0.5FL)  → stereo, original rate

    Mono input
        speech  = resample to 16 kHz mono s16le
        ambient = same-duration silent stereo at the source's frame rate
                  (consumed only for optional mixback; rms expected 0)
    """

    speech_filename = "speech_for_asr.wav"
    ambient_filename = "ambient.wav"

    def separate(
        self,
        source_audio_path: str,
        output_dir: str,
        *,
        skip_if_exists: bool = True,
    ) -> AudioSeparationResult:
        source_path = Path(source_audio_path).expanduser().resolve(strict=False)
        if not source_path.exists():
            raise AudioSeparationError(f"Source audio file not found: {source_path}")

        output_root = Path(output_dir).expanduser().resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        speech_path = output_root / self.speech_filename
        ambient_path = output_root / self.ambient_filename

        if skip_if_exists and self._is_cache_valid(source_path, speech_path, ambient_path):
            return AudioSeparationResult(
                source_audio_path=str(source_path),
                speech_audio_path=str(speech_path),
                ambient_audio_path=str(ambient_path),
                reused_cache=True,
            )

        channels = self._probe_channel_count(source_path)

        if channels <= 1:
            self._run_ffmpeg_mono_speech(source_path, speech_path)
            self._run_ffmpeg_silent_stereo(source_path, ambient_path)
        else:
            self._run_ffmpeg_stereo_speech(source_path, speech_path)
            self._run_ffmpeg_stereo_ambient(source_path, ambient_path)

        return AudioSeparationResult(
            source_audio_path=str(source_path),
            speech_audio_path=str(speech_path.resolve(strict=False)),
            ambient_audio_path=str(ambient_path.resolve(strict=False)),
            reused_cache=False,
        )

    # ------------------------------------------------------------------
    # ffmpeg / ffprobe primitives — all O(1) python memory
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_channel_count(source_path: Path) -> int:
        """Use ffprobe to decide mono vs stereo processing path. Defaults
        to mono (safer fallback) if probe fails."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=channels",
                    "-of", "json",
                    str(source_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            payload = json.loads(result.stdout or "{}")
            streams = payload.get("streams") or []
            if streams and isinstance(streams[0], dict):
                ch = streams[0].get("channels")
                if isinstance(ch, int) and ch > 0:
                    return ch
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            FileNotFoundError,
        ):
            pass
        return 1

    @staticmethod
    def _run_ffmpeg(args: list[str], *, timeout_s: int = 1800) -> None:
        """Wrapper around ffmpeg with consistent error mapping. Stream-level
        timeout default 30 min — enough for multi-hour inputs at real-time
        decode speeds, but finite so a hung subprocess doesn't stick forever."""
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.CalledProcessError as exc:
            # Surface ffmpeg's stderr for diagnostics without dumping raw
            # bytes into the traceback (stderr can be verbose).
            stderr_tail = (exc.stderr or "")[-2000:]
            raise AudioSeparationError(
                f"ffmpeg failed: {' '.join(args[:4])}... rc={exc.returncode}\n"
                f"stderr (tail):\n{stderr_tail}"
            ) from exc
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise AudioSeparationError(f"ffmpeg invocation failed: {exc}") from exc

    def _run_ffmpeg_stereo_speech(self, source: Path, dst: Path) -> None:
        """Stereo → mono 16 kHz speech via center-channel estimate."""
        self._run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(source),
            "-af", "pan=mono|c0=0.5*FL+0.5*FR",
            "-ar", str(DEFAULT_SPEECH_FRAME_RATE),
            "-ac", "1",
            "-sample_fmt", "s16",
            "-f", "wav",
            str(dst),
        ])

    def _run_ffmpeg_stereo_ambient(self, source: Path, dst: Path) -> None:
        """Stereo → stereo ambient: subtract center channel from each side.

        L_new = 0.5*L - 0.5*R ; R_new = 0.5*R - 0.5*L

        Matches the pydub invert-phase-and-overlay chain's output to
        within the same ~0.2% coefficient rounding as the speech path."""
        self._run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(source),
            "-af", "pan=stereo|c0=0.5*FL-0.5*FR|c1=0.5*FR-0.5*FL",
            "-ac", "2",
            "-sample_fmt", "s16",
            "-f", "wav",
            str(dst),
        ])

    def _run_ffmpeg_mono_speech(self, source: Path, dst: Path) -> None:
        """Mono → mono 16 kHz resample."""
        self._run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(source),
            "-ar", str(DEFAULT_SPEECH_FRAME_RATE),
            "-ac", "1",
            "-sample_fmt", "s16",
            "-f", "wav",
            str(dst),
        ])

    def _run_ffmpeg_silent_stereo(self, source: Path, dst: Path) -> None:
        """Mono source → same-duration silent stereo wav.

        Implementation trick: pipe the source through volume=0 — ffmpeg
        preserves duration + frame rate, sets every sample to zero, then
        ``-ac 2`` upmixes to stereo. O(1) python memory; no separate
        probe for duration needed."""
        self._run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(source),
            "-af", "volume=0",
            "-ac", "2",
            "-sample_fmt", "s16",
            "-f", "wav",
            str(dst),
        ])

    def _is_cache_valid(self, source_path: Path, speech_path: Path, ambient_path: Path) -> bool:
        if not speech_path.exists() or not ambient_path.exists():
            return False
        source_mtime = source_path.stat().st_mtime
        return (
            speech_path.stat().st_mtime >= source_mtime
            and ambient_path.stat().st_mtime >= source_mtime
        )

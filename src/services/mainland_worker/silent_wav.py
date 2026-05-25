"""Small WAV helpers for the mainland worker mock and live paths."""
from __future__ import annotations

import io
import struct
import wave


SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2
N_CHANNELS = 1


def generate_silent_wav(duration_ms: int) -> bytes:
    """Generate silent mono 16-bit PCM WAV bytes."""
    if duration_ms < 0:
        raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

    n_frames = int(SAMPLE_RATE * duration_ms / 1000)
    pcm_bytes = b"\x00\x00" * n_frames

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(N_CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


def wav_duration_ms(wav_bytes: bytes) -> int:
    """Parse WAV bytes and return duration in milliseconds.

    DashScope streaming WAV responses can contain placeholder chunk sizes in
    the RIFF header. Python's ``wave`` module trusts that header and may report
    an impossible multi-hour duration for a short payload. When that happens,
    compute duration from the actual bytes available in the ``data`` chunk.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as r:
            n_frames = r.getnframes()
            rate = r.getframerate()
            channels = r.getnchannels()
            sample_width = r.getsampwidth()
            if rate <= 0:
                return 0
            duration = int(n_frames * 1000 / rate)
            bytes_per_second = rate * max(channels, 1) * max(sample_width, 1)
            max_possible_ms = int(max(len(wav_bytes) - 44, 0) * 1000 / bytes_per_second) + 1000
            if duration <= max_possible_ms:
                return duration
    except (wave.Error, EOFError):
        pass

    fallback = _wav_duration_ms_from_chunks(wav_bytes)
    if fallback is not None:
        return fallback

    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        n_frames = r.getnframes()
        rate = r.getframerate()
        if rate <= 0:
            return 0
        return int(n_frames * 1000 / rate)


def _wav_duration_ms_from_chunks(wav_bytes: bytes) -> int | None:
    """Compute duration from actual RIFF chunk bytes."""
    if len(wav_bytes) < 44:
        return None
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return None

    pos = 12
    sample_rate = 0
    block_align = 0
    data_bytes = 0

    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos:pos + 4]
        declared_size = struct.unpack_from("<I", wav_bytes, pos + 4)[0]
        data_start = pos + 8
        remaining = max(len(wav_bytes) - data_start, 0)
        actual_size = min(declared_size, remaining)

        if chunk_id == b"fmt " and actual_size >= 16:
            try:
                _fmt, channels, rate, _byte_rate, align, bits = struct.unpack_from(
                    "<HHIIHH",
                    wav_bytes,
                    data_start,
                )
            except struct.error:
                return None
            if channels > 0 and rate > 0:
                sample_rate = rate
                block_align = align or max(1, channels * max(bits // 8, 1))
        elif chunk_id == b"data":
            data_bytes = actual_size
            break

        if declared_size >= remaining:
            break
        pos = data_start + declared_size + (declared_size % 2)

    if sample_rate <= 0 or block_align <= 0:
        return None
    frames = data_bytes // block_align
    return int(frames * 1000 / sample_rate)

"""Silent WAV / text_hash 工具函数测试。"""
from __future__ import annotations

import hashlib
import io
import struct
import wave

import pytest

from services.mainland_worker.silent_wav import (
    N_CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    generate_silent_wav,
    wav_duration_ms,
)
from services.mainland_worker.types import compute_text_hash


def test_silent_wav_has_riff_header() -> None:
    wav = generate_silent_wav(1000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"


def test_silent_wav_duration_matches() -> None:
    for ms in (1000, 3200, 12345):
        wav = generate_silent_wav(ms)
        # 取整到最近毫秒（采样率 16k 下，1ms 精度等价于 16 samples）
        assert abs(wav_duration_ms(wav) - ms) <= 1, f"duration mismatch for {ms}ms"


def test_silent_wav_format_locked() -> None:
    wav = generate_silent_wav(500)
    with wave.open(io.BytesIO(wav), "rb") as r:
        assert r.getnchannels() == N_CHANNELS == 1
        assert r.getsampwidth() == SAMPLE_WIDTH_BYTES == 2
        assert r.getframerate() == SAMPLE_RATE == 16000


def test_silent_wav_is_actually_silent() -> None:
    wav = generate_silent_wav(100)
    with wave.open(io.BytesIO(wav), "rb") as r:
        frames = r.readframes(r.getnframes())
    # 全 0 字节
    assert set(frames) == {0}


def test_silent_wav_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        generate_silent_wav(-1)


def test_silent_wav_zero_duration_returns_header_only() -> None:
    wav = generate_silent_wav(0)
    assert wav[:4] == b"RIFF"
    assert wav_duration_ms(wav) == 0


def test_wav_duration_uses_actual_data_bytes_when_header_size_is_placeholder() -> None:
    wav = bytearray(generate_silent_wav(1500))
    data_pos = bytes(wav).index(b"data")
    struct.pack_into("<I", wav, data_pos + 4, 0xFFFFFFFF)
    assert wav_duration_ms(bytes(wav)) == 1500


# ---------------------------------------------------------------------------
# compute_text_hash
# ---------------------------------------------------------------------------

def test_text_hash_is_sha256_utf8() -> None:
    text = "你好 hello"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert compute_text_hash(text) == expected


def test_text_hash_case_sensitive() -> None:
    assert compute_text_hash("Hello") != compute_text_hash("hello")


def test_text_hash_no_normalize() -> None:
    """plan §POST /cosyvoice/synthesize-batch text_hash 规范：不做 normalize。

    "café" 在 NFC / NFD 两种形态下应该是不同的 hash —— 让 client 和 worker
    不做隐式 normalize 才能保证字节级一致。
    """
    import unicodedata
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    if nfc != nfd:
        assert compute_text_hash(nfc) != compute_text_hash(nfd)


def test_text_hash_empty_string() -> None:
    expected = hashlib.sha256(b"").hexdigest()
    assert compute_text_hash("") == expected

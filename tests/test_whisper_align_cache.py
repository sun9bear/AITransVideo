"""Tests for ``services.whisper_align.run_whisper_subprocess_cached``.

Phase C of 2026-05-04-subtitle-audio-sync-plan, Task C5.

Per-block content-hash cache. Avoids re-running faster-whisper on a
WAV whose bytes haven't changed — common case during Studio
edit-commit when the user changes a few segments and most WAVs are
untouched (γ publish-only path re-runs cue generation but most
aligned audio is identical).

Cache key: ``sha256(wav_bytes) + "_" + model + "_" + language``.
Cache file: written next to the WAV at ``{wav_path}.whisper_{model}_{lang}.json``.
Per-WAV file rather than a project-level dir to keep cleanup automatic
(removing the project removes its caches in lockstep).

The bare ``run_whisper_subprocess`` is unchanged — it stays cache-free
so existing call sites and tests are unaffected. Cue pipeline
explicitly opts in via ``run_whisper_subprocess_cached``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _write_silence_wav(path: Path, n_bytes: int = 32000) -> None:
    """Tiny fake WAV for cache-key hashing tests. Real WAV-ness doesn't
    matter — the wrapper just hashes bytes and shells out to subprocess
    (which we mock)."""
    path.write_bytes(b"RIFF" + b"\x00" * (n_bytes - 4))


# ---------------------------------------------------------------------------
# Cache miss → subprocess called → result written to disk
# ---------------------------------------------------------------------------


def test_first_call_invokes_subprocess_and_writes_cache(tmp_path):
    """First call for a (wav, model, lang) trio: subprocess runs,
    result returned, cache file written next to the WAV."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)
    expected = [{"start_ms": 0, "end_ms": 500, "text": "你好"}]

    call_count = {"n": 0}
    def _fake(*a, **kw):
        call_count["n"] += 1
        return expected
    with patch(
        "services.whisper_align.run_whisper_subprocess",
        side_effect=_fake,
    ) as mock_run:
        out = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )

    assert out == expected
    assert call_count["n"] == 1
    assert mock_run.call_count == 1

    # A cache file was written somewhere next to the WAV.
    cache_files = list(tmp_path.glob("*.whisper_*.json"))
    assert len(cache_files) == 1
    payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert payload["words"] == expected


# ---------------------------------------------------------------------------
# Cache hit → subprocess NOT called → cached value returned
# ---------------------------------------------------------------------------


def test_second_call_with_unchanged_wav_returns_cache_without_subprocess(tmp_path):
    """Same WAV bytes + same model + same lang → cache hit, subprocess
    NOT spawned. This is the main optimization payload: edit-commit
    re-runs cue regeneration but most segment WAVs didn't change, so
    most blocks should hit the cache."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)
    expected = [{"start_ms": 0, "end_ms": 500, "text": "你好"}]

    call_count = {"n": 0}
    def _fake(*a, **kw):
        call_count["n"] += 1
        return expected
    with patch(
        "services.whisper_align.run_whisper_subprocess",
        side_effect=_fake,
    ):
        # First call: cache miss, subprocess runs.
        out1 = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )
        # Second call: cache hit, subprocess MUST NOT run.
        out2 = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )

    assert out1 == expected
    assert out2 == expected
    assert call_count["n"] == 1, (
        f"subprocess invoked {call_count['n']}x; expected 1 (cache miss + hit)"
    )


# ---------------------------------------------------------------------------
# Cache invalidation: different bytes / model / language → re-run
# ---------------------------------------------------------------------------


def test_changed_wav_bytes_invalidate_cache(tmp_path):
    """WAV at the same path but with different content (e.g. user
    re-generated TTS for that segment) must trigger a fresh subprocess
    call — the cache key is content hash, not path."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"RIFF" + b"\x01" * 1000)
    expected_v1 = [{"start_ms": 0, "end_ms": 500, "text": "v1"}]
    expected_v2 = [{"start_ms": 0, "end_ms": 500, "text": "v2"}]

    results = iter([expected_v1, expected_v2])
    with patch(
        "services.whisper_align.run_whisper_subprocess",
        side_effect=lambda *a, **kw: next(results),
    ) as mock_run:
        # First call: hashes content_v1, subprocess runs, returns v1.
        out1 = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )
        # Now overwrite the WAV with different bytes (simulates re-TTS).
        wav.write_bytes(b"RIFF" + b"\x02" * 2000)
        # Second call: different hash, cache miss, subprocess runs again.
        out2 = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )

    assert out1 == expected_v1
    assert out2 == expected_v2
    assert mock_run.call_count == 2


def test_different_model_does_not_share_cache(tmp_path):
    """Cache keys include model name so switching tiny↔small picks the
    right entry without contaminating either."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)

    results = iter([
        [{"start_ms": 0, "end_ms": 500, "text": "small"}],
        [{"start_ms": 0, "end_ms": 500, "text": "tiny"}],
    ])
    with patch(
        "services.whisper_align.run_whisper_subprocess",
        side_effect=lambda *a, **kw: next(results),
    ) as mock_run:
        out_small = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )
        out_tiny = run_whisper_subprocess_cached(
            str(wav), language="zh", model="tiny",
        )

    assert out_small[0]["text"] == "small"
    assert out_tiny[0]["text"] == "tiny"
    assert mock_run.call_count == 2


def test_different_language_does_not_share_cache(tmp_path):
    """Same logic for language: zh and en transcripts are distinct
    artifacts even off the same WAV."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)

    results = iter([
        [{"start_ms": 0, "end_ms": 500, "text": "zh"}],
        [{"start_ms": 0, "end_ms": 500, "text": "en"}],
    ])
    with patch(
        "services.whisper_align.run_whisper_subprocess",
        side_effect=lambda *a, **kw: next(results),
    ) as mock_run:
        out_zh = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )
        out_en = run_whisper_subprocess_cached(
            str(wav), language="en", model="small",
        )

    assert out_zh[0]["text"] == "zh"
    assert out_en[0]["text"] == "en"
    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Robustness: don't let cache I/O issues cascade to publish failure
# ---------------------------------------------------------------------------


def test_corrupt_cache_file_falls_back_to_fresh_run(tmp_path):
    """If a cache file is corrupted (mid-write crash, manual edit, etc.),
    re-run rather than crashing. Cache should be advisory, never required."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)
    expected = [{"start_ms": 0, "end_ms": 500, "text": "fresh"}]

    # Pre-populate a corrupt cache file. We need to compute the path the
    # cached wrapper will look up.
    from services.whisper_align import _cache_path_for
    cache_path = _cache_path_for(str(wav), model="small", language="zh")
    cache_path.write_text("not valid json", encoding="utf-8")

    with patch(
        "services.whisper_align.run_whisper_subprocess",
        return_value=expected,
    ) as mock_run:
        out = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )

    assert out == expected
    assert mock_run.call_count == 1


def test_cache_write_failure_does_not_break_caller(tmp_path, monkeypatch):
    """If we can't write the cache file (read-only FS, permission error,
    disk full), still return the fresh result rather than raising. The
    cache is an optimization — never a hard dependency."""
    from services.whisper_align import run_whisper_subprocess_cached

    wav = tmp_path / "seg.wav"
    _write_silence_wav(wav)
    expected = [{"start_ms": 0, "end_ms": 500, "text": "abc"}]

    # Patch the cache write to raise.
    def _fail_write(self, *a, **kw):
        raise OSError("simulated read-only FS")
    monkeypatch.setattr(Path, "write_text", _fail_write)

    with patch(
        "services.whisper_align.run_whisper_subprocess",
        return_value=expected,
    ):
        # Should not raise.
        out = run_whisper_subprocess_cached(
            str(wav), language="zh", model="small",
        )
    assert out == expected

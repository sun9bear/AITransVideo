"""Opt-in real-faster-whisper smoke tests for the whisper alignment stack.

Phase C of 2026-05-04-subtitle-audio-sync-plan, Task C4.

These tests actually spawn the whisper subprocess, load the small model
(~466 MB on disk, ~1.5 GB peak RAM), and transcribe a real WAV. They
take 30 sec - 2 min on CPU and pull a model download on first run, so
they are SKIPPED by default per CodeX guardrail #3 ("tests don't fetch
real models").

To enable, set ``AVT_RUN_REAL_WHISPER_TESTS=1`` and ensure
``faster-whisper`` is installed:

    pip install faster-whisper
    AVT_RUN_REAL_WHISPER_TESTS=1 pytest tests/test_whisper_align_real_smoke.py -v

CI pipelines and dev machines run with the env unset → these tests
emit a clean SKIPPED entry without attempting any model load.

Smoke level: we verify the runner subprocess infrastructure works
end-to-end (spawns, loads model, exits cleanly, produces valid JSON
shape). We do NOT assert on transcribed content — silence is fine.
A "transcription accuracy" benchmark is out of scope here; that's
better suited for a separate evaluation suite once we have golden
audio fixtures.
"""
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Skip gate — real whisper costs ~1.5GB RAM + ~466MB disk + 30s-2min CPU.
# Opt in deliberately via env var.
#
# CodeX P2 (2026-05-04): module-level ``pytest.importorskip`` is a TRAP —
# it raises Skipped during collection BEFORE pytestmark gets a chance to
# evaluate, leaving pytest with "no tests collected" and exit code 5
# even when the user intentionally has the env unset. Default-skip must
# be a clean exit 0 so CI / dev runs of this file in isolation don't
# look like a failure.
#
# Instead: ``pytestmark = skipif(...)`` keyed on the env var is the ONLY
# module-level gate. Each test individually does ``pytest.importorskip(
# "faster_whisper")`` at the top of its body — which only fires when the
# env IS set (i.e. user explicitly opted in to real whisper but hasn't
# installed the package). Reports as skipped per-test with the actionable
# message, returns exit 0.
# ---------------------------------------------------------------------------

_REAL_WHISPER_ENABLED = os.environ.get("AVT_RUN_REAL_WHISPER_TESTS", "") == "1"
_SKIP_REASON = (
    "Real-whisper smoke tests skipped. Set AVT_RUN_REAL_WHISPER_TESTS=1 to "
    "enable (downloads ~466MB small model on first run, peaks at ~1.5GB RAM, "
    "takes 30s-2min on CPU)."
)
_FASTER_WHISPER_MISSING_REASON = (
    "faster-whisper not installed; install with `pip install faster-whisper`"
)

pytestmark = pytest.mark.skipif(not _REAL_WHISPER_ENABLED, reason=_SKIP_REASON)


# ---------------------------------------------------------------------------
# Fixture: minimal silence WAV (~1 sec) generated at test time
# ---------------------------------------------------------------------------


def _write_silence_wav(path: Path, *, duration_sec: float = 1.0,
                       sample_rate: int = 16000) -> None:
    """Write a mono PCM-16 silence WAV. faster-whisper accepts any
    valid WAV; silence transcribes to empty word list, which is exactly
    what we want for an infrastructure smoke test."""
    n_frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)


# ---------------------------------------------------------------------------
# End-to-end: subprocess spawns, model loads, JSON shape is correct
# ---------------------------------------------------------------------------


def test_real_whisper_subprocess_runs_end_to_end_on_silence(tmp_path: Path):
    """Spawn the actual ``services.whisper_align.runner`` as a subprocess,
    point it at a silence WAV, verify the parent wrapper parses the
    output shape correctly. Validates the entire C1+C4 stack:
    subprocess.run argv → runner main() → faster-whisper.transcribe →
    JSON dump → wrapper JSON.loads → list[dict] return shape.
    """
    pytest.importorskip("faster_whisper", reason=_FASTER_WHISPER_MISSING_REASON)
    from services.whisper_align import run_whisper_subprocess

    wav = tmp_path / "silence.wav"
    _write_silence_wav(wav, duration_sec=1.0)

    # Should not raise. May return empty list (silence has no words);
    # may rarely return a single false-positive word for very short
    # silence — both are acceptable for a smoke test.
    words = run_whisper_subprocess(
        str(wav), language="zh", model="small", timeout_sec=300,
    )
    # Shape contract: list of dicts, each with the three required keys.
    assert isinstance(words, list)
    for w in words:
        assert isinstance(w, dict)
        assert "start_ms" in w and isinstance(w["start_ms"], int)
        assert "end_ms" in w and isinstance(w["end_ms"], int)
        assert "text" in w and isinstance(w["text"], str)


def test_real_whisper_runs_with_tiny_model_too(tmp_path: Path):
    """Verify the model param actually flows through to the subprocess.
    Tiny model (~75MB) is smaller than small — useful for fast CI smoke
    if anyone ever wires this up. Same shape contract."""
    pytest.importorskip("faster_whisper", reason=_FASTER_WHISPER_MISSING_REASON)
    from services.whisper_align import run_whisper_subprocess

    wav = tmp_path / "silence.wav"
    _write_silence_wav(wav, duration_sec=0.5)

    words = run_whisper_subprocess(
        str(wav), language="zh", model="tiny", timeout_sec=120,
    )
    assert isinstance(words, list)


def test_real_whisper_handles_invalid_wav_path():
    """Pointing at a non-existent file should fail via the subprocess
    error path (RuntimeError with stderr context), not silently. This
    is the negative end-to-end smoke."""
    pytest.importorskip("faster_whisper", reason=_FASTER_WHISPER_MISSING_REASON)
    from services.whisper_align import run_whisper_subprocess

    with pytest.raises(RuntimeError) as exc_info:
        run_whisper_subprocess(
            "/path/that/does/not/exist.wav",
            language="zh", model="small", timeout_sec=60,
        )
    # Confirm the error carries useful triage info — runner should mention
    # the file is missing somehow.
    msg = str(exc_info.value).lower()
    assert "rc=" in msg  # non-zero return code reported

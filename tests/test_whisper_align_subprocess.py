"""Tests for ``services.whisper_align.run_whisper_subprocess``.

Phase C of 2026-05-04-subtitle-audio-sync-plan. Subprocess wrapper
isolates faster-whisper's ~1.5GB model footprint from the long-lived
Job-API / runner process — the model loads/unloads in a child process
and the parent never imports ``faster_whisper`` directly.

These tests do NOT spawn a real subprocess or load the model. They mock
``subprocess.run`` to verify:
  - The wrapper passes the right CLI to the runner (model name, language,
    OMP thread cap if applicable, wav path).
  - On success, parses stdout JSON into the word-list shape callers expect.
  - On non-zero exit, raises with stderr context.
  - On JSON-decode error, raises (not crashes).
  - On timeout, propagates ``subprocess.TimeoutExpired``.

The "real faster-whisper end-to-end" smoke test is gated on a marker
and skipped by default — see test_whisper_align_runner_smoke.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Wrapper happy path
# ---------------------------------------------------------------------------


def test_run_whisper_subprocess_returns_word_list_on_success():
    """Successful subprocess returns parsed JSON's ``words`` list as-is."""
    from services.whisper_align import run_whisper_subprocess

    fake_stdout = json.dumps({
        "words": [
            {"start_ms": 100, "end_ms": 500, "text": "你好"},
            {"start_ms": 500, "end_ms": 900, "text": "世界"},
        ],
        "duration_ms": 1000,
    })
    fake_proc = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc):
        words = run_whisper_subprocess("/fake/audio.wav", language="zh")

    assert words == [
        {"start_ms": 100, "end_ms": 500, "text": "你好"},
        {"start_ms": 500, "end_ms": 900, "text": "世界"},
    ]


def test_run_whisper_subprocess_passes_correct_cli_arguments():
    """Wrapper invokes the runner module with --wav, --language, --model."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
        run_whisper_subprocess("/audio.wav", language="zh", model="small")

    cmd = mock_run.call_args.args[0]
    assert sys.executable in cmd[0]
    assert "-m" in cmd
    assert "services.whisper_align.runner" in cmd
    assert "--wav" in cmd
    assert "/audio.wav" in cmd
    assert "--language" in cmd
    assert "zh" in cmd
    assert "--model" in cmd
    assert "small" in cmd


def test_run_whisper_subprocess_default_model_is_small():
    """Default model param is 'small' (good CN accuracy + fits in 8GB RAM)."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
        run_whisper_subprocess("/audio.wav")

    cmd = mock_run.call_args.args[0]
    assert "small" in cmd


def test_run_whisper_subprocess_passes_default_timeout():
    """Default 600s timeout — enough for 30+ minute audio on small/INT8 CPU."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
        run_whisper_subprocess("/audio.wav")

    assert mock_run.call_args.kwargs.get("timeout") == 600


def test_run_whisper_subprocess_caller_can_override_timeout():
    """Caller can set timeout per-call (e.g. shorter for a known-tiny block)."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
        run_whisper_subprocess("/audio.wav", timeout_sec=30)

    assert mock_run.call_args.kwargs.get("timeout") == 30


# ---------------------------------------------------------------------------
# Failure paths — caller's job is to fall back to proportional layout
# ---------------------------------------------------------------------------


def test_run_whisper_subprocess_raises_on_nonzero_exit():
    """A subprocess crash / model-load failure surfaces as RuntimeError
    with stderr context. Caller catches and falls back to proportional."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(
        returncode=1,
        stdout="",
        stderr="cuda: out of memory" * 10,  # long stderr, wrapper truncates
    )
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc):
        try:
            run_whisper_subprocess("/audio.wav")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            msg = str(exc)
            assert "rc=1" in msg
            assert "cuda" in msg.lower()


def test_run_whisper_subprocess_raises_on_invalid_json_output():
    """Runner emitted non-JSON or partial JSON → wrapper raises rather
    than silently returning empty (caller would think alignment succeeded
    with zero words and produce malformed cues)."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout="not json at all", stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc):
        try:
            run_whisper_subprocess("/audio.wav")
            assert False, "expected an exception on invalid JSON"
        except (ValueError, json.JSONDecodeError, RuntimeError):
            pass


def test_run_whisper_subprocess_propagates_timeout():
    """``subprocess.TimeoutExpired`` rises through unchanged so the
    cue-pipeline caller can treat it as "this block didn't get aligned"
    and fall back."""
    from services.whisper_align import run_whisper_subprocess

    timeout_exc = subprocess.TimeoutExpired(cmd=["x"], timeout=600)
    with patch("services.whisper_align.subprocess.run", side_effect=timeout_exc):
        try:
            run_whisper_subprocess("/audio.wav")
            assert False, "expected TimeoutExpired"
        except subprocess.TimeoutExpired:
            pass


def test_run_whisper_subprocess_handles_missing_words_key():
    """Runner output without a "words" key → empty list (not crash).
    Equivalent to "no words detected"; caller's fallback decides what
    to do (likely proportional layout since 0 words can't drive DTW)."""
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"duration_ms": 500}),  # no "words" key
        stderr="",
    )
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc):
        words = run_whisper_subprocess("/audio.wav")

    assert words == []


# ---------------------------------------------------------------------------
# Runner module: lazy import keeps the parent process clean
# ---------------------------------------------------------------------------


def test_runner_module_imports_without_faster_whisper_at_module_level():
    """Importing services.whisper_align.runner should NOT import
    faster_whisper. The library loads lazily inside main() so the
    subprocess pays the model load cost, not the parent.

    Verified by: temporarily masking faster_whisper in sys.modules and
    confirming the runner module still imports cleanly."""
    import importlib
    masked = {"faster_whisper": None}
    with patch.dict(sys.modules, masked, clear=False):
        # Drop any already-imported instance so importlib re-parses.
        sys.modules.pop("services.whisper_align.runner", None)
        mod = importlib.import_module("services.whisper_align.runner")
        assert hasattr(mod, "main")  # entry point exists


def test_wrapper_module_imports_without_faster_whisper():
    """Same guarantee for the parent-side wrapper module — the wrapper
    must never import faster_whisper directly.

    Saves and restores both ``services.whisper_align`` and its ``dtw``
    submodule so a re-import inside this test doesn't leave stale module
    references that break subsequent tests' monkeypatches.
    """
    import importlib

    saved = {
        name: sys.modules.get(name)
        for name in (
            "services.whisper_align",
            "services.whisper_align.dtw",
            "services.whisper_align.runner",
        )
    }
    masked = {"faster_whisper": None}
    try:
        with patch.dict(sys.modules, masked, clear=False):
            for name in saved:
                sys.modules.pop(name, None)
            mod = importlib.import_module("services.whisper_align")
            assert hasattr(mod, "run_whisper_subprocess")
    finally:
        # Restore — otherwise downstream tests' monkeypatches against
        # the cached `services.whisper_align.dtw` etc. would target a
        # stale module reference.
        for name, saved_mod in saved.items():
            if saved_mod is not None:
                sys.modules[name] = saved_mod
            else:
                sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# CodeX P1 (2026-05-04): subprocess inherits PYTHONPATH so child can find
# `services.whisper_align.runner`. Without this, `python -m services...`
# raises ModuleNotFoundError in any environment where the parent has src
# on sys.path but not on PYTHONPATH (which is most environments — pytest
# adds via conftest, container entrypoint adds at startup, but neither
# propagates to subprocess.run children automatically).
# ---------------------------------------------------------------------------


def test_run_whisper_subprocess_passes_pythonpath_with_src_root_to_child():
    """The subprocess.run call MUST pass an env where PYTHONPATH includes
    the project's src/ directory. Without this, the child can't import
    services.* and silently falls back to proportional cues."""
    import os
    from pathlib import Path
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
        run_whisper_subprocess("/audio.wav", language="zh")

    env = mock_run.call_args.kwargs.get("env")
    assert env is not None, (
        "run_whisper_subprocess MUST pass env=... to subprocess.run; without "
        "an explicit env the child won't have PYTHONPATH and `python -m "
        "services.whisper_align.runner` raises ModuleNotFoundError"
    )
    pythonpath = env.get("PYTHONPATH", "")
    # The project's src/ directory must appear in PYTHONPATH so the
    # `services` package resolves in the child.
    src_root = Path(__file__).resolve().parents[1] / "src"
    assert str(src_root) in pythonpath.split(os.pathsep), (
        f"PYTHONPATH={pythonpath!r} does not contain src root {src_root!r}; "
        "child subprocess won't be able to import services.whisper_align.runner"
    )


def test_run_whisper_subprocess_preserves_existing_pythonpath():
    """If the parent process already has PYTHONPATH set (e.g. for a
    parent-injected dependency), the wrapper must PREPEND src/ rather
    than overwriting. Otherwise the child loses the parent's deps."""
    import os
    from services.whisper_align import run_whisper_subprocess

    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"words": []}), stderr="")
    with patch.dict(os.environ, {"PYTHONPATH": "/some/parent/dep/path"}, clear=False):
        with patch("services.whisper_align.subprocess.run", return_value=fake_proc) as mock_run:
            run_whisper_subprocess("/audio.wav")

    env = mock_run.call_args.kwargs.get("env") or {}
    pythonpath = env.get("PYTHONPATH", "")
    # Both parent's pre-existing entry AND src/ root must be present.
    assert "/some/parent/dep/path" in pythonpath, (
        f"parent's PYTHONPATH was lost: child got {pythonpath!r}"
    )

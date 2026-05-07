"""P1-14 (audit 2026-05-07) regression: measure_duration_ms ffprobe
must have a timeout to prevent worker threads hanging on hostile or
network-mounted audio sources.
"""
import ast
import inspect
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def test_measure_duration_ms_passes_timeout_to_subprocess_run():
    """AST guard: the subprocess.run() call inside measure_duration_ms
    MUST include timeout= keyword argument."""
    from utils.audio_utils import measure_duration_ms
    src = inspect.getsource(measure_duration_ms)
    tree = ast.parse(src)
    found_subprocess_run = False
    found_timeout = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match subprocess.run(...) by attribute access
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "run":
            found_subprocess_run = True
            for kw in node.keywords:
                if kw.arg == "timeout":
                    found_timeout = True
                    break
    assert found_subprocess_run, "subprocess.run not found in measure_duration_ms"
    assert found_timeout, (
        "P1-14 regression: measure_duration_ms's subprocess.run does NOT pass "
        "timeout=; ffprobe can hang the worker thread indefinitely on hostile input"
    )


def test_measure_duration_ms_handles_timeout_expired():
    """When subprocess.TimeoutExpired fires, measure_duration_ms must
    raise the project's domain exception (AudioProbeError) — NOT let
    TimeoutExpired escape to the caller. The caller chain (pipeline
    alignment, editing_commit) only catches the domain exception, so a
    leaked TimeoutExpired would crash the pipeline."""
    from utils.audio_utils import measure_duration_ms, AudioProbeError

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "ffprobe", timeout=30)

    with patch("subprocess.run", side_effect=fake_run):
        # We tolerate any of these exception types — the contract is
        # "don't leak TimeoutExpired to the caller"
        with pytest.raises(Exception) as exc_info:
            measure_duration_ms(Path("/nonexistent/fake.wav"))
        # Just assert it's NOT a bare TimeoutExpired escaping
        assert not isinstance(exc_info.value, subprocess.TimeoutExpired) or \
               isinstance(exc_info.value.__cause__, subprocess.TimeoutExpired), (
            f"TimeoutExpired escaped without being wrapped in domain "
            f"exception. Got {type(exc_info.value).__name__}: {exc_info.value}"
        )
        # Stronger assertion: it should be AudioProbeError specifically
        assert isinstance(exc_info.value, AudioProbeError), (
            f"Expected AudioProbeError, got {type(exc_info.value).__name__}: "
            f"{exc_info.value}"
        )

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from utils.audio_fit import FitPolicy, FitResult


REPO = Path(__file__).resolve().parent.parent
AUDIO_FIT = REPO / "src" / "utils" / "audio_fit.py"


def test_audio_fit_has_no_tail_trim_env_behavior_before_phase2() -> None:
    source = AUDIO_FIT.read_text(encoding="utf-8")

    assert "AVT_AUDIO_TAIL_TRIM" not in source
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "utils.env_flags" not in imported_modules


def test_audio_fit_policy_and_result_shape_stay_current_without_tail_trim() -> None:
    assert [field.name for field in fields(FitPolicy)] == [
        "tolerance_ms",
        "atempo_min",
        "atempo_max",
        "silence_trim_enabled",
        "silence_trim_max_ms",
        "silence_threshold_dbfs",
        "silence_chunk_ms",
        "pad_short_with_silence",
    ]
    assert [field.name for field in fields(FitResult)] == [
        "initial_duration_ms",
        "trimmed_duration_ms",
        "stretched_duration_ms",
        "final_duration_ms",
        "speed_ratio_used",
        "silence_padded_ms",
        "truncated_ms",
    ]

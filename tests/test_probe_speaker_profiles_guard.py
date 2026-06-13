"""B' guard: S4 probe segments must be stamped with speaker styles + structure
profiles *after* probe translation and *before* probe TTS calibration.

Root cause (pre-fix): translator.translate_probe() builds DubbingSegment objects
with empty gender / age_group / persona_style / energy_level (see
services/gemini/translator.py), and the probe cache save/load drops those fields
too. So probe TTS calibration ran with an empty voice profile — mis-estimating
chars/second and spamming "[CosyVoice] empty gender" warnings.

The fix wires the same two apply helpers the main segments use onto
``_probe_segments`` right after ``_run_probe_translation()`` returns. This AST
guard pins that wiring (and its ordering) so a future refactor can't silently drop
it — the only symptom would be a subtle calibration-accuracy regression, which is
easy to miss in review.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESS_PY = REPO_ROOT / "src" / "pipeline" / "process.py"


def _tree() -> ast.AST:
    return ast.parse(PROCESS_PY.read_text(encoding="utf-8"))


def _attr_call_linenos(tree: ast.AST, attr: str) -> list[int]:
    """Lines of every ``self.<attr>(...)`` / ``x.<attr>(...)`` call."""
    out: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == attr:
                out.append(node.lineno)
    return out


def _apply_calls_on_probe(tree: ast.AST, attr: str) -> list[ast.Call]:
    """``self.<attr>(_probe_segments, ...)`` calls — first positional arg is the
    ``_probe_segments`` Name."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == attr):
            continue
        if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "_probe_segments":
            out.append(node)
    return out


def test_probe_segments_receive_review_speaker_styles() -> None:
    tree = _tree()
    calls = _apply_calls_on_probe(tree, "_apply_review_speaker_styles_to_segments")
    assert calls, (
        "_probe_segments must be passed to _apply_review_speaker_styles_to_segments "
        "before probe TTS — otherwise probe calibration runs with empty gender/persona."
    )


def test_probe_segments_receive_speaker_structure_profiles() -> None:
    tree = _tree()
    calls = _apply_calls_on_probe(tree, "_apply_speaker_structure_profiles_to_segments")
    assert calls, (
        "_probe_segments must be passed to _apply_speaker_structure_profiles_to_segments "
        "before probe TTS — otherwise probe segments carry no speaker structure role."
    )


def test_probe_profile_apply_is_ordered_after_translation_before_tts() -> None:
    """The apply must sit between _run_probe_translation() and the probe TTS call,
    so the profiles land on freshly-translated (or cache-restored) probe segments
    and are visible to calibration."""
    tree = _tree()

    translation_lines = _attr_call_linenos(tree, "_run_probe_translation")
    probe_tts_lines = _attr_call_linenos(tree, "_run_probe_tts_and_calibrate")
    assert translation_lines, "expected a _run_probe_translation() call site"
    assert probe_tts_lines, "expected a _run_probe_tts_and_calibrate() call site"

    first_translation = min(translation_lines)
    last_probe_tts = max(probe_tts_lines)

    apply_lines = [
        c.lineno
        for attr in (
            "_apply_review_speaker_styles_to_segments",
            "_apply_speaker_structure_profiles_to_segments",
        )
        for c in _apply_calls_on_probe(tree, attr)
    ]
    assert apply_lines, "expected _probe_segments apply call sites"
    in_window = [ln for ln in apply_lines if first_translation < ln < last_probe_tts]
    assert len(in_window) >= 2, (
        "both _probe_segments apply calls must occur after _run_probe_translation "
        f"(line {first_translation}) and before probe TTS (line {last_probe_tts}); "
        f"apply call lines = {sorted(apply_lines)}"
    )

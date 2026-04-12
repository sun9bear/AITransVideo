"""Voice labeling task runner — calls existing scripts via subprocess.

Each function spawns the corresponding script in ``--json`` mode, passes
voice metadata via stdin, and parses structured JSON output from stdout.
Results are returned as a list of dicts ready for Gateway ``write_labels_batch()``.

Accepts full voice metadata (from Gateway DB) so dynamic voices work
without needing to exist in the static Python catalog.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
_LABEL_SCRIPT = _SCRIPTS_DIR / "volcengine_batch_label.py"
_PROFILER_SCRIPT = _SCRIPTS_DIR / "volcengine_voice_profiler.py"

# Limits per call
MAX_TEXT_VOICES = 50
MAX_AUDIO_VOICES = 10
SUBPROCESS_TIMEOUT = 300  # 5 minutes


def run_text_labeling(voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run text labeling for given voices.

    ``voices`` is a list of metadata dicts, each with at least:
    voice_id, display_name, scene.

    Returns list of label dicts: [{"voice_id": ..., "age_group": ..., ...}]
    Raises RuntimeError if no labels were generated.
    """
    if not voices:
        raise ValueError("voices 列表为空")
    if len(voices) > MAX_TEXT_VOICES:
        raise ValueError(f"最多 {MAX_TEXT_VOICES} 条/次，收到 {len(voices)} 条")

    req = json.dumps({"voices": voices})
    result = _run_script(_LABEL_SCRIPT, req)

    labels = result.get("labels", {})
    if not labels:
        raise RuntimeError("标注脚本未生成任何 labels")

    return [
        {"voice_id": vid, **data}
        for vid, data in labels.items()
    ]


def run_audio_profiling(voices: list[dict[str, Any]], round_name: str) -> list[dict[str, Any]]:
    """Run audio profiling for given voices and round.

    ``voices`` is a list of metadata dicts, each with at least:
    voice_id, language, provider_config (with resource_id).

    round_name: "round1", "round2", or "round3"
    Returns list of label dicts with all 10 profile dimensions.
    Raises RuntimeError if no labels were generated.
    """
    if not voices:
        raise ValueError("voices 列表为空")
    if round_name not in ("round1", "round2", "round3"):
        raise ValueError(f"Invalid round: {round_name}")
    if len(voices) > MAX_AUDIO_VOICES:
        raise ValueError(f"音频 profiling 最多 {MAX_AUDIO_VOICES} 条/次，收到 {len(voices)} 条")

    req = json.dumps({"voices": voices, "round_name": round_name})
    result = _run_script(_PROFILER_SCRIPT, req)

    labels = result.get("labels", {})
    if not labels:
        raise RuntimeError("profiling 脚本未生成任何 labels")

    return [
        {"voice_id": vid, **data}
        for vid, data in labels.items()
    ]


def _run_script(script_path: Path, stdin_json: str) -> dict[str, Any]:
    """Run a script in --json mode and parse stdout JSON."""
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "--json"],
            input=stdin_json,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"脚本超时 ({SUBPROCESS_TIMEOUT}s): {script_path.name}")

    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(
            f"脚本执行失败 (exit={proc.returncode}): {proc.stderr[:500]}"
        )

    # Parse the last line of stdout as JSON
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(f"脚本无输出: {script_path.name}")

    try:
        result = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        raise RuntimeError(f"脚本输出非 JSON: {stdout[:300]}")

    if not result.get("ok"):
        raise RuntimeError(result.get("error", "未知错误"))

    return result

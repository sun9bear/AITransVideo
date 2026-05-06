import sys
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"


def test_collector_help_works():
    """collector --help 不抛异常，返回 exit 0"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--projects-root" in result.stdout
    assert "--jobs-root" in result.stdout
    assert "--out-dir" in result.stdout
    assert "--limit" in result.stdout

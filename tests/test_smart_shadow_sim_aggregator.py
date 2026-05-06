import sys
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_sim_aggregator.py"


def test_aggregator_help_works():
    """aggregator --help 不抛异常，--simulator-out-dir 出现在 stdout"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--simulator-out-dir" in result.stdout
    assert "--out-dir" in result.stdout

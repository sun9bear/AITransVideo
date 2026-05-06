import sys
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_sim_simulator.py"


def test_simulator_help_works():
    """simulator --help 不抛异常，--facts 出现在 stdout"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--facts" in result.stdout
    assert "--out-dir" in result.stdout
    assert "--projects-root" in result.stdout

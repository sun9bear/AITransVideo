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


def test_aggregator_empty_dir_writes_empty_aggregate(tmp_path):
    """Empty simulator-out-dir → aggregator writes aggregate_report.json with jobs_simulated=0."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    out = tmp_path / "agg_out"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--simulator-out-dir", str(sim_out),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    agg_path = out / "aggregate_report.json"
    assert agg_path.is_file()
    agg = json.loads(agg_path.read_text(encoding="utf-8"))
    assert agg["schema_version"] == 1
    assert agg["jobs_simulated"] == 0
    assert "warnings" in agg


def test_aggregator_picks_up_per_job_reports(tmp_path):
    """3 mock per-job reports under simulator-out-dir → aggregate jobs_simulated=3."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    for i in range(3):
        job_dir = sim_out / f"job_test_{i:03d}"
        job_dir.mkdir()
        (job_dir / "smart_shadow_report.json").write_text(json.dumps({
            "schema_version": 1,
            "job_id": f"job_test_{i:03d}",
            "smart_eligibility": "pass",
            "stage_decisions_count": 0,
            "warnings": [],
        }), encoding="utf-8")
    out = tmp_path / "agg_out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--simulator-out-dir", str(sim_out),
         "--out-dir", str(out)],
        check=True, capture_output=True, text=True
    )
    agg = json.loads((out / "aggregate_report.json").read_text(encoding="utf-8"))
    assert agg["jobs_simulated"] == 3

import sys
import json
import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_analyzer.py")


def test_analyzer_help():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "--facts" in result.stdout


def test_analyzer_rejects_incomplete_run(tmp_path):
    """summary.is_complete_run=false → analyzer 拒读"""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"is_complete_run": False, "schema_version": 1, "scan_stats": {}}))
    out = tmp_path / "report"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--summary", str(summary),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "is_complete_run" in (result.stderr + result.stdout)


def test_analyzer_rejects_summary_missing_schema_version(tmp_path):
    """summary 无 schema_version 字段 → 显式 reject（不 silent fallthrough）"""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    summary = tmp_path / "summary.json"
    # NO schema_version key
    summary.write_text(json.dumps({"is_complete_run": True, "scan_stats": {}}))
    out = tmp_path / "report"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--summary", str(summary),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "schema_version" in (result.stderr + result.stdout)


def test_analyzer_skeleton_writes_minimal_report(tmp_path):
    """Without summary: minimal report.md + report_summary.json with facts_count."""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    out = tmp_path / "report"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert (out / "report.md").is_file()
    assert (out / "report_summary.json").is_file()
    rs = json.loads((out / "report_summary.json").read_text(encoding="utf-8"))
    assert rs["facts_count"] == 0

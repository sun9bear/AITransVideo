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


def test_analyzer_speaker_count_section(tmp_path):
    facts = tmp_path / "facts.jsonl"
    # 5 jobs: 3 with main_speaker_count=2, 2 with =4
    samples = [
        {"schema_version": 1, "job_id": f"j{i}", "created_at": "2026-04-01",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": cnt}}}
        for i, cnt in enumerate([2, 2, 2, 4, 4])
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Speaker 数分布" in report
    # 3/5 = 60% main_speaker ≤ 3 (at threshold 0.10)
    assert "60" in report or "0.6" in report

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


def test_simulator_empty_facts_writes_summary(tmp_path):
    """Empty facts.jsonl → simulator writes summary.json with is_complete_run=true,
    jobs_simulated=0, exit code 1 (no jobs simulated)."""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    out = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    # 0 jobs simulated → exit 1 per spec §8
    assert result.returncode == 1
    summary_path = out / "summary.json"
    assert summary_path.is_file()
    s = json.loads(summary_path.read_text(encoding="utf-8"))
    assert s["schema_version"] == 1
    assert s["is_complete_run"] is True
    assert s["scan_stats"]["jobs_simulated"] == 0


def test_simulator_one_fact_writes_per_job_sidecar(tmp_path):
    """1 fact → one <out>/<job_id>/smart_shadow_decisions.jsonl + smart_shadow_report.json."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1,
        "job_id": "job_test_001",
        "project_id": "pid_test",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T10:00:00+00:00",
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    job_dir = out / "job_test_001"
    assert job_dir.is_dir()
    assert (job_dir / "smart_shadow_decisions.jsonl").is_file()
    assert (job_dir / "smart_shadow_report.json").is_file()
    # Report must have required v1 keys (skeleton OK to leave most empty/null)
    report = json.loads((job_dir / "smart_shadow_report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["job_id"] == "job_test_001"
    assert "smart_eligibility" in report
    assert "stage_decisions_count" in report
    assert "warnings" in report


def test_simulator_loads_editor_segments_when_available(tmp_path):
    """If projects-root has editor/segments.json for the job, simulator loads it."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1,
        "job_id": "job_b1_test",
        "project_id": "pid_b1",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
    }
    facts.write_text(json.dumps(fact) + "\n")

    projects = tmp_path / "projects" / "pid_b1" / "job_b1_test" / "editor"
    projects.mkdir(parents=True)
    (projects / "segments.json").write_text(json.dumps([
        {"segment_id": "1", "speaker_id": "A", "cn_text": "hello", "start_ms": 0, "end_ms": 5000},
        {"segment_id": "2", "speaker_id": "A", "cn_text": "world", "start_ms": 5000, "end_ms": 10000},
    ]), encoding="utf-8")

    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--projects-root", str(tmp_path / "projects"),
         "--out-dir", str(out)],
        check=True, capture_output=True, text=True
    )
    # Phase B1 just confirms simulator runs without crashing when segments are present.
    # Detailed segment-level assertions come in B8.
    report = json.loads((out / "job_b1_test" / "smart_shadow_report.json").read_text(encoding="utf-8"))
    assert report["job_id"] == "job_b1_test"


def test_simulator_handles_missing_editor_segments(tmp_path):
    """No editor/segments.json → simulator falls back gracefully."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1,
        "job_id": "job_b1_no_segs",
        "project_id": "pid_b1_none",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "pid_b1_none" / "job_b1_no_segs"
    projects.mkdir(parents=True)
    out = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--projects-root", str(tmp_path / "projects"),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

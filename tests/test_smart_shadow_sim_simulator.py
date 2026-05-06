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


def test_b2_stage_decisions_pass_path(tmp_path):
    """Job with main=2 speakers, both have >=8s samples -> pass+clone+clone."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1,
        "job_id": "job_b2_pass",
        "project_id": "pid_b2",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {
            "asr_speaker_count": 2,
            "speaker_duration_shares": [0.6, 0.4],
            "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
        },
        "clone_sample_stats": {
            "eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
            ],
        },
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--out-dir", str(out)],
        check=True, capture_output=True, text=True
    )
    decisions_path = out / "job_b2_pass" / "smart_shadow_decisions.jsonl"
    decisions = [json.loads(line) for line in decisions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    # eligibility_gate
    assert stages["eligibility_gate"]["smart_decision"]["decision"] == "pass"
    # voice_sample_selection: both speakers eligible for clone
    vs = stages["voice_sample_selection"]["smart_decision"]
    assert isinstance(vs, list)
    assert len(vs) == 2
    assert all(s["choice"] == "clone" for s in vs)
    # clone_policy: list of 2 cloned speakers
    cp = stages["clone_policy"]["smart_decision"]
    assert cp.get("auto_clone_main_speakers")
    assert len(cp["auto_clone_main_speakers"]) == 2


def test_b2_eligibility_gate_rejects_main_gt_3(tmp_path):
    """Main >3 -> reject_main_speakers_gt_3."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1,
        "job_id": "job_b2_reject",
        "project_id": "pid",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {
            "asr_speaker_count": 5,
            "speaker_duration_shares": [0.3, 0.25, 0.2, 0.15, 0.10],
            "speaker_count_by_threshold": {"0.05": 5, "0.10": 4, "0.15": 3, "0.20": 2},
        },
        "clone_sample_stats": {"eligible_speakers": 4, "eligible_sample_count_buckets_by_speaker": []},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--out-dir", str(out)],
        check=True, capture_output=True
    )
    decisions = [json.loads(line) for line in (out / "job_b2_reject" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    assert stages["eligibility_gate"]["smart_decision"]["decision"] == "reject_main_speakers_gt_3"


def test_b2_voice_selection_unevaluable_when_clone_stats_missing(tmp_path):
    """Missing clone_sample_stats -> voice_sample_selection unevaluable."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "job_b2_uneval", "project_id": "pid",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2, "speaker_duration_shares": [0.6, 0.4],
                          "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2}},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "job_b2_uneval" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    vs = stages["voice_sample_selection"]["smart_decision"]
    assert vs == "unevaluable" or (isinstance(vs, dict) and vs.get("unevaluable"))

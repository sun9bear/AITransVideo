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


def test_collector_with_empty_fixtures(tmp_path):
    """空 jobs_root 不报错，产 0 行 facts.jsonl + summary.json is_complete_run=true"""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    out_dir = tmp_path / "out"
    jobs_root.mkdir()
    projects_root.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

    facts = out_dir / "facts.jsonl"
    summary = out_dir / "summary.json"
    assert facts.is_file()
    assert facts.read_text() == ""
    assert summary.is_file()
    s = json.loads(summary.read_text())
    assert s["is_complete_run"] is True
    assert s["scan_stats"]["jobs_factsheeted"] == 0


def test_collector_with_one_real_fixture(tmp_path):
    """喂 fixture 'job_post_phase_full' 应产 1 行 inventory + 1 行 fact"""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"

    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

    inventory = (out_dir / "inventory.jsonl").read_text().strip().splitlines()
    assert len(inventory) >= 1
    inv = json.loads(inventory[0])
    assert inv["job_id"] == "job_post_phase_full"
    assert inv["status"] == "succeeded"
    assert inv["service_mode"] in ("studio", "express")


def test_collector_extracts_duration_and_language(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True, text=True
    )
    inventory = [json.loads(line) for line in
                 (out_dir / "inventory.jsonl").read_text().splitlines()]
    inv = next(i for i in inventory if i["job_id"] == "job_post_phase_full")
    assert inv["duration_seconds"] == 254.0
    assert inv["source_language"] == "en_us"
    assert inv["target_language"] == "zh-CN"


def test_collector_writes_minimal_fact_sheet(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True, text=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text().splitlines()]
    assert len(facts) >= 1
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    assert f["schema_version"] == 1
    assert f["service_mode"] == "studio"
    assert f["tts_provider"] == "minimax"
    assert f["tts_model"] == "speech-2.8-hd"
    assert f["edit_generation"] == 1
    assert f["had_post_edit"] is True  # edit_generation > 0
    assert "run_id" in f
    assert "artifact_presence" in f
    assert f["artifact_presence"]["project_state_json"] is True
    assert f["artifact_presence"]["transcript_json"] is True


def test_fact_sheet_line_under_4kb(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    for line in (out_dir / "facts.jsonl").read_text().splitlines():
        assert len(line.encode("utf-8")) <= 4096


def test_speaker_stats_extraction(tmp_path):
    """transcript.json 3 lines: A=5s+4s=9s, B=3s. Total 12s. Shares: A=0.75, B=0.25"""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text().splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    ss = f["speaker_stats"]
    assert ss["asr_speaker_count"] == 2
    assert ss["speaker_duration_shares"] == [0.75, 0.25]
    assert ss["speaker_count_by_threshold"]["0.05"] == 2
    assert ss["speaker_count_by_threshold"]["0.10"] == 2
    assert ss["speaker_count_by_threshold"]["0.20"] == 2

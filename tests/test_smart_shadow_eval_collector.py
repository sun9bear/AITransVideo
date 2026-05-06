import shutil
import sys
import json
import subprocess
from pathlib import Path

import pytest

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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
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
    for line in (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= 4096


def test_speaker_stats_extraction(tmp_path):
    """transcript.json 5 lines: A=6s+10s+7s=23s, B=4s+12s=16s. Total 39s."""
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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    ss = f["speaker_stats"]
    # Expected: speaker_a 23/39 ≈ 0.5897, speaker_b 16/39 ≈ 0.4103
    assert ss["asr_speaker_count"] == 2
    assert ss["speaker_duration_shares"][0] == pytest.approx(0.5897, abs=0.001)
    assert ss["speaker_duration_shares"][1] == pytest.approx(0.4103, abs=0.001)
    assert ss["speaker_count_by_threshold"]["0.05"] == 2
    assert ss["speaker_count_by_threshold"]["0.10"] == 2
    assert ss["speaker_count_by_threshold"]["0.15"] == 2
    assert ss["speaker_count_by_threshold"]["0.20"] == 2


def test_clone_sample_buckets(tmp_path):
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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    css = f["clone_sample_stats"]
    assert css["eligible_speakers"] == 2
    # speaker_a: 6s, 10s, 7s
    assert css["eligible_sample_count_buckets_by_speaker"][0] == \
           {"≥5s": 3, "≥8s": 1, "≥10s": 1, "≥15s": 0}
    # speaker_b: 4s, 12s
    assert css["eligible_sample_count_buckets_by_speaker"][1] == \
           {"≥5s": 1, "≥8s": 1, "≥10s": 1, "≥15s": 0}


def test_actual_clone_stats(tmp_path):
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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    acs = f["actual_clone_stats"]
    assert acs["cloned_speakers"] == 1  # speaker_a uses moss_audio_*
    assert acs["preset_speakers"] == 1  # speaker_b uses preset_chinese_male_1
    assert acs["voice_ids_by_speaker"][0].startswith("moss_audio_")
    assert "preset" in acs["voice_ids_by_speaker"][1].lower()


def test_retry_stats_fallback(tmp_path):
    """No metering/usage_events.jsonl → fallback to editor.segments.rewrite_count sum"""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    # Copy fixture but skip metering/ subdir to test fallback path
    test_jobs = tmp_path / "jobs"
    test_projects = tmp_path / "projects"
    shutil.copytree(fixtures / "jobs", test_jobs)
    shutil.copytree(
        fixtures / "projects", test_projects,
        ignore=shutil.ignore_patterns("metering"),
    )
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(test_jobs),
         "--projects-root", str(test_projects),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    rs = f["retry_stats"]
    # editor segs: rewrite_count 1 + 0 = 1
    assert rs["rewrite_count"] == 1
    assert rs["retts_count"] is None  # no metering = no retts data
    assert rs["_data_source"] == "fallback_editor_segments"


def test_retry_stats_from_metering(tmp_path):
    """When metering exists, prefer metering data."""
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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    rs = f["retry_stats"]
    assert rs["_data_source"] == "metering"
    assert rs["rewrite_count"] == 2  # 2 s5_rewrite events in fixture
    assert rs["retts_count"] == 3    # 3 post_tts_resynth events
    assert rs["retts_total_duration_ms"] == 4500  # 1500 + 1500 + 1500


def test_usage_meter_aggregation(tmp_path):
    """Usage meter aggregates llm tokens, tts chars, clone calls, rewrite chars."""
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
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    um = f["usage_meter"]
    assert um is not None
    assert um["llm_input_tokens"] == 680     # 100 + 80 + 500
    assert um["llm_output_tokens"] == 390    # 50 + 40 + 300
    assert um["tts_chars_total"] == 350      # 200 + 50 + 50 + 50
    assert um["post_tts_resynth_billed_chars"] == 150  # 50 * 3
    assert um["post_edit_resynth_billed_chars"] == 0
    assert um["clone_calls"] == 1
    assert um["rewrite_count"] == 2
    assert um["rewrite_input_text_chars_total"] == 55  # 30 + 25

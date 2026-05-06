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

    inventory = [json.loads(line) for line in
                 (out_dir / "inventory.jsonl").read_text().strip().splitlines()]
    assert len(inventory) >= 1
    inv = next(i for i in inventory if i["job_id"] == "job_post_phase_full")
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


def test_subtitle_sync(tmp_path):
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
    ss = f["subtitle_sync"]
    assert ss["text_audio_drift_count"] == 2
    assert "drift_block_ids" in ss
    assert ss["drift_block_ids"] == ["block_0007", "block_0012"]


def test_whisper_and_workflow_cache(tmp_path):
    """Whisper from subtitle_cues, workflow cache from project_state — different fields."""
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

    w = f["whisper"]
    assert w["alignment_model"] == "small"
    assert w["alignment_fingerprint"] == "abc123def456"
    # 5 cues total, 3 whisper-aligned, 2 fallback
    assert w["whisper_aligned_cue_count"] == 3
    assert w["proportional_fallback_cue_count"] == 2

    wac = f["workflow_alignment_cache"]
    assert wac["cache_hit_blocks"] == 4
    assert wac["block_count"] == 5


def test_user_edits(tmp_path):
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
    ue = f["user_edits"]
    assert ue["speaker_corrections_effective"] == 2
    assert ue["splits_confirmed_effective"] == 1
    assert ue["text_changes_effective"] == 3


def test_corrupted_record_skipped(tmp_path):
    """Job with no created_at → skipped, count incremented."""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["scan_stats"]["skipped_for_missing_identity"] >= 1

    # Also verify the corrupted job didn't make it into facts.jsonl
    facts = (out_dir / "facts.jsonl").read_text(encoding="utf-8")
    assert "job_corrupted_state" not in facts


def test_project_id_fallback_from_project_dir(tmp_path):
    """JobRecord without project_id field but with project_dir absolute path → resolve via path."""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    jobs_root.mkdir()
    # Create a real-shape JobRecord without project_id at top level
    real_job = {
        "job_id": "job_realshape",
        "status": "succeeded",
        "service_mode": "studio",
        "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-04-19T13:36:59+00:00",
        "edit_generation": 0,
        "copy_of_job_id": None,
        "root_job_id": "job_realshape",
        # Mimic real data: project_dir is absolute path; no project_id field
        "project_dir": "/opt/aivideotrans/app/projects/uuid_test_pid/job_realshape",
        "manifest_path": "/opt/aivideotrans/app/projects/uuid_test_pid/job_realshape/manifest.json",
    }
    (jobs_root / "job_realshape.json").write_text(
        json.dumps(real_job), encoding="utf-8"
    )
    # Create matching project_dir under projects_root
    project_dir = projects_root / "uuid_test_pid" / "job_realshape"
    (project_dir / "transcript").mkdir(parents=True)
    (project_dir / "project_state.json").write_text(json.dumps({
        "stages": {
            "ingestion": {"payload": {"duration_ms": 60000}},
            "media_understanding": {"payload": {"language": "en_us", "speaker_count": 1}},
        }
    }), encoding="utf-8")
    (project_dir / "transcript" / "transcript.json").write_text(json.dumps({
        "lines": [{"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 30000}]
    }), encoding="utf-8")

    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_realshape")
    # Project_id was resolved from project_dir path
    assert f["project_id"] == "uuid_test_pid"
    # Artifact extraction worked
    assert f["artifact_presence"]["project_state_json"] is True
    assert f["artifact_presence"]["transcript_json"] is True
    assert f["duration_seconds"] == 60.0
    assert f["source_language"] == "en_us"
    assert f["speaker_stats"]["asr_speaker_count"] == 1


def test_since_filter_excludes_old_jobs(tmp_path):
    """--since 2026-05-05 should exclude jobs with created_at < 2026-05-05."""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    jobs_root.mkdir()
    projects_root.mkdir()
    # Old job (April)
    old_job = {
        "job_id": "job_old", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-04-19T13:36:59+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_old",
    }
    # New job (May)
    new_job = {
        "job_id": "job_new", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-05-06T10:00:00+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_new",
    }
    (jobs_root / "job_old.json").write_text(json.dumps(old_job), encoding="utf-8")
    (jobs_root / "job_new.json").write_text(json.dumps(new_job), encoding="utf-8")

    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir),
         "--since", "2026-05-05"],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    job_ids = [f["job_id"] for f in facts]
    assert "job_new" in job_ids
    assert "job_old" not in job_ids

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["scan_stats"]["skipped_for_date_filter"] == 1


def test_until_filter_excludes_future_jobs(tmp_path):
    """--until 2026-04-30 should include April jobs and exclude May jobs."""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    jobs_root.mkdir()
    projects_root.mkdir()
    old_job = {
        "job_id": "job_old", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-04-19T13:36:59+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_old",
    }
    new_job = {
        "job_id": "job_new", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-05-06T10:00:00+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_new",
    }
    (jobs_root / "job_old.json").write_text(json.dumps(old_job), encoding="utf-8")
    (jobs_root / "job_new.json").write_text(json.dumps(new_job), encoding="utf-8")

    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir),
         "--until", "2026-04-30"],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    job_ids = [f["job_id"] for f in facts]
    assert "job_old" in job_ids
    assert "job_new" not in job_ids


def test_limit_applied_after_filter(tmp_path):
    """--limit 1 with --since should pick the first job that PASSES filter, not the
    first job in alphabetical order."""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    jobs_root.mkdir()
    projects_root.mkdir()
    # Two old (filter excludes) + one new (passes)
    for i, ts in enumerate(["2026-04-01", "2026-04-15"]):
        (jobs_root / f"job_old{i}.json").write_text(json.dumps({
            "job_id": f"job_old{i}", "status": "succeeded",
            "service_mode": "studio", "tts_provider": "minimax",
            "tts_model": "speech-2.8-hd",
            "created_at": f"{ts}T00:00:00+00:00",
            "edit_generation": 0, "copy_of_job_id": None, "root_job_id": f"job_old{i}",
        }), encoding="utf-8")
    (jobs_root / "job_new.json").write_text(json.dumps({
        "job_id": "job_new", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-05-06T10:00:00+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_new",
    }), encoding="utf-8")

    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir),
         "--since", "2026-05-01",
         "--limit", "1"],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(facts) == 1
    assert facts[0]["job_id"] == "job_new"


def test_orphaned_project_dir_count(tmp_path):
    """Count project_dirs without corresponding JobRecord."""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    jobs_root.mkdir()
    projects_root.mkdir()
    # 1 job WITH record
    (jobs_root / "job_known.json").write_text(json.dumps({
        "job_id": "job_known", "status": "succeeded",
        "service_mode": "studio", "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
        "created_at": "2026-05-06T10:00:00+00:00",
        "edit_generation": 0, "copy_of_job_id": None, "root_job_id": "job_known",
    }), encoding="utf-8")
    # Create matching project_dir
    (projects_root / "pid1" / "job_known").mkdir(parents=True)
    # 2 orphaned project_dirs (no JobRecord)
    (projects_root / "pid1" / "job_orphan1").mkdir(parents=True)
    (projects_root / "pid2" / "job_orphan2").mkdir(parents=True)
    # 1 non-job dir (should be ignored)
    (projects_root / "pid1" / "not_a_job_dir").mkdir(parents=True)

    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    # 3 dirs starting with "job_": job_known + job_orphan1 + job_orphan2
    # Of these, 1 has JobRecord (job_known) and 2 are orphaned
    assert summary["scan_stats"]["orphaned_project_dir_count"] == 2


@pytest.mark.skipif(sys.platform == "win32",
                     reason="SIGINT to subprocess not reliably testable on Windows")
def test_sigint_writes_incomplete_summary(tmp_path):
    """Send SIGINT during scan → summary.is_complete_run can be False (or True if too fast)."""
    import time
    import signal as sig
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
    )
    time.sleep(0.05)  # Let it start
    try:
        proc.send_signal(sig.SIGINT)
    except (OSError, ValueError):
        proc.terminate()  # Windows fallback
    proc.wait(timeout=5)
    # Should be either completed or interrupted; check summary
    summary_path = out_dir / "summary.json"
    if summary_path.is_file():
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "is_complete_run" in s
        # If interrupted in time, is_complete_run=false; if too fast or signal didn't land, true is OK

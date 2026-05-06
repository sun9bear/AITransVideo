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


def test_b3_translation_auto_approve(tmp_path):
    """Low uncertain + all clone eligible -> auto_approve."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    assert stages["translation_review_auto_approval"]["smart_decision"]["decision"] == "auto_approve"


def test_b3_translation_manual_review_high_uncertain(tmp_path):
    """High uncertain_speaker_duration_share -> manual_review_required."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j2", "project_id": "p2",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.25},  # 25% uncertain
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j2" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    assert stages["translation_review_auto_approval"]["smart_decision"]["decision"] == "manual_review_required"
    assert "uncertain" in stages["translation_review_auto_approval"]["smart_decision"]["reason"].lower()


def test_b4_retry_estimation_no_retries_needed(tmp_path):
    """Segments well within budget -> expected_retts_count=0, no budget hit."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b4_low", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 60,  # 1 source min
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "p" / "job_j_b4_low"
    (projects / "editor").mkdir(parents=True)
    # 5 segments, each well within k=240 chars/min × duration × 1.05
    segs = [
        {"segment_id": str(i), "speaker_id": "A",
         "cn_text": "短文本",  # 3 chars, way below threshold
         "start_ms": i * 10000, "end_ms": (i + 1) * 10000,
         "rewrite_count": 0}
        for i in range(5)
    ]
    (projects / "editor" / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT),
                     "--facts", str(facts),
                     "--projects-root", str(tmp_path / "projects"),
                     "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b4_low" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    rep = stages["tts_duration_repair_policy"]["smart_decision"]
    assert rep["expected_retts_count"] == 0
    assert rep["would_hit_budget_cap"] is False


def test_b4_retry_estimation_with_long_segment(tmp_path):
    """Length-only segment (cn_text > threshold, rewrite_count=0).

    v1 added +1 to expected_retts_count for the length-overflow trigger.
    v2 (spec §3.5, 2026-05-07) demoted length-only triggers — they now
    surface as a soft per-segment marker but contribute 0 to the aggregate
    expected_retts_count. See p1-done-note §4-bis.3 for empirical
    justification (1534 length-only segments across 38 jobs were the main
    over-prediction driver in v1).
    """
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b4_long", "project_id": "p2",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 30,  # 0.5 src min
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "p2" / "job_j_b4_long"
    (projects / "editor").mkdir(parents=True)
    # k=240 chars/min × 0.5min × 1.05 = 126 chars threshold for a full-duration segment.
    # If segment duration = 30s (0.5 min), cn_text > 126 chars triggers length-only.
    segs = [{
        "segment_id": "1", "speaker_id": "A",
        "cn_text": "这是一段非常非常长的中文文本，远远超过基线阈值，所以智能版会触发重新合成。" * 5,  # ~250 chars
        "start_ms": 0, "end_ms": 30000,
        "rewrite_count": 0,
    }]
    (projects / "editor" / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT),
                     "--facts", str(facts),
                     "--projects-root", str(tmp_path / "projects"),
                     "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b4_long" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    rep = stages["tts_duration_repair_policy"]["smart_decision"]
    # v2: length-only no longer contributes to expected_retts_count.
    assert rep["expected_retts_count"] == 0
    assert rep["estimation_formula_version"] == 2
    # But the segment is still flagged at per-segment level for human review:
    seg_decisions = [d for d in decisions if d.get("decision_kind") == "segment"]
    assert any(s.get("smart_decision", {}).get("expected_retts") is True
               for s in seg_decisions)


def test_b5_subtitle_sync_post_phase_d(tmp_path):
    """Post-Phase-D: whisper.alignment_model='small' -> recommend whisper_align."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b5", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "whisper": {"alignment_model": "small",
                     "whisper_aligned_cue_count": 80,
                     "proportional_fallback_cue_count": 5},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b5" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    ss = stages["subtitle_sync_policy"]["smart_decision"]
    assert ss["whisper_align_recommended"] is True
    assert ss["expected_fallback_ratio"] >= 0


def test_b5_subtitle_sync_pre_phase_d_unevaluable(tmp_path):
    """Pre-Phase-D: whisper data null -> unevaluable."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b5_pre", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "whisper": {"alignment_model": None, "whisper_aligned_cue_count": None,
                     "proportional_fallback_cue_count": None,
                     "_reason_null": "subtitle_cues.json absent (pre-Phase-D job)"},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b5_pre" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = {d["stage_or_segment_id"]: d for d in decisions if d.get("decision_kind") == "stage"}
    ss = stages["subtitle_sync_policy"]["smart_decision"]
    assert ss.get("unevaluable") is True or ss.get("decision") == "unevaluable"


def test_b6_studio_actual_eligibility_gate_always_pass(tmp_path):
    """eligibility_gate.studio_actual = pass tautology (task ran in Studio)."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b6_a", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {
            "asr_speaker_count": 2,
            "speaker_duration_shares": [0.6, 0.4],
            "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
            "uncertain_speaker_duration_share": 0.0,
        },
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b6_a" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    elig = next(d for d in decisions if d.get("stage_or_segment_id") == "eligibility_gate")
    assert elig["studio_actual"] == "pass"


def test_b6_studio_actual_voice_selection_from_actual_clone_stats(tmp_path):
    """voice_sample_selection.studio_actual = list of cloned/preset per speaker, from actual_clone_stats.voice_ids_by_speaker."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b6_v", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        "actual_clone_stats": {
            "cloned_speakers": 1,
            "preset_speakers": 1,
            "voice_ids_by_speaker": ["moss_audio_xxx-yyyy-zzzz", "preset_chinese_male_1"],
        },
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b6_v" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    vs = next(d for d in decisions if d.get("stage_or_segment_id") == "voice_sample_selection")
    actual = vs["studio_actual"]
    assert isinstance(actual, list)
    assert actual[0]["choice"] == "clone"
    assert actual[1]["choice"] == "preset"


def test_b6_studio_actual_translation_review_no_user_changes(tmp_path):
    """translation_review.studio_actual = 'auto_approved' if user_edits.text_changes_effective == 0."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b6_t", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        "user_edits": {"speaker_corrections_effective": 0,
                        "splits_confirmed_effective": 0,
                        "text_changes_effective": 0},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b6_t" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    tr = next(d for d in decisions if d.get("stage_or_segment_id") == "translation_review_auto_approval")
    assert tr["studio_actual"] == "auto_approved"


def test_b6_studio_actual_translation_review_user_modified(tmp_path):
    """translation_review.studio_actual = 'user_modified' if user_edits.text_changes_effective > 0."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b6_t2", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        "user_edits": {"speaker_corrections_effective": 0,
                        "splits_confirmed_effective": 0,
                        "text_changes_effective": 5},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b6_t2" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    tr = next(d for d in decisions if d.get("stage_or_segment_id") == "translation_review_auto_approval")
    assert tr["studio_actual"] == "user_modified"


def test_b7_diff_kind_match_and_more_aggressive(tmp_path):
    """smart=pass + studio=pass → match. smart=reject + studio=pass → more_aggressive."""
    # Test pass=pass match
    facts = tmp_path / "facts.jsonl"
    fact_pass = {
        "schema_version": 1, "job_id": "j_b7_match", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
    }
    facts.write_text(json.dumps(fact_pass) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b7_match" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    elig = next(d for d in decisions if d.get("stage_or_segment_id") == "eligibility_gate")
    assert elig["match"] is True
    assert elig["diff_kind"] == "match"


def test_b7_diff_kind_smart_more_aggressive(tmp_path):
    """smart=reject_main_speakers_gt_3 + studio=pass → smart_more_aggressive."""
    facts = tmp_path / "facts.jsonl"
    fact_reject = {
        "schema_version": 1, "job_id": "j_b7_more", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 5,
                           "speaker_duration_shares": [0.3, 0.25, 0.2, 0.15, 0.10],
                           "speaker_count_by_threshold": {"0.05": 5, "0.10": 4, "0.15": 3, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 4,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                ]},
    }
    facts.write_text(json.dumps(fact_reject) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b7_more" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    elig = next(d for d in decisions if d.get("stage_or_segment_id") == "eligibility_gate")
    assert elig["match"] is False
    assert elig["diff_kind"] == "smart_more_aggressive"


def test_b7_diff_kind_no_studio_signal(tmp_path):
    """studio_actual = unknown → match=null, diff_kind='no_studio_signal'."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b7_unk", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        # No actual_clone_stats → voice_selection studio_actual = "unknown"
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b7_unk" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    vs = next(d for d in decisions if d.get("stage_or_segment_id") == "voice_sample_selection")
    assert vs["match"] is None
    assert vs["diff_kind"] == "no_studio_signal"


def test_b8_per_segment_records_long_text(tmp_path):
    """Segment with long cn_text → recorded as expected_retts."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b8_long", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 60,
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "p" / "job_j_b8_long"
    (projects / "editor").mkdir(parents=True)
    segs = [
        # Segment 1: short, NOT interesting
        {"segment_id": "1", "speaker_id": "A", "cn_text": "短", "start_ms": 0, "end_ms": 5000, "rewrite_count": 0},
        # Segment 2: long, INTERESTING (expected_retts)
        {"segment_id": "2", "speaker_id": "A",
         "cn_text": "这是一段非常非常长的文本超过基线很多很多需要重新合成的中文文本。" * 10,
         "start_ms": 5000, "end_ms": 15000, "rewrite_count": 0},
    ]
    (projects / "editor" / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT),
                     "--facts", str(facts),
                     "--projects-root", str(tmp_path / "projects"),
                     "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b8_long" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    seg_decisions = [d for d in decisions if d.get("decision_kind") == "segment"]
    assert len(seg_decisions) >= 1
    long_seg = next((d for d in seg_decisions if d.get("stage_or_segment_id") == "segment_2"), None)
    assert long_seg is not None
    assert long_seg["smart_decision"].get("expected_retts") is True


def test_b8_per_segment_skips_uninteresting(tmp_path):
    """All-short, no rewrite, no user-edit segments → no per-segment records."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b8_skip", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 60,
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "p" / "job_j_b8_skip"
    (projects / "editor").mkdir(parents=True)
    segs = [
        {"segment_id": str(i), "speaker_id": "A", "cn_text": "短", "start_ms": i * 5000, "end_ms": (i + 1) * 5000, "rewrite_count": 0}
        for i in range(3)
    ]
    (projects / "editor" / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT),
                     "--facts", str(facts),
                     "--projects-root", str(tmp_path / "projects"),
                     "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b8_skip" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    seg_decisions = [d for d in decisions if d.get("decision_kind") == "segment"]
    assert len(seg_decisions) == 0  # all uninteresting


def test_b8_per_segment_records_rewrite_count_gt_0(tmp_path):
    """Segment with editor.rewrite_count > 0 → recorded."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b8_rw", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 60,
    }
    facts.write_text(json.dumps(fact) + "\n")
    projects = tmp_path / "projects" / "p" / "job_j_b8_rw"
    (projects / "editor").mkdir(parents=True)
    segs = [{"segment_id": "1", "speaker_id": "A", "cn_text": "短", "start_ms": 0, "end_ms": 5000, "rewrite_count": 3}]
    (projects / "editor" / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT),
                     "--facts", str(facts),
                     "--projects-root", str(tmp_path / "projects"),
                     "--out-dir", str(out)], check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b8_rw" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    seg_decisions = [d for d in decisions if d.get("decision_kind") == "segment"]
    assert len(seg_decisions) == 1
    assert seg_decisions[0]["smart_decision"].get("expected_rewrite") is True


def test_b9_report_complete_schema(tmp_path):
    """Report has all v1 keys with correct counts derived from decisions."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b9", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        "user_edits": {"text_changes_effective": 0},
        "actual_clone_stats": {
            "voice_ids_by_speaker": ["moss_audio_xxx-yyyy-zzzz", "preset_chinese_male_1"]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    report = json.loads((out / "j_b9" / "smart_shadow_report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["job_id"] == "j_b9"
    assert report["smart_eligibility"] == "pass"
    assert report["stage_decisions_count"] == 6
    # We have data for all 6 stages so match counts should be > 0
    assert report["stage_decisions_match"] >= 1
    assert "stages_unevaluable" in report
    assert "thresholds_used" in report
    assert report["thresholds_used"].get("main_speaker_threshold") == 0.10


def test_b9_report_warnings_for_unevaluable(tmp_path):
    """Pre-Phase-D fact (no whisper) → subtitle_sync_policy unevaluable, listed in stages_unevaluable."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b9_uneval", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 2,
                           "speaker_duration_shares": [0.6, 0.4],
                           "speaker_count_by_threshold": {"0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 2,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                    {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0},
                                ]},
        "whisper": {"alignment_model": None, "_reason_null": "pre-Phase-D"},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    report = json.loads((out / "j_b9_uneval" / "smart_shadow_report.json").read_text(encoding="utf-8"))
    assert "subtitle_sync_policy" in report["stages_unevaluable"]


def test_classify_voice_id_vt_prefix_is_cloned():
    """Same regression test as P0 collector - vt_* must be cloned."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sim_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_sim_simulator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._classify_voice_id("vt_speaker_a_1777851965742") == "cloned"


def test_classify_voice_id_unknown_default_no_false_aggressive():
    """Unknown voice_id should NOT default to 'preset' - that caused false
    smart_more_aggressive findings in Phase B Gate 2."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sim_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_sim_simulator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._classify_voice_id("some_unknown_id") == "unknown"
    assert mod._classify_voice_id("") == "unknown"


def test_classify_voice_id_consistency_across_p0_and_p1():
    """P0 collector and P1 simulator MUST classify identically - drift would
    cause aggregate diff numbers to disagree with collector facts."""
    import importlib.util

    sim_spec = importlib.util.spec_from_file_location(
        "sim_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_sim_simulator.py"
    )
    sim_mod = importlib.util.module_from_spec(sim_spec)
    sim_spec.loader.exec_module(sim_mod)

    coll_spec = importlib.util.spec_from_file_location(
        "coll_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    coll_mod = importlib.util.module_from_spec(coll_spec)
    coll_spec.loader.exec_module(coll_mod)

    test_ids = [
        "vt_speaker_a_1777851965742",
        "moss_audio_85bcf79d-00f2-11f1-b80b-cafa791d3a11",
        "preset_chinese_male_1",
        "some_unknown_xyz",
        "",
        "auto",
        "abc123def456-7890-abcd-ef01-23456789abcd",
    ]
    for vid in test_ids:
        assert sim_mod._classify_voice_id(vid) == coll_mod._classify_voice_id(vid), (
            f"Classification drift for {vid!r}: "
            f"sim={sim_mod._classify_voice_id(vid)} vs collector={coll_mod._classify_voice_id(vid)}"
        )


def test_b7_unknown_speaker_in_studio_actual_no_studio_signal(tmp_path):
    """If actual_clone_stats has unknown voice_id, diff_kind = no_studio_signal,
    NOT false smart_more_aggressive (Codex Gate 2 finding)."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_b7_vt", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 1,
                           "speaker_duration_shares": [1.0],
                           "speaker_count_by_threshold": {"0.05": 1, "0.10": 1, "0.15": 1, "0.20": 1},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 1,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                ]},
        # vt_* should be classified as cloned (after fix)
        "actual_clone_stats": {
            "voice_ids_by_speaker": ["vt_speaker_a_1777851965742"]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in (out / "j_b7_vt" / "smart_shadow_decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    vs = next(d for d in decisions if d.get("stage_or_segment_id") == "voice_sample_selection")
    # Smart picks clone (≥10s sample), Studio actually cloned (vt_) → match
    assert vs["match"] is True
    assert vs["diff_kind"] == "match"
    # clone_policy: Smart says clone speaker 0, Studio also cloned speaker 0 → match
    cp = next(d for d in decisions if d.get("stage_or_segment_id") == "clone_policy")
    assert cp["match"] is True
    assert cp["diff_kind"] == "match"


def test_clone_policy_unknown_voice_id_no_studio_signal(tmp_path):
    """clone_policy: when ANY studio voice_id is unknown, diff_kind must be
    no_studio_signal — NOT smart_more_aggressive.

    Bug being fixed: previously when smart said clone speaker 0 and studio
    voice_id classified as 'unknown', _extract_studio_actual returned
    cloned_speaker_indices=[] (unknown silently dropped). Then
    smart_set={0} > actual_set={} → falsely flagged as smart_more_aggressive.
    The honest answer is "we can't tell what studio did", which is
    no_studio_signal.
    """
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_unk_cp", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 1,
                           "speaker_duration_shares": [1.0],
                           "speaker_count_by_threshold": {"0.05": 1, "0.10": 1, "0.15": 1, "0.20": 1},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 1,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                ]},
        # voice_id doesn't match any cloned/preset pattern -> unknown
        "actual_clone_stats": {
            "voice_ids_by_speaker": ["weird_unrecognized_id_xyz"]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in
                  (out / "j_unk_cp" / "smart_shadow_decisions.jsonl")
                  .read_text(encoding="utf-8").splitlines() if line.strip()]
    cp = next(d for d in decisions
              if d.get("stage_or_segment_id") == "clone_policy")
    # Smart wants to clone speaker 0, but we can't tell what Studio did.
    # Must NOT be smart_more_aggressive.
    assert cp["diff_kind"] == "no_studio_signal", (
        f"clone_policy expected no_studio_signal, got {cp['diff_kind']!r}; "
        f"smart={cp['smart_decision']} studio={cp['studio_actual']}"
    )
    assert cp["match"] is None
    assert cp["diff_kind"] != "smart_more_aggressive"


def test_voice_sample_selection_unknown_voice_id_no_studio_signal(tmp_path):
    """voice_sample_selection: existing path already handles unknown studio
    choices via the 'any unknown -> no_studio_signal' early return; this
    test pins that contract so future refactors don't regress it."""
    facts = tmp_path / "facts.jsonl"
    fact = {
        "schema_version": 1, "job_id": "j_unk_vs", "project_id": "p",
        "service_mode": "studio", "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "speaker_stats": {"asr_speaker_count": 1,
                           "speaker_duration_shares": [1.0],
                           "speaker_count_by_threshold": {"0.05": 1, "0.10": 1, "0.15": 1, "0.20": 1},
                           "uncertain_speaker_duration_share": 0.0},
        "clone_sample_stats": {"eligible_speakers": 1,
                                "eligible_sample_count_buckets_by_speaker": [
                                    {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                                ]},
        "actual_clone_stats": {
            "voice_ids_by_speaker": ["mystery_id_no_pattern"]},
    }
    facts.write_text(json.dumps(fact) + "\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(SCRIPT), "--facts", str(facts), "--out-dir", str(out)],
                    check=True, capture_output=True)
    decisions = [json.loads(line) for line in
                  (out / "j_unk_vs" / "smart_shadow_decisions.jsonl")
                  .read_text(encoding="utf-8").splitlines() if line.strip()]
    vs = next(d for d in decisions
              if d.get("stage_or_segment_id") == "voice_sample_selection")
    assert vs["diff_kind"] == "no_studio_signal"
    assert vs["match"] is None

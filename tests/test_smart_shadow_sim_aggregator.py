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


# ============================================================================
# Phase C helpers — synthesize a mock per-job sidecar set in tmp_path.
# ============================================================================

STAGE_IDS = [
    "eligibility_gate",
    "voice_sample_selection",
    "clone_policy",
    "translation_review_auto_approval",
    "tts_duration_repair_policy",
    "subtitle_sync_policy",
]


def _make_stage_decision(stage_id, smart_decision, studio_actual, match, diff_kind, evidence=None):
    return {
        "schema_version": 1,
        "decision_kind": "stage",
        "stage_or_segment_id": stage_id,
        "smart_decision": smart_decision,
        "studio_actual": studio_actual,
        "match": match,
        "diff_kind": diff_kind,
        "evidence": evidence or {},
    }


def _write_job_sidecar(sim_out_dir: Path, job_id: str, smart_eligibility: str,
                       stages: dict, segments: list = None):
    """Write per-job report.json + decisions.jsonl under sim_out_dir/<job_id>/.

    `stages`: dict {stage_id: (smart_decision, studio_actual, match, diff_kind, evidence)}
    `segments`: optional list of (smart, studio, match, diff_kind, evidence) tuples
    """
    job_dir = sim_out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    decisions = []
    for stage_id in STAGE_IDS:
        if stage_id not in stages:
            continue
        s, a, m, dk, ev = stages[stage_id]
        decisions.append(_make_stage_decision(stage_id, s, a, m, dk, ev))
    if segments:
        for i, (s, a, m, dk, ev) in enumerate(segments, start=1):
            decisions.append({
                "schema_version": 1,
                "decision_kind": "segment",
                "stage_or_segment_id": f"segment_{i}",
                "smart_decision": s, "studio_actual": a,
                "match": m, "diff_kind": dk, "evidence": ev or {},
            })
    (job_dir / "smart_shadow_decisions.jsonl").write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in decisions) + "\n",
        encoding="utf-8")

    # Build summary report consistent with simulator's per-job report schema
    stage_decisions = [d for d in decisions if d["decision_kind"] == "stage"]
    seg_decisions = [d for d in decisions if d["decision_kind"] == "segment"]
    stages_unevaluable = []
    if stage_decisions:
        for d in stage_decisions:
            sd = d["smart_decision"]
            sid = d["stage_or_segment_id"]
            unev = (isinstance(sd, dict) and sd.get("decision") == "unevaluable") \
                   or (isinstance(sd, dict) and sd.get("unevaluable") is True)
            if unev:
                stages_unevaluable.append(sid)
    report = {
        "schema_version": 1,
        "job_id": job_id,
        "smart_eligibility": smart_eligibility,
        "stage_decisions_count": len(stage_decisions),
        "stage_decisions_match": sum(1 for d in stage_decisions if d["match"] is True),
        "segment_decisions_count": len(seg_decisions),
        "segment_decisions_match": sum(1 for d in seg_decisions if d["match"] is True),
        "smart_more_aggressive_count": sum(1 for d in stage_decisions if d["diff_kind"] == "smart_more_aggressive"),
        "smart_less_aggressive_count": sum(1 for d in stage_decisions if d["diff_kind"] == "smart_less_aggressive"),
        "orthogonal_count": sum(1 for d in stage_decisions if d["diff_kind"] == "orthogonal"),
        "stages_unevaluable": stages_unevaluable,
        "thresholds_used": {"main_speaker_threshold": 0.10,
                            "clone_min_seconds_soft": 8,
                            "clone_min_seconds_preferred": 10},
        "warnings": [],
    }
    (job_dir / "smart_shadow_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_aggregator(tmp_path: Path):
    sim_out = tmp_path / "sim_out"
    out = tmp_path / "agg_out"
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--simulator-out-dir", str(sim_out),
         "--out-dir", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    agg = json.loads((out / "aggregate_report.json").read_text(encoding="utf-8"))
    return agg, out


# ============================================================================
# C2: stage_decision_diff_breakdown (5-bucket) + smart_eligibility_breakdown +
#     stages_unevaluable_rate
# ============================================================================


def test_c2_stage_decision_diff_breakdown_5_bucket(tmp_path):
    """Each stage gets a 5-bucket count: match / smart_more_aggressive /
    smart_less_aggressive / orthogonal / no_studio_signal + match_rate string.
    """
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 3 jobs, all pass eligibility, varied voice_sample_selection diffs
    for i, vs_diff in enumerate(["match", "smart_more_aggressive", "no_studio_signal"]):
        _write_job_sidecar(sim_out, f"job_{i}", "pass", {
            "eligibility_gate": (
                {"decision": "pass", "main_count": 1}, "pass", True, "match", {}),
            "voice_sample_selection": (
                [{"speaker_index": 0, "choice": "clone", "reason": "≥10s_sample_available"}],
                [{"speaker_index": 0, "choice": "clone", "voice_id": "vt_x"}] if vs_diff == "match"
                else "unknown" if vs_diff == "no_studio_signal"
                else [{"speaker_index": 0, "choice": "preset", "voice_id": "preset_x"}],
                vs_diff == "match", vs_diff, {}),
        })
    agg, _ = _run_aggregator(tmp_path)
    # eligibility_gate: 3 matches
    eg = agg["stage_decision_diff_breakdown"]["eligibility_gate"]
    assert eg["match"] == 3
    assert eg["smart_more_aggressive"] == 0
    assert eg["smart_less_aggressive"] == 0
    assert eg["orthogonal"] == 0
    assert eg["no_studio_signal"] == 0
    assert eg["match_rate"] == "3/3 (100%)"
    # voice_sample_selection: 1/3 match, 1 more_aggressive, 1 no_studio_signal
    vs = agg["stage_decision_diff_breakdown"]["voice_sample_selection"]
    assert vs["match"] == 1
    assert vs["smart_more_aggressive"] == 1
    assert vs["no_studio_signal"] == 1
    assert vs["match_rate"] == "1/3 (33%)"


def test_c2_smart_eligibility_breakdown(tmp_path):
    """eligibility_breakdown buckets jobs by smart_eligibility field."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    _write_job_sidecar(sim_out, "j_pass1", "pass", {})
    _write_job_sidecar(sim_out, "j_pass2", "pass", {})
    _write_job_sidecar(sim_out, "j_reject", "reject_main_speakers_gt_3", {})
    _write_job_sidecar(sim_out, "j_unev", "unevaluable", {})
    agg, _ = _run_aggregator(tmp_path)
    eb = agg["smart_eligibility_breakdown"]
    assert eb["pass"] == 2
    assert eb["reject_main_speakers_gt_3"] == 1
    assert eb["unevaluable"] == 1


def test_c2_stages_unevaluable_rate(tmp_path):
    """Per-stage unevaluable rate based on stages_unevaluable field in each report.
    User specifically asked for: translation_review_auto_approval,
    subtitle_sync_policy, tts_duration_repair_policy."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 5 jobs total. translation_review unevaluable in 3; subtitle in 4; tts_repair in 1.
    _write_job_sidecar(sim_out, "j1", "pass", {
        "translation_review_auto_approval": (
            {"decision": "unevaluable", "reason": "missing_signals"}, "unknown", None, "no_studio_signal", {}),
        "subtitle_sync_policy": (
            {"unevaluable": True, "reason": "pre_phase_d_or_no_whisper"}, "unknown", None, "no_studio_signal", {}),
        "tts_duration_repair_policy": (
            {"expected_retts_count": 5}, {"actual_retts_count": 5}, True, "match", {}),
    })
    _write_job_sidecar(sim_out, "j2", "pass", {
        "translation_review_auto_approval": (
            {"decision": "unevaluable"}, "unknown", None, "no_studio_signal", {}),
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    _write_job_sidecar(sim_out, "j3", "pass", {
        "translation_review_auto_approval": (
            {"decision": "unevaluable"}, "unknown", None, "no_studio_signal", {}),
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    _write_job_sidecar(sim_out, "j4", "pass", {
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    _write_job_sidecar(sim_out, "j5", "pass", {
        "tts_duration_repair_policy": (
            {"unevaluable": True, "reason": "no_segments"}, "unknown", None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    rates = agg["stages_unevaluable_rate"]
    assert rates["translation_review_auto_approval"] == "3/5 (60%)"
    assert rates["subtitle_sync_policy"] == "4/5 (80%)"
    assert rates["tts_duration_repair_policy"] == "1/5 (20%)"


# ============================================================================
# C3: voice_selection_diff (with unknown_speakers handling) +
#     translation_review_diff + subtitle_drift_observations
# ============================================================================


def test_c3_voice_selection_diff_excludes_unknown_from_preset(tmp_path):
    """Studio voice_id classified as 'unknown' must NOT be counted as preset.

    Bucket counts: smart_studio_match / smart_more_clones / smart_fewer_clones /
    smart_unevaluable (no_studio_signal).
    Plus: studio_unknown_voices (jobs where studio_actual had any unknown voice).
    """
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # j1: smart=clone, studio=clone (vt_) -> match
    _write_job_sidecar(sim_out, "j_match", "pass", {
        "voice_sample_selection": (
            [{"speaker_index": 0, "choice": "clone", "reason": "≥10s_sample_available"}],
            [{"speaker_index": 0, "choice": "clone", "voice_id": "vt_speaker_a_1"}],
            True, "match", {}),
    })
    # j2: smart=clone, studio=preset -> smart_more_clones
    _write_job_sidecar(sim_out, "j_more", "pass", {
        "voice_sample_selection": (
            [{"speaker_index": 0, "choice": "clone", "reason": "≥10s_sample_available"}],
            [{"speaker_index": 0, "choice": "preset", "voice_id": "preset_x"}],
            False, "smart_more_aggressive", {}),
    })
    # j3: smart=preset, studio=clone -> smart_fewer_clones
    _write_job_sidecar(sim_out, "j_fewer", "pass", {
        "voice_sample_selection": (
            [{"speaker_index": 0, "choice": "preset", "reason": "no_eligible_sample"}],
            [{"speaker_index": 0, "choice": "clone", "voice_id": "vt_speaker_b_1"}],
            False, "smart_less_aggressive", {}),
    })
    # j4: studio voice unknown -> studio_unknown_voices=1, smart_unevaluable
    _write_job_sidecar(sim_out, "j_unk", "pass", {
        "voice_sample_selection": (
            [{"speaker_index": 0, "choice": "clone", "reason": "≥10s_sample_available"}],
            [{"speaker_index": 0, "choice": "unknown", "voice_id": "weird_id"}],
            None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    vsd = agg["voice_selection_diff"]
    assert vsd["jobs_evaluated"] == 4
    assert vsd["smart_studio_match"] == 1
    assert vsd["smart_more_clones"] == 1
    assert vsd["smart_fewer_clones"] == 1
    assert vsd["smart_unevaluable"] == 1
    # Critical: unknown studio voice tracked as its own signal, NOT bucketed as preset
    assert vsd["studio_unknown_voices"] == 1


def test_c3_translation_review_diff_quadrants(tmp_path):
    """translation_review_diff has 4 quadrants + smart_unevaluable."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # smart=auto_approve + studio=auto_approved
    _write_job_sidecar(sim_out, "j_aa_aa", "pass", {
        "translation_review_auto_approval": (
            {"decision": "auto_approve"}, "auto_approved", True, "match", {}),
    })
    # smart=auto_approve + studio=user_modified -> less_aggressive
    _write_job_sidecar(sim_out, "j_aa_um", "pass", {
        "translation_review_auto_approval": (
            {"decision": "auto_approve"}, "user_modified", False, "smart_less_aggressive", {}),
    })
    # smart=manual_review + studio=auto_approved -> more_aggressive
    _write_job_sidecar(sim_out, "j_mr_aa", "pass", {
        "translation_review_auto_approval": (
            {"decision": "manual_review_required"}, "auto_approved", False, "smart_more_aggressive", {}),
    })
    # smart=unevaluable
    _write_job_sidecar(sim_out, "j_unev", "pass", {
        "translation_review_auto_approval": (
            {"decision": "unevaluable"}, "unknown", None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    trd = agg["translation_review_diff"]
    assert trd["jobs_evaluated"] == 4
    assert trd["smart_auto_approved_studio_unmodified"] == 1
    assert trd["smart_auto_approved_studio_modified"] == 1
    assert trd["smart_rejected_studio_unmodified"] == 1
    assert trd["smart_rejected_studio_modified"] == 0
    assert trd["smart_unevaluable"] == 1


def test_c3_subtitle_drift_observations(tmp_path):
    """subtitle_drift_observations tallies drift counts from
    subtitle_sync_policy decision's studio_actual.drift_count."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 2 jobs with drift=0
    _write_job_sidecar(sim_out, "j_drift_0a", "pass", {
        "subtitle_sync_policy": (
            {"whisper_align_recommended": True}, {"alignment_model": "small", "drift_count": 0},
            True, "match", {}),
    })
    _write_job_sidecar(sim_out, "j_drift_0b", "pass", {
        "subtitle_sync_policy": (
            {"whisper_align_recommended": True}, {"alignment_model": "small", "drift_count": 0},
            True, "match", {}),
    })
    # 1 job with drift>0
    _write_job_sidecar(sim_out, "j_drift_5", "pass", {
        "subtitle_sync_policy": (
            {"whisper_align_recommended": True}, {"alignment_model": "small", "drift_count": 5},
            False, "orthogonal", {}),
    })
    # 1 unevaluable (pre-Phase-D)
    _write_job_sidecar(sim_out, "j_pre", "pass", {
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    sdo = agg["subtitle_drift_observations"]
    assert sdo["jobs_with_drift_data"] == 3
    assert sdo["jobs_with_drift_count_zero"] == 2
    assert sdo["jobs_with_drift_count_gt_zero"] == 1


# ============================================================================
# C4: retry_estimation_vs_actual + p2_readiness_signals
# ============================================================================


def test_c4_retry_estimation_vs_actual(tmp_path):
    """Aggregate retry estimation error: |smart_estimated - actual| / actual.

    p50/p90 over all jobs that have BOTH smart estimate AND actual count
    from metering."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 3 metered jobs with varying error
    for jid, smart_n, actual_n in [
        ("j_metered_1", 20, 21),  # ~5% off
        ("j_metered_2", 30, 30),  # 0%
        ("j_metered_3", 40, 39),  # ~3% off
    ]:
        _write_job_sidecar(sim_out, jid, "pass", {
            "tts_duration_repair_policy": (
                {"expected_retts_count": smart_n, "would_hit_budget_cap": False},
                {"actual_retts_count": actual_n,
                 "actual_retts_total_duration_ms": actual_n * 25000,
                 "data_source": "metering"},
                True, "match", {}),
        })
    # 1 fallback job — should NOT count toward jobs_with_metering
    _write_job_sidecar(sim_out, "j_fallback", "pass", {
        "tts_duration_repair_policy": (
            {"expected_retts_count": 7},
            {"actual_retts_count": None,
             "actual_retts_total_duration_ms": None,
             "data_source": "fallback_editor_segments"},
            None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    re = agg["retry_estimation_vs_actual"]
    assert re["jobs_with_metering"] == 3
    assert re["actual_retts_count_p50"] == 30
    assert re["estimation_formula_version"] == 1
    # error% < 100 (both p50 and p90)
    err_p50 = re["estimation_error_p50"]
    assert err_p50.endswith("%")


def test_c4_retry_estimation_metering_actual_only_when_smart_unevaluable(tmp_path):
    """When facts have metering data but smart says unevaluable (e.g. no
    project_dir / no segments), surface the gap honestly via three counts:
    jobs_with_metering_actual, jobs_with_smart_estimate, jobs_with_metering.
    """
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 2 metering jobs but smart unevaluable (no segments)
    for jid, actual_n in [("j_meter_a", 7), ("j_meter_b", 23)]:
        _write_job_sidecar(sim_out, jid, "pass", {
            "tts_duration_repair_policy": (
                {"unevaluable": True, "reason": "no_segments"},
                {"actual_retts_count": actual_n,
                 "actual_retts_total_duration_ms": actual_n * 25000,
                 "data_source": "metering"},
                None, "no_studio_signal", {}),
        })
    # 1 fully fallback job
    _write_job_sidecar(sim_out, "j_fb", "pass", {
        "tts_duration_repair_policy": (
            {"unevaluable": True, "reason": "no_segments"},
            {"actual_retts_count": None, "data_source": "fallback_editor_segments"},
            None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    re = agg["retry_estimation_vs_actual"]
    assert re["jobs_with_metering_actual"] == 2
    assert re["jobs_with_smart_estimate"] == 0
    assert re["jobs_with_metering"] == 0  # intersection empty
    assert re["estimation_error_p50"] == "n/a"
    # Warning text should reflect the gap, not just say "no metering"
    assert any("smart estimate is unavailable" in w
               or "smart could only estimate" in w
               for w in agg["warnings"])


def test_c4_p2_readiness_signals(tmp_path):
    """p2_readiness_signals.post_phase_metered_jobs counts jobs with metering
    data AND whisper alignment (proxy for post-Phase-D)."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    # 2 post-Phase-D metered jobs
    for jid in ["j_post_a", "j_post_b"]:
        _write_job_sidecar(sim_out, jid, "pass", {
            "tts_duration_repair_policy": (
                {"expected_retts_count": 10},
                {"actual_retts_count": 10, "data_source": "metering"},
                True, "match", {}),
            "subtitle_sync_policy": (
                {"whisper_align_recommended": True},
                {"alignment_model": "small", "drift_count": 0},
                True, "match", {}),
        })
    # 1 pre-Phase-D job (no whisper)
    _write_job_sidecar(sim_out, "j_pre", "pass", {
        "tts_duration_repair_policy": (
            {"expected_retts_count": 7},
            {"actual_retts_count": None, "data_source": "fallback_editor_segments"},
            None, "no_studio_signal", {}),
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    p2 = agg["p2_readiness_signals"]
    assert p2["post_phase_metered_jobs"] == 2
    assert p2["p2_threshold_metered_jobs"] == 10
    assert p2["ready_for_p2_rerun"] is False  # 2 < 10


# ============================================================================
# C5: warnings + markdown summary
# ============================================================================


def test_c5_warning_when_few_metered_jobs(tmp_path):
    """When post_phase_metered_jobs < 10, aggregator emits warning."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    _write_job_sidecar(sim_out, "j_only", "pass", {
        "subtitle_sync_policy": (
            {"unevaluable": True}, "unknown", None, "no_studio_signal", {}),
    })
    agg, _ = _run_aggregator(tmp_path)
    assert any("metered" in w.lower() for w in agg["warnings"])


def test_c5_markdown_summary_file_written(tmp_path):
    """aggregator writes aggregate_report.md alongside aggregate_report.json."""
    sim_out = tmp_path / "sim_out"
    sim_out.mkdir()
    _write_job_sidecar(sim_out, "j1", "pass", {
        "eligibility_gate": ({"decision": "pass", "main_count": 1}, "pass", True, "match", {}),
    })
    agg, out = _run_aggregator(tmp_path)
    md_path = out / "aggregate_report.md"
    assert md_path.is_file()
    md = md_path.read_text(encoding="utf-8")
    # Must contain key sections
    assert "Smart Shadow Aggregate" in md or "aggregate" in md.lower()
    assert "stage_decision_diff_breakdown" in md.lower() \
           or "stage decision" in md.lower()
    assert "eligibility_gate" in md or "eligibility" in md.lower()

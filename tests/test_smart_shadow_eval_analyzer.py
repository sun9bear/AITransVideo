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


def test_analyzer_clone_availability_section(tmp_path):
    """§4: 按 main_speaker_count(threshold=0.10) 分桶 → 每桶有≥1 个合格样本(≥5s) 的占比"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 3, "≥8s": 2, "≥10s": 0, "≥15s": 0}]}},
        {"schema_version": 1, "job_id": "j2",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 0, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
        {"schema_version": 1, "job_id": "j3",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 3}},
         "clone_sample_stats": {"eligible_speakers": 3,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 4, "≥8s": 2, "≥10s": 0, "≥15s": 0},
                {"≥5s": 2, "≥8s": 1, "≥10s": 0, "≥15s": 0}]}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§4 克隆样本可用率" in report
    assert "main=2" in report or "2 speakers" in report
    assert "main=3" in report or "3 speakers" in report


def test_analyzer_retry_section(tmp_path):
    """§5: retry/rewrite 分布 + 区分 metering vs fallback 数据源"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "duration_seconds": 60,
         "retry_stats": {"rewrite_count": 3, "retts_count": 5,
                          "retts_total_duration_ms": 12000,
                          "_data_source": "metering"},
         "usage_meter": {"rewrite_input_text_chars_total": 100}},
        {"schema_version": 1, "job_id": "j2",
         "duration_seconds": 120,
         "retry_stats": {"rewrite_count": 1, "retts_count": 2,
                          "retts_total_duration_ms": 4000,
                          "_data_source": "metering"},
         "usage_meter": {"rewrite_input_text_chars_total": 50}},
        {"schema_version": 1, "job_id": "j3",
         "duration_seconds": 60,
         "retry_stats": {"rewrite_count": 2, "retts_count": None,
                          "_data_source": "fallback_editor_segments"}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§5 Retry" in report or "§5 重试" in report
    assert "metering" in report.lower()
    assert "fallback" in report.lower()
    assert "rewrite_input_text_chars_total" in report


def test_analyzer_drift_section(tmp_path):
    """§6: text_audio_drift_count 分布（仅有 subtitle_quality_report 子集）"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 0}},
        {"schema_version": 1, "job_id": "j2",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 2}},
        {"schema_version": 1, "job_id": "j3",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 5}},
        {"schema_version": 1, "job_id": "j4_pre_b",
         "artifact_presence": {"subtitle_quality_report": False},
         "subtitle_sync": {"text_audio_drift_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§6 字幕一致性" in report
    assert "drift=0" in report or "无 drift" in report
    assert "Phase B+" in report or "subtitle_quality_report" in report


def test_analyzer_whisper_section_uses_cue_source_not_cache(tmp_path):
    """§7: alignment_model 分布 + whisper_aligned_cue_count / total，不用 cache_hits"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "artifact_presence": {"subtitle_cues": True},
         "whisper": {"alignment_model": "small",
                     "whisper_aligned_cue_count": 80,
                     "proportional_fallback_cue_count": 20,
                     "whisper_sidecar_count": 5}},
        {"schema_version": 1, "job_id": "j2",
         "artifact_presence": {"subtitle_cues": True},
         "whisper": {"alignment_model": "medium",
                     "whisper_aligned_cue_count": 100,
                     "proportional_fallback_cue_count": 0,
                     "whisper_sidecar_count": 8}},
        {"schema_version": 1, "job_id": "j3_pre_d",
         "artifact_presence": {"subtitle_cues": False},
         "whisper": {"alignment_model": None,
                     "whisper_aligned_cue_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§7 Whisper" in report
    assert "small" in report
    assert "medium" in report
    assert "whisper_aligned" in report or "aligned_cue" in report
    assert "wall_time" in report
    # Negative assertions — Codex iter 4 P1
    # §7 must NOT use cache_hits/cache_misses (those are §7b workflow_alignment_cache)
    assert "cache_hits" not in report
    assert "cache_misses" not in report


def test_analyzer_workflow_cache_section_with_explicit_not_whisper_warning(tmp_path):
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "workflow_alignment_cache": {"cache_hit_blocks": 8, "block_count": 10}},
        {"schema_version": 1, "job_id": "j2",
         "workflow_alignment_cache": {"cache_hit_blocks": 5, "block_count": 10}},
        {"schema_version": 1, "job_id": "j3",
         "workflow_alignment_cache": {"cache_hit_blocks": None, "block_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§7b" in report
    # CRITICAL: 必须含明确 NOT Whisper 警告
    assert "NOT Whisper" in report or "不是 Whisper" in report
    # 必须明示不能用作 Whisper-default-on 决策
    assert "不能用" in report or "do not use" in report.lower()


def test_analyzer_threshold_matrix(tmp_path):
    """§10: 4×4 matrix of Smart eligibility/rejection/degradation rates"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        # j1: 2 speakers, both have ≥10s samples → eligible at all thresholds
        {"schema_version": 1, "job_id": "j1",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 2, "≥15s": 1},
                {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0}]}},
        # j2: 4 speakers (gate fails at threshold 0.05/0.10) - rejected
        {"schema_version": 1, "job_id": "j2",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 4, "0.10": 4, "0.15": 3, "0.20": 2}},
         "clone_sample_stats": {"eligible_speakers": 4,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 4, "≥8s": 2, "≥10s": 0, "≥15s": 0},
                {"≥5s": 2, "≥8s": 0, "≥10s": 0, "≥15s": 0},
                {"≥5s": 1, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
        # j3: 3 speakers, 1 has insufficient samples - degraded at higher min_seconds
        {"schema_version": 1, "job_id": "j3",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 3, "0.10": 3, "0.15": 3, "0.20": 3}},
         "clone_sample_stats": {"eligible_speakers": 3,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 3, "≥8s": 1, "≥10s": 0, "≥15s": 0},
                {"≥5s": 0, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§10" in report
    assert "适配率" in report or "eligible" in report.lower()
    summary = json.loads((out / "report_summary.json").read_text(encoding="utf-8"))
    assert "threshold_matrix" in summary

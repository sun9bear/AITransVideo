"""PII injection guard for P1 simulator: reuses P0's PII_LITERALS list to ensure
no PII strings leak into smart_shadow_decisions.jsonl or smart_shadow_report.json.

By importing PII_LITERALS from the P0 guard test, we lock the two test suites
to the same set of sensitive literals — no drift between P0 and P1.
"""
import sys
import json
import subprocess
from pathlib import Path

# Reuse the same PII literals as P0 to avoid drift between the two guard tests.
# This is an explicit cross-test import — kept module-level so any P0 update
# auto-propagates to P1.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_smart_shadow_eval_collector_pii_guard import PII_LITERALS

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_sim_simulator.py")


def test_no_pii_in_simulator_output(tmp_path):
    """Inject PII into facts.jsonl + project_dir → simulator output must contain none."""
    # Build a fact with PII in metadata fields
    pii_chinese = PII_LITERALS[0]  # e.g. "贝基·奎克"
    pii_phone = next((p for p in PII_LITERALS if p.isdigit()), "13800138000")
    fact = {
        "schema_version": 1,
        "job_id": "job_pii_test",
        "project_id": "test_pid_pii",
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-06T08:00:00+00:00",
        "duration_seconds": 60,
        # Some optional fields with PII content (simulator should NOT echo these):
        "tts_provider": "minimax",
        "tts_model": "speech-2.8-hd",
    }
    facts = tmp_path / "facts.jsonl"
    facts.write_text(json.dumps(fact, ensure_ascii=False) + "\n")

    # Build a minimal project_dir with PII strings in user_edit_events / segments
    projects_root = tmp_path / "projects"
    project_dir = projects_root / "test_pid_pii" / "job_pii_test"
    (project_dir / "audit").mkdir(parents=True)
    (project_dir / "editor").mkdir()
    (project_dir / "audit" / "user_edit_events.jsonl").write_text(
        json.dumps({
            "event_type": "translation_segment_text_changed",
            "effective_marker": "effective",
            "before": {"cn_text": f"{pii_chinese} 联系电话 {pii_phone}"},
            "after": {"cn_text": "正常翻译"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps([{
            "segment_id": "1",
            "speaker_id": "speaker_a",
            "voice_id": "moss_audio_xxx",
            "cn_text": f"{pii_chinese} 你好",
            "display_name": pii_chinese,
        }], ensure_ascii=False),
        encoding="utf-8",
    )

    out = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--projects-root", str(projects_root),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

    # Concatenate all simulator outputs and assert no PII literal leaked
    job_dir = out / "job_pii_test"
    all_output = ""
    for fname in ("smart_shadow_decisions.jsonl", "smart_shadow_report.json"):
        p = job_dir / fname
        if p.is_file():
            all_output += p.read_text(encoding="utf-8")
    summary_path = out / "summary.json"
    if summary_path.is_file():
        all_output += summary_path.read_text(encoding="utf-8")

    for lit in PII_LITERALS:
        assert lit not in all_output, (
            f"PII leak in simulator output: {lit!r} found"
        )

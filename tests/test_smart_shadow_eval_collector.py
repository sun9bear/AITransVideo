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


def test_classify_voice_id_vt_prefix_is_cloned(tmp_path):
    """Production cloned voice IDs use 'vt_*' prefix (per process.py::_validate_cloned_voices).

    Regression: this prefix was previously misclassified as 'preset', causing
    false smart_more_aggressive findings in P1 shadow simulator.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collector_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Direct unit test of helper
    assert mod._classify_voice_id("vt_speaker_a_1777851965742") == "cloned"
    assert mod._classify_voice_id("vt_") == "cloned"  # edge: bare prefix


def test_classify_voice_id_unknown_default(tmp_path):
    """Unrecognized voice_id -> 'unknown', NOT 'preset' by default.

    Defaulting to 'preset' caused false smart_more_aggressive findings.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collector_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._classify_voice_id("some_unknown_id") == "unknown"
    assert mod._classify_voice_id("") == "unknown"
    assert mod._classify_voice_id("auto") == "unknown"


def test_classify_voice_id_explicit_preset(tmp_path):
    """preset_* prefix is the only way to be classified as 'preset'."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collector_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._classify_voice_id("preset_chinese_male_1") == "preset"
    assert mod._classify_voice_id("preset_anything") == "preset"


def test_classify_voice_id_existing_clone_patterns_still_work(tmp_path):
    """moss_audio_* and UUID-like patterns continue to classify as cloned."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collector_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._classify_voice_id("moss_audio_85bcf79d-00f2-11f1-b80b-cafa791d3a11") == "cloned"
    assert mod._classify_voice_id("abc123def456-7890-abcd-ef01-23456789abcd") == "cloned"


def test_classify_voice_id_minimax_mandarin_descriptive_is_preset():
    """Pattern A (Gap C, 2026-05-07): MiniMax catalog "Chinese (Mandarin)_*" descriptive
    voice IDs classify as preset.

    Evidence: 10 instances across 5 unique voice_ids in 38-job production scan
    (run_id 2026-05-06T22-42Z). All 5 unique IDs verified present in
    src/services/tts/minimax_voice_catalog_604.json.
    """
    mod = _load_collector_module()
    assert mod._classify_voice_id("Chinese (Mandarin)_News_Anchor") == "preset"
    assert mod._classify_voice_id("Chinese (Mandarin)_Radio_Host") == "preset"
    assert mod._classify_voice_id("Chinese (Mandarin)_Reliable_Executive") == "preset"
    assert mod._classify_voice_id("Chinese (Mandarin)_Gentle_Senior") == "preset"
    assert mod._classify_voice_id("Chinese (Mandarin)_Warm_Girl") == "preset"
    assert mod._classify_voice_id("Chinese (Mandarin)_Male_Announcer") == "preset"


def test_classify_voice_id_minimax_chinese_vv_is_preset():
    """Pattern B (Gap C, 2026-05-07): MiniMax catalog "Chinese_*_vv1" / "Chinese_*_vv2"
    system-named voice IDs classify as preset.

    Evidence: 24 instances across 7 unique voice_ids in 38-job production scan.
    All 7 unique IDs verified present in minimax_voice_catalog_604.json.
    """
    mod = _load_collector_module()
    # vv1 examples
    assert mod._classify_voice_id("Chinese_radio_reporter_vv1") == "preset"
    assert mod._classify_voice_id("Chinese_radio_host_male_vv1") == "preset"
    assert mod._classify_voice_id("Chinese_deep_voiced_male_vv1") == "preset"
    assert mod._classify_voice_id("Chinese_financial_reporter_vv1") == "preset"
    # vv2 examples
    assert mod._classify_voice_id("Chinese_gravelly_storyteller_vv2") == "preset"
    assert mod._classify_voice_id("Chinese_casual_storyteller_vv2") == "preset"
    assert mod._classify_voice_id("Chinese_casual_instructor_vv2") == "preset"


def test_classify_voice_id_cosyvoice_v3_suffix_is_preset():
    """Pattern C (Gap C, 2026-05-07): CosyVoice "*_v3" suffix voice IDs
    classify as preset.

    Evidence: 3 instances across 3 unique voice_ids in 38-job production scan
    (loongbella_v3, longshuo_v3, longanlang_v3). 66/68 voices in
    src/services/tts/cosyvoice_voice_catalog.py end with "_v3" — robust suffix.

    Cloned voices win first (vt_/moss_audio_/UUID prefix checks run before this),
    so a hypothetical user-cloned "vt_anything_v3" would still classify as cloned.
    """
    mod = _load_collector_module()
    assert mod._classify_voice_id("loongbella_v3") == "preset"
    assert mod._classify_voice_id("longshuo_v3") == "preset"
    assert mod._classify_voice_id("longanlang_v3") == "preset"


def test_classify_voice_id_below_threshold_patterns_stay_unknown():
    """Gap C decision (2026-05-07): patterns with < 3 production instances
    are NOT auto-classified — they surface as unknown for future maintenance.

    Specifically:
    - "Wise_Woman" (3 same-literal instances, NOT a pattern; not in MiniMax 604
      catalog — could be deprecated/legacy ID, needs catalog confirmation)
    - "zh_male_liufei_uranus_bigtts" (1 instance, "*_bigtts" is VolcEngine 1.0/2.0
      naming convention but n=1 below ≥3 evidence threshold)

    Per task constraint "至少 3 个同模式实例才纳入,不要主观猜": don't extend
    classifier on intuition. These are surfaced as unknown so future
    maintenance with more samples can decide.
    """
    mod = _load_collector_module()
    assert mod._classify_voice_id("Wise_Woman") == "unknown"
    assert mod._classify_voice_id("zh_male_liufei_uranus_bigtts") == "unknown"


def test_classify_voice_id_v2_does_not_overreach():
    """v2 new patterns must NOT match obviously unrelated strings.

    Defends against accidentally classifying random IDs as preset because
    their suffix happens to match (e.g., "anything_v3" beyond CosyVoice
    catalog, "Chinese_*" without _vv1/_vv2, etc.).
    """
    mod = _load_collector_module()
    # Bare "_v3" but not a real CosyVoice convention — wait, this WILL match.
    # We accept the suffix's looseness because cloned/UUID checks above catch
    # the realistic alternatives. Document that "*_v3" is a deliberate broad
    # bucket (any non-cloned, non-vt_, non-moss_audio_ voice ending _v3 is
    # treated as preset). If a future cloned-voice naming scheme conflicts,
    # the cloned prefix check should be extended FIRST, not the preset check
    # narrowed.
    # Below: assert things that should still be unknown.
    assert mod._classify_voice_id("just_chinese") == "unknown"  # no _vv suffix
    assert mod._classify_voice_id("Chinese_no_suffix") == "unknown"  # missing vv1/vv2
    assert mod._classify_voice_id("Chinese_radio_vv3") == "unknown"  # vv3 not in v1/v2 set
    assert mod._classify_voice_id("Mandarin_News_Anchor") == "unknown"  # missing "Chinese (" prefix
    # Edge: case-sensitive — production data is title-case, so we don't lowercase
    assert mod._classify_voice_id("chinese (mandarin)_news_anchor") == "unknown"


def _load_collector_module():
    """Load smart_shadow_eval_collector.py as a module for direct helper testing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collector_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project_dir_with_segments(tmp_path: Path, segments: list) -> Path:
    """Build a minimal project_dir with editor/segments.json containing given segments."""
    pdir = tmp_path / "proj_test"
    (pdir / "editor").mkdir(parents=True)
    (pdir / "editor" / "segments.json").write_text(
        json.dumps(segments), encoding="utf-8"
    )
    return pdir


def test_actual_clone_stats_unknown_voice_id_counted(tmp_path):
    """voice_id that doesn't match any cloned/preset pattern -> unknown_speakers count.

    Regression for 195911c follow-up: voice_ids classified as 'unknown' were
    silently dropped from cloned/preset buckets, breaking
    cloned + preset == len(voice_ids) invariant. unknown_speakers makes the
    classification surface explicit so aggregator stats are honest.
    """
    mod = _load_collector_module()
    pdir = _make_project_dir_with_segments(tmp_path, [
        {"segment_id": "1", "speaker_id": "speaker_a",
         "voice_id": "weird_unrecognized_format_xyz",
         "start_ms": 0, "end_ms": 1000},
    ])
    acs = mod._compute_actual_clone_stats(pdir)
    assert acs is not None
    assert acs["unknown_speakers"] == 1
    assert acs["cloned_speakers"] == 0
    assert acs["preset_speakers"] == 0
    assert acs["voice_ids_by_speaker"] == ["weird_unrecognized_format_xyz"]


def test_actual_clone_stats_buckets_sum_invariant(tmp_path):
    """cloned_speakers + preset_speakers + unknown_speakers == len(voice_ids_by_speaker).

    Each speaker contributes to exactly one bucket. Aggregator-side cross-job
    statistics rely on this; when the invariant breaks, dashboards lie."""
    mod = _load_collector_module()
    pdir = _make_project_dir_with_segments(tmp_path, [
        {"segment_id": "1", "speaker_id": "spk_clone",
         "voice_id": "vt_speaker_a_1234567890", "start_ms": 0, "end_ms": 1000},
        {"segment_id": "2", "speaker_id": "spk_preset",
         "voice_id": "preset_chinese_male_1", "start_ms": 1000, "end_ms": 2000},
        {"segment_id": "3", "speaker_id": "spk_unknown",
         "voice_id": "mystery_id_abc", "start_ms": 2000, "end_ms": 3000},
    ])
    acs = mod._compute_actual_clone_stats(pdir)
    assert acs is not None
    total = (acs["cloned_speakers"] + acs["preset_speakers"]
             + acs["unknown_speakers"])
    assert total == len(acs["voice_ids_by_speaker"]) == 3
    assert acs["cloned_speakers"] == 1
    assert acs["preset_speakers"] == 1
    assert acs["unknown_speakers"] == 1


def test_uncertain_speaker_duration_share_basic(tmp_path):
    """speaker_stats.uncertain_speaker_duration_share = sum of correction
    audit_event durations / total transcript duration.

    P1 simulator translation_review_auto_approval depends on this signal —
    when missing, every job lands in 'unevaluable: missing_signals'. Closing
    Gap B from P1 Done note §5.2.
    """
    mod = _load_collector_module()
    pdir = tmp_path / "proj"
    (pdir / "transcript").mkdir(parents=True)
    # 100s transcript, 1 line
    (pdir / "transcript" / "transcript.json").write_text(json.dumps({
        "lines": [
            {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 100000},
        ],
    }), encoding="utf-8")
    # Audit: 1 correction covering 30s (30%)
    (pdir / "transcript" / "s2_review_audit.json").write_text(json.dumps({
        "audit_events": [
            {"start_ms": 10000, "end_ms": 40000, "source": "correction",
             "before_speaker_id": "speaker_b", "after_speaker_id": "speaker_a"},
        ],
    }), encoding="utf-8")
    share = mod._compute_uncertain_speaker_duration_share(pdir)
    assert share is not None
    assert abs(share - 0.30) < 1e-6


def test_uncertain_speaker_duration_share_no_audit_file(tmp_path):
    """Missing s2_review_audit.json → None (not 0.0).

    None propagates 'unevaluable: missing_signals' through the simulator.
    Returning 0.0 would falsely promote pre-Phase-A jobs to auto_approve."""
    mod = _load_collector_module()
    pdir = tmp_path / "proj"
    (pdir / "transcript").mkdir(parents=True)
    (pdir / "transcript" / "transcript.json").write_text(json.dumps({
        "lines": [{"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 1000}],
    }), encoding="utf-8")
    assert mod._compute_uncertain_speaker_duration_share(pdir) is None


def test_uncertain_speaker_duration_share_zero_when_no_corrections(tmp_path):
    """Audit file present but 0 corrections → 0.0 (different from None).

    Distinct semantics: this means S2 Pass 1 ran and confirmed every original
    speaker assignment — the ASR was confident. Smart should auto_approve."""
    mod = _load_collector_module()
    pdir = tmp_path / "proj"
    (pdir / "transcript").mkdir(parents=True)
    (pdir / "transcript" / "transcript.json").write_text(json.dumps({
        "lines": [{"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 100000}],
    }), encoding="utf-8")
    (pdir / "transcript" / "s2_review_audit.json").write_text(json.dumps({
        "audit_events": [],
    }), encoding="utf-8")
    share = mod._compute_uncertain_speaker_duration_share(pdir)
    assert share == 0.0


def test_uncertain_speaker_duration_share_skips_non_correction_sources(tmp_path):
    """Only source=='correction' counts. sanity_check / other sources ignored.

    sanity_check events (n=2 in 43-job local survey) are S2's "I checked,
    no change needed" markers — they should NOT inflate uncertainty share.
    """
    mod = _load_collector_module()
    pdir = tmp_path / "proj"
    (pdir / "transcript").mkdir(parents=True)
    (pdir / "transcript" / "transcript.json").write_text(json.dumps({
        "lines": [{"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 100000}],
    }), encoding="utf-8")
    (pdir / "transcript" / "s2_review_audit.json").write_text(json.dumps({
        "audit_events": [
            {"start_ms": 0, "end_ms": 50000, "source": "sanity_check"},
            {"start_ms": 50000, "end_ms": 60000, "source": "correction"},
        ],
    }), encoding="utf-8")
    share = mod._compute_uncertain_speaker_duration_share(pdir)
    assert abs(share - 0.10) < 1e-6  # only the 10s correction


def test_speaker_stats_includes_uncertain_speaker_duration_share(tmp_path):
    """End-to-end: collector run produces fact with
    speaker_stats.uncertain_speaker_duration_share populated.

    Wires the helper into _compute_speaker_stats output so simulator's
    _decide_translation_review can read fact.speaker_stats.uncertain_*.
    """
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
    assert "uncertain_speaker_duration_share" in f["speaker_stats"]
    # Fixture has no s2_review_audit.json → expect None
    assert f["speaker_stats"]["uncertain_speaker_duration_share"] is None


def test_actual_clone_stats_classifications_by_speaker_parallel(tmp_path):
    """classifications_by_speaker is a parallel list to voice_ids_by_speaker.

    Per-position classification surfaces *which* speaker is unknown, not
    just the count. Useful for downstream debugging without re-running
    _classify_voice_id on every consumer.
    """
    mod = _load_collector_module()
    pdir = _make_project_dir_with_segments(tmp_path, [
        {"segment_id": "1", "speaker_id": "spk_a",
         "voice_id": "vt_abc_123", "start_ms": 0, "end_ms": 1000},
        {"segment_id": "2", "speaker_id": "spk_b",
         "voice_id": "preset_x", "start_ms": 1000, "end_ms": 2000},
        {"segment_id": "3", "speaker_id": "spk_c",
         "voice_id": "weirdo", "start_ms": 2000, "end_ms": 3000},
    ])
    acs = mod._compute_actual_clone_stats(pdir)
    assert "classifications_by_speaker" in acs
    assert acs["classifications_by_speaker"] == ["cloned", "preset", "unknown"]
    # Parallel arrays — same length, same order:
    assert len(acs["classifications_by_speaker"]) == \
           len(acs["voice_ids_by_speaker"])


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


def test_until_filter_iso_sentinel_ordering():
    """Lock in the +99:99 sentinel comparison trick — must order correctly across
    the day-boundary regardless of any timezone suffix in actual job timestamps.
    """
    # The sentinel
    until_marker = "2026-04-19" + "T23:59:59.999999+99:99"
    # Real timestamps that should sort BEFORE the marker (within Apr 19)
    inside_day = "2026-04-19T13:36:59+00:00"
    inside_day_late = "2026-04-19T23:59:58+00:00"
    inside_day_neg = "2026-04-19T23:59:59-12:00"
    # And the next day, which should sort AFTER
    next_day = "2026-04-20T00:00:00+00:00"

    assert inside_day < until_marker
    assert inside_day_late < until_marker
    assert inside_day_neg < until_marker
    assert next_day > until_marker

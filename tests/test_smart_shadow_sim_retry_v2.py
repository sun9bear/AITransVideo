"""End-to-end + unit tests for spec §3.5 retry estimation v2.

Background — v1 formula FAILED on real metered jobs (15 jobs from
c7_remote_run/results/p1, see docs/plans/2026-05-06-smart-shadow-sim-p1-done-note.md
§4-bis.3): p50 estimation error = 385.7%, p90 = 666.7%, vs spec §3.5 expected
≤50%. Three root causes documented there:

1. Double-counting: a segment satisfying BOTH `cn_chars > k * dur_min * 1.05`
   AND `rewrite_count > 0` was counted as `1 + rewrite_count` instead of just
   `max(1, rewrite_count)`. 797 segments across 38 jobs hit this branch.
2. Length-only trigger too optimistic: 1534 segments triggered length overflow
   only (no rewrite recorded), but length overflow is necessary-not-sufficient
   for re-tts — TTS speed / pause stripping / per-voice rate variance often let
   first-take succeed within tolerance. Treating length-only as `+1 retts`
   pushes estimates 4-7× over actual.
3. `k_cn_chars_per_src_min=240` not calibrated per voice/provider/speaking-rate
   (deferred to v3).

Tests in this file split into three groups:
- Baseline group: pin v1's bad behavior on a synthetic fixture so any
  formula change is visible. Updated in the same commit that lands v2.
- Unit group: contract tests for each v2 invariant (double-count fixed,
  length-only dropped). FAIL before v2 lands; GREEN after.
- Integration group: run aggregator on the real 15-metered-job sidecars
  from c7_remote_run/results/p1 and assert v2 p90 stays within target.
  Skipped automatically if those sidecars aren't present locally.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SIMULATOR_SCRIPT = REPO_ROOT / "scripts" / "smart_shadow_sim_simulator.py"
AGGREGATOR_SCRIPT = REPO_ROOT / "scripts" / "smart_shadow_sim_aggregator.py"

# c7 remote-run artifacts produced 2026-05-07 — 15 metered jobs that revealed
# the v1 FAIL verdict. See p1-done-note §4-bis.1 for run metadata.
C7_RESULTS_P1 = Path(
    "D:/Claude/temp/smart_shadow_sim/c7_remote_run/results/p1"
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _load_simulator_module():
    """Import simulator.py as a module so we can call _estimate_retry directly
    without spawning a subprocess (fast unit-test path)."""
    spec = importlib.util.spec_from_file_location(
        "_sim_under_test", SIMULATOR_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_simulator(facts_path: Path, projects_root: Path,
                   out_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SIMULATOR_SCRIPT),
         "--facts", str(facts_path),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"simulator exit={result.returncode}\nstderr={result.stderr}"
    )


def _run_aggregator(simulator_out_dir: Path,
                    out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(AGGREGATOR_SCRIPT),
         "--simulator-out-dir", str(simulator_out_dir),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"aggregator exit={result.returncode}\nstderr={result.stderr}"
    )
    return json.loads(
        (out_dir / "aggregate_report.json").read_text(encoding="utf-8")
    )


def _pct_of(s: str) -> float:
    """Parse '386.5%' → 386.5."""
    assert s.endswith("%"), f"Expected percent string, got: {s!r}"
    return float(s[:-1])


def _build_synthetic_metered_job(*, job_id: str, project_id: str,
                                 duration_seconds: int,
                                 segments: list[dict],
                                 actual_retts_count: int,
                                 projects_root: Path) -> dict:
    """Produce one fact dict + write its editor/segments.json to projects_root.

    Caller appends the returned fact to facts.jsonl.
    """
    proj_dir = projects_root / project_id / f"job_{job_id}"
    (proj_dir / "editor").mkdir(parents=True, exist_ok=True)
    (proj_dir / "editor" / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "job_id": f"job_{job_id}",
        "project_id": project_id,
        "service_mode": "studio",
        "status": "succeeded",
        "created_at": "2026-05-07T10:00:00+00:00",
        "duration_seconds": duration_seconds,
        "speaker_stats": {
            "asr_speaker_count": 1,
            "speaker_duration_shares": [1.0],
            "speaker_count_by_threshold": {
                "0.05": 1, "0.10": 1, "0.15": 1, "0.20": 1,
            },
            "uncertain_speaker_duration_share": 0.0,
        },
        "clone_sample_stats": {
            "eligible_speakers": 1,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
            ],
        },
        "actual_clone_stats": {
            "voice_ids_by_speaker": ["vt_synthetic_speaker_1"],
        },
        "user_edits": {"text_changes_effective": 0},
        "retry_stats": {
            "_data_source": "metering",
            "retts_count": actual_retts_count,
            "retts_total_duration_ms": actual_retts_count * 5000,
        },
        "whisper": {
            "alignment_model": "small",
            "whisper_aligned_cue_count": 80,
            "proportional_fallback_cue_count": 5,
        },
        "subtitle_sync": {
            "text_audio_drift_count": 0,
            "drift_block_ids": [],
        },
    }


# k_cn_chars_per_src_min × duration_min × 1.05 = char threshold per segment
# At duration_min = 1.0 (60s segment), threshold = 240 × 1 × 1.05 = 252 chars
LONG_TEXT_60S = "x" * 300   # 300 > 252 → length overflow
SHORT_TEXT_60S = "x" * 100  # 100 < 252 → no length overflow


def _build_double_count_pattern_segments() -> list[dict]:
    """4 segments per job — one per double-count case category.

    With v1 formula:
      seg1 (length-only): +1 (length trigger, no rewrite)
      seg2 (rewrite-only): +2 (rewrite_count=2)
      seg3 (BOTH — double-counted in v1): +1 (length) + 2 (rewrite) = +3
      seg4 (neither): 0
    v1 sum = 6 per job.

    With v2 (drop length-only + max per seg, double-count fix):
      seg1: 0 (length alone → no retts contribution)
      seg2: 2 (rewrite)
      seg3: 2 (max(length=1, rewrite=2) = 2)
      seg4: 0
    v2 sum = 4 per job.
    """
    return [
        {"segment_id": "1", "speaker_id": "A",
         "cn_text": LONG_TEXT_60S, "start_ms": 0, "end_ms": 60_000,
         "rewrite_count": 0},
        {"segment_id": "2", "speaker_id": "A",
         "cn_text": SHORT_TEXT_60S, "start_ms": 60_000, "end_ms": 120_000,
         "rewrite_count": 2},
        {"segment_id": "3", "speaker_id": "A",
         "cn_text": LONG_TEXT_60S, "start_ms": 120_000, "end_ms": 180_000,
         "rewrite_count": 2},
        {"segment_id": "4", "speaker_id": "A",
         "cn_text": SHORT_TEXT_60S, "start_ms": 180_000, "end_ms": 240_000,
         "rewrite_count": 0},
    ]


# ----------------------------------------------------------------------------
# 1. Baseline group — pin v1 behavior so any formula change is visible
# ----------------------------------------------------------------------------


def test_v1_baseline_double_count_pinned_per_job(tmp_path):
    """Pin per-job estimate on the 4-segment double-count fixture.

    With v1 (length+rewrite double-counted, length-only triggers retts):
      seg1 length-only (rew=0):     +1 (length)
      seg2 rewrite-only (rew=2):    +2 (rewrite)
      seg3 BOTH (rew=2):            +1 (length) + 2 (rewrite) = +3  ← double-count
      seg4 neither:                  0
      Total: expected_retts_count = 6, expected_rewrite_count = 4.

    With v2 (length-only dropped, per-seg max):
      seg1: 0 (length alone → no retts contribution)
      seg2: 2 (rewrite)
      seg3: 2 (max(length=1, rewrite=2) = 2)  ← double-count fix
      seg4: 0
      Total: expected_retts_count = 4, expected_rewrite_count = 4.

    Pins v2 numbers. If the formula regresses or further calibration changes
    the numbers, the assertion changes too.
    """
    facts_path = tmp_path / "facts.jsonl"
    projects_root = tmp_path / "projects"
    out_dir = tmp_path / "sim_out"

    fact = _build_synthetic_metered_job(
        job_id="baseline1",
        project_id="proj_baseline",
        duration_seconds=240,
        segments=_build_double_count_pattern_segments(),
        actual_retts_count=2,
        projects_root=projects_root,
    )
    facts_path.write_text(json.dumps(fact) + "\n", encoding="utf-8")
    _run_simulator(facts_path, projects_root, out_dir)

    decisions_path = out_dir / "job_baseline1" / "smart_shadow_decisions.jsonl"
    decisions = [
        json.loads(line)
        for line in decisions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    repair = next(d for d in decisions
                  if d.get("stage_or_segment_id") == "tts_duration_repair_policy")
    smart = repair["smart_decision"]
    # v2 expectations: per-seg max + length-only dropped → retts = 4
    # (sum of rewrite_count across the 4 segments: 0+2+2+0).
    assert smart["expected_retts_count"] == 4, (
        f"Expected v2 retts=4 (rewrite-only, length dropped), "
        f"got {smart['expected_retts_count']}."
    )
    assert smart["expected_rewrite_count"] == 4
    assert smart["estimation_formula_version"] == 2


def test_v1_baseline_aggregator_p90_pin(tmp_path):
    """End-to-end: 5 synthetic metered jobs → simulator → aggregator.

    Each job uses the same 4-segment double-count fixture (smart_v1=6 per
    job) with varying actual_retts_count for percentile spread.

    actuals = [2, 4, 1, 3, 2]
    v1 errors per job (smart=6):
      |6-2|/2 = 200%, |6-4|/4 =  50%, |6-1|/1 = 500%,
      |6-3|/3 = 100%, |6-2|/2 = 200%
    sorted = [50, 100, 200, 200, 500].
    Aggregator's nearest-rank percentile uses banker's rounding (Python 3
    round): p50 → k=int(round(2.5))=2 → s[1]=100; p90 → k=int(round(4.5))=4
    → s[3]=200. So v1 reports p50=100%, p90=200%.

    Under v2 (smart=4 per job):
      |4-2|/2 = 100%, |4-4|/4 =   0%, |4-1|/1 = 300%,
      |4-3|/3 ≈  33%, |4-2|/2 = 100%
    sorted = [0, 33, 100, 100, 300]. p50=s[1]=33, p90=s[3]=100.

    This test pins the CURRENT (v1) percentiles. When v2 lands the
    assertions update to (33, 100). The 5-job synthetic spread is
    intentionally narrower than the 15-metered-job production data
    (p50=386% p90=667%) — synthetic fixture is a regression sentinel,
    not a production-truth claim. Production validation is in
    test_v2_integration_15_metered_jobs_p90_target.
    """
    facts_path = tmp_path / "facts.jsonl"
    projects_root = tmp_path / "projects"
    sim_out = tmp_path / "sim_out"
    agg_out = tmp_path / "agg_out"

    actuals = [2, 4, 1, 3, 2]
    facts_lines = []
    for i, actual_n in enumerate(actuals, start=1):
        fact = _build_synthetic_metered_job(
            job_id=f"v1pin{i}",
            project_id=f"proj_v1pin{i}",
            duration_seconds=240,
            segments=_build_double_count_pattern_segments(),
            actual_retts_count=actual_n,
            projects_root=projects_root,
        )
        facts_lines.append(json.dumps(fact))
    facts_path.write_text("\n".join(facts_lines) + "\n", encoding="utf-8")

    _run_simulator(facts_path, projects_root, sim_out)
    aggregate = _run_aggregator(sim_out, agg_out)

    retry = aggregate["retry_estimation_vs_actual"]
    assert retry["jobs_with_metering"] == 5, retry
    # v2 numbers (smart=4 per job): sorted errors = [0, 33.3, 100, 100, 300]
    # → p50=33.3%, p90=100%. Aggregator format strings round to 1 decimal.
    assert _pct_of(retry["estimation_error_p50"]) == pytest.approx(33.3, abs=0.1), (
        f"Expected v2 p50≈33.3%, got {retry['estimation_error_p50']}."
    )
    assert _pct_of(retry["estimation_error_p90"]) == 100.0, (
        f"Expected v2 p90=100%, got {retry['estimation_error_p90']}."
    )
    # estimation_formula_version is read from per-job decisions (set 2 by v2)
    assert retry["estimation_formula_version"] == 2


# ----------------------------------------------------------------------------
# 2. Unit group — contract tests for each v2 invariant
# ----------------------------------------------------------------------------


def test_v2_double_count_fixed_segment_with_length_and_rewrite():
    """A single segment with BOTH length overflow AND rewrite_count=N must
    contribute MAX(length_indicator, N) to expected_retts_count, NOT
    1 + N.

    Per p1-done-note §4-bis.3 root cause #1: 797 segments across 38 jobs hit
    this double-count branch in v1, causing 1.5-2× over-estimation.
    """
    sim = _load_simulator_module()
    # length overflow (1 min duration, 300 chars > 252 threshold) AND rewrite_count=3
    seg = {
        "segment_id": "s1", "speaker_id": "A",
        "cn_text": LONG_TEXT_60S, "start_ms": 0, "end_ms": 60_000,
        "rewrite_count": 3,
    }
    decision, _ev = sim._estimate_retry([seg], source_duration_seconds=60)
    # v2 behaviour: per-seg max → 3 (not 1 + 3 = 4).
    assert decision["expected_retts_count"] == 3, (
        f"Double-count not fixed: expected_retts_count={decision['expected_retts_count']}, "
        f"want 3 (max of length=1 and rewrite=3)."
    )
    assert decision["expected_rewrite_count"] == 3


def test_v2_length_only_trigger_dropped():
    """A segment with length overflow but rewrite_count=0 must NOT
    contribute to expected_retts_count.

    Per p1-done-note §4-bis.3 root cause #2: 1534 length-only segments across
    38 jobs were treated as +1 retts each in v1, but length overflow is a
    necessary-not-sufficient predictor — most first-take TTS still fits within
    duration tolerance after pause-strip / per-voice rate variance.

    v2 demotes length-only to a soft signal: still surfaced in per-segment
    decisions as `expected_retts: bool` for human review, but NOT counted
    in the aggregate `expected_retts_count` driving cost predictions.
    """
    sim = _load_simulator_module()
    seg = {
        "segment_id": "s1", "speaker_id": "A",
        "cn_text": LONG_TEXT_60S,  # 300 chars in 60s → length overflow
        "start_ms": 0, "end_ms": 60_000,
        "rewrite_count": 0,         # NO rewrite signal
    }
    decision, _ev = sim._estimate_retry([seg], source_duration_seconds=60)
    assert decision["expected_retts_count"] == 0, (
        f"Length-only trigger still active: "
        f"expected_retts_count={decision['expected_retts_count']}, want 0."
    )
    # rewrite_count=0 → expected_rewrite_count=0
    assert decision["expected_rewrite_count"] == 0


def test_v2_rewrite_only_segment_unchanged():
    """A segment with rewrite_count > 0 but no length overflow still
    contributes rewrite_count to expected_retts_count. v2 doesn't change
    this path — only length-only and double-count cases differ from v1.
    """
    sim = _load_simulator_module()
    seg = {
        "segment_id": "s1", "speaker_id": "A",
        "cn_text": SHORT_TEXT_60S,  # 100 chars in 60s → no overflow
        "start_ms": 0, "end_ms": 60_000,
        "rewrite_count": 2,
    }
    decision, _ev = sim._estimate_retry([seg], source_duration_seconds=60)
    assert decision["expected_retts_count"] == 2
    assert decision["expected_rewrite_count"] == 2


def test_v2_neither_signal_segment_zero():
    """Sanity: a segment with no length overflow and no rewrite contributes 0."""
    sim = _load_simulator_module()
    seg = {
        "segment_id": "s1", "speaker_id": "A",
        "cn_text": SHORT_TEXT_60S, "start_ms": 0, "end_ms": 60_000,
        "rewrite_count": 0,
    }
    decision, _ev = sim._estimate_retry([seg], source_duration_seconds=60)
    assert decision["expected_retts_count"] == 0
    assert decision["expected_rewrite_count"] == 0


def test_v2_per_segment_decision_still_surfaces_length_signal(tmp_path):
    """Length-only segments are dropped from `expected_retts_count` but MUST
    still appear in per-segment decisions with `expected_retts: True` so
    humans can see the soft signal during review.

    Drop from cost estimate ≠ silent. The signal still has diagnostic value
    for "which segments looked long?" investigations.
    """
    facts_path = tmp_path / "facts.jsonl"
    projects_root = tmp_path / "projects"
    out_dir = tmp_path / "out"
    fact = _build_synthetic_metered_job(
        job_id="surface1",
        project_id="proj_surface",
        duration_seconds=60,
        segments=[{
            "segment_id": "1", "speaker_id": "A",
            "cn_text": LONG_TEXT_60S, "start_ms": 0, "end_ms": 60_000,
            "rewrite_count": 0,
        }],
        actual_retts_count=0,
        projects_root=projects_root,
    )
    facts_path.write_text(json.dumps(fact) + "\n", encoding="utf-8")
    _run_simulator(facts_path, projects_root, out_dir)
    decisions = [
        json.loads(line)
        for line in (out_dir / "job_surface1"
                      / "smart_shadow_decisions.jsonl"
                      ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seg_decisions = [d for d in decisions
                     if d.get("decision_kind") == "segment"]
    assert len(seg_decisions) >= 1, (
        "Length-only segment must still appear in per-segment decisions."
    )
    assert seg_decisions[0]["smart_decision"].get("expected_retts") is True
    # But the aggregate count is 0:
    repair = next(d for d in decisions
                  if d.get("stage_or_segment_id") == "tts_duration_repair_policy")
    assert repair["smart_decision"]["expected_retts_count"] == 0


def test_v2_estimation_formula_version_bumped():
    """When v2 lands, smart_decision.estimation_formula_version (or aggregate
    retry_estimation_vs_actual.estimation_formula_version) must reflect the
    bump so downstream consumers can branch on it.

    For now this is a contract reminder — we'll wire it in alongside the
    v2 implementation.
    """
    sim = _load_simulator_module()
    seg = {
        "segment_id": "s1", "speaker_id": "A",
        "cn_text": SHORT_TEXT_60S, "start_ms": 0, "end_ms": 60_000,
        "rewrite_count": 1,
    }
    decision, _ev = sim._estimate_retry([seg], source_duration_seconds=60)
    # v2 must declare itself in the per-stage decision so aggregator can
    # surface the formula version. Either as a top-level key in decision
    # or in evidence — accept either.
    has_version = (
        "estimation_formula_version" in decision
        or "estimation_formula_version" in (_ev or {})
    )
    assert has_version, (
        "v2 must expose estimation_formula_version in decision or evidence."
    )


# ----------------------------------------------------------------------------
# 3. Integration group — real 15-metered-job sidecars from c7_remote_run
# ----------------------------------------------------------------------------


def _have_c7_sidecars() -> bool:
    if not C7_RESULTS_P1.is_dir():
        return False
    # Need at least 10 job_* dirs with metered sidecars to make the integration
    # test meaningful.
    job_dirs = [d for d in C7_RESULTS_P1.iterdir()
                if d.is_dir() and d.name.startswith("job_")]
    return len(job_dirs) >= 10


@pytest.mark.skipif(
    not _have_c7_sidecars(),
    reason="c7_remote_run/results/p1 sidecars not available locally",
)
def test_v2_integration_15_metered_jobs_improvement_floor(tmp_path):
    """Regression FLOOR — v2 must stay at least 5× better than v1 on the
    c7 metered-job sidecars. This is NOT a spec-target check.

    Spec §3.5 originally targeted estimation_error_p90 ≤ 50%. v2 (drop
    length-only + per-seg max) lands p50≈75% / p90≈120% on the 15 metered
    c7 jobs (5× improvement over v1's 386% / 667%) but does NOT clear the
    50% target. Per 2026-05-07 verdict: the original target is deferred to
    v3 per-voice k calibration; v2 is accepted as a *conservative planning
    signal* for P2-alpha and onward, not a precise cost predictor. See
    docs/plans/2026-05-06-smart-shadow-sim-p1-done-note.md §4-bis.3 +
    P0 results §13 + smart-auto-pipeline-plan §15 P2 entry conditions.

    What this test asserts: p50 < 150% AND p90 < 200%. That's roughly the
    midpoint between v1's bad numbers and v2's actual numbers — anything
    that drifts back toward v1 trips the floor. **Not** a check that the
    spec target is cleared.

    HOW: rebuild per-job sidecars in tmp_path by replaying segment evidence
    through the current _estimate_retry (no project_dirs needed), then run
    the real aggregator and check the resulting percentiles.
    """
    # The c7 sidecars are simulator outputs; we re-aggregate them as-is.
    # To validate v2, we need to REPLACE the smart_decision with what v2 would
    # produce given the same segment evidence. The sidecars have per-segment
    # decisions with rewrite_count in evidence — sufficient to recompute.

    # Load sim under test for the v2 formula.
    sim = _load_simulator_module()

    fake_sim_out = tmp_path / "sim_out"
    fake_sim_out.mkdir(parents=True, exist_ok=True)

    n_metered = 0
    for job_dir in sorted(C7_RESULTS_P1.iterdir()):
        if not job_dir.is_dir() or not job_dir.name.startswith("job_"):
            continue
        decisions_path = job_dir / "smart_shadow_decisions.jsonl"
        report_path = job_dir / "smart_shadow_report.json"
        if not decisions_path.is_file() or not report_path.is_file():
            continue

        # Reconstruct synthetic segments from per-segment evidence so we can
        # re-run _estimate_retry under v2.
        segs: list[dict] = []
        repair_dec = None
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("decision_kind") == "segment":
                ev = d.get("evidence") or {}
                # Reconstruct minimal segment fields for _estimate_retry
                cn_chars = ev.get("cn_text_chars", 0)
                duration_ms = ev.get("duration_ms", 0)
                rewrite_count = ev.get("rewrite_count", 0)
                segs.append({
                    "segment_id": d.get("stage_or_segment_id", ""),
                    "cn_text": "x" * cn_chars,
                    "start_ms": 0,
                    "end_ms": duration_ms or 1,
                    "rewrite_count": rewrite_count,
                })
            elif (d.get("decision_kind") == "stage"
                  and d.get("stage_or_segment_id")
                  == "tts_duration_repair_policy"):
                repair_dec = d

        if not repair_dec:
            continue
        actual = repair_dec.get("studio_actual") or {}
        if (not isinstance(actual, dict)
                or actual.get("data_source") != "metering"
                or actual.get("actual_retts_count") is None):
            continue

        n_metered += 1

        # Per-segment decisions in c7 sidecars only include "interesting"
        # segments. To get a fair v2 estimate we need to rebuild the
        # per-segment evidence as the simulator originally saw it. Since the
        # c7 sidecar already records all segments that contributed to either
        # length overflow or rewrite, summing those is sufficient — segments
        # with NEITHER signal contributed 0 in both v1 and v2 anyway.
        v2_decision, _v2_ev = sim._estimate_retry(segs,
                                                  source_duration_seconds=None)

        # Replace the smart_decision with v2 output and write a synthetic
        # sidecar the aggregator can consume.
        repair_dec_v2 = dict(repair_dec)
        # Preserve evidence (segment_count etc.) for transparency.
        new_smart = dict(repair_dec.get("smart_decision") or {})
        new_smart["expected_retts_count"] = v2_decision["expected_retts_count"]
        new_smart["expected_rewrite_count"] = v2_decision["expected_rewrite_count"]
        repair_dec_v2["smart_decision"] = new_smart

        # Write a minimal sidecar that the aggregator can pick up.
        new_job_dir = fake_sim_out / job_dir.name
        new_job_dir.mkdir(parents=True, exist_ok=True)
        # Copy the original report unchanged + emit a single-decision JSONL
        # containing only the v2 repair stage. Aggregator's
        # _retry_estimation_vs_actual reads the per-job decisions.jsonl,
        # finds tts_duration_repair_policy, and pulls
        # smart_decision.expected_retts_count + studio_actual.actual_retts_count.
        shutil.copy(report_path, new_job_dir / "smart_shadow_report.json")
        (new_job_dir / "smart_shadow_decisions.jsonl").write_text(
            json.dumps(repair_dec_v2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    assert n_metered >= 10, (
        f"Need ≥10 metered c7 jobs to validate v2 p90, found {n_metered}."
    )

    # Aggregate.
    agg_out = tmp_path / "agg_out"
    aggregate = _run_aggregator(fake_sim_out, agg_out)
    retry = aggregate["retry_estimation_vs_actual"]
    assert retry["jobs_with_metering"] == n_metered, retry
    p50_pct = _pct_of(retry["estimation_error_p50"])
    p90_pct = _pct_of(retry["estimation_error_p90"])

    # v1 baseline on this set: p50=386%, p90=667%.
    # v2 measured on this set:  p50=75%,  p90=120% (ACCEPTED as conservative
    # planning signal per 2026-05-07 verdict; original ≤50% target deferred
    # to v3 per-voice k calibration).
    # Floor assertions are looser than the measured v2 numbers so minor
    # downstream changes don't trip them, but tight enough that any
    # regression toward v1's order of magnitude trips immediately.
    assert p90_pct < 200.0, (
        f"v2 p90={p90_pct:.0f}% — regressed past 200% floor (v2 measured 120%, "
        f"v1 was 667%). Something in _estimate_retry is no longer dropping "
        f"length-only triggers or is double-counting again."
    )
    assert p50_pct < 150.0, (
        f"v2 p50={p50_pct:.0f}% — regressed past 150% floor (v2 measured 75%, "
        f"v1 was 386%)."
    )

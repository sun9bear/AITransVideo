"""Smart Shadow Simulator Aggregator (P1) — read multiple per-job sidecars,
emit cross-job aggregate report (JSON + markdown). Read-only, offline,
stdlib-only.

Quick usage:
  python scripts/smart_shadow_sim_aggregator.py \
    --simulator-out-dir D:/Claude/temp/smart_shadow_sim/local_smoke \
    --out-dir D:/Claude/temp/smart_shadow_sim/local_smoke

See docs/plans/2026-05-06-smart-shadow-sim-design.md §4.3.
"""
from __future__ import annotations
import argparse
import datetime
import json
import socket
import subprocess as sp
import sys
from pathlib import Path


SCHEMA_VERSION = 1

# 6 stages emitted by simulator (per design §3.4).
STAGE_IDS = (
    "eligibility_gate",
    "voice_sample_selection",
    "clone_policy",
    "translation_review_auto_approval",
    "tts_duration_repair_policy",
    "subtitle_sync_policy",
)

# 5-bucket diff_kind enum from simulator (per design §3.3).
DIFF_KINDS = (
    "match",
    "smart_more_aggressive",
    "smart_less_aggressive",
    "orthogonal",
    "no_studio_signal",
)

# Threshold for "ready_for_p2_rerun" — see plan §15 P0 results note §7.1.
P2_THRESHOLD_METERED_JOBS = 10


# ----------------------------------------------------------------------------
# IO helpers
# ----------------------------------------------------------------------------


def _git_sha() -> str:
    try:
        out = sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=sp.DEVNULL, text=True, timeout=2,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _make_run_id() -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%MZ")
    return f"{ts}-{socket.gethostname()}-{_git_sha()}"


def _load_per_job_data(simulator_out_dir: Path) -> list[dict]:
    """Glob <simulator_out_dir>/<job_id>/{smart_shadow_report.json,smart_shadow_decisions.jsonl}.

    Returns list of dicts: {"report": dict, "decisions": list[dict]}.
    Skips job dirs missing either file or with malformed JSON.
    """
    if not simulator_out_dir.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(simulator_out_dir.iterdir()):
        if not child.is_dir():
            continue
        report_path = child / "smart_shadow_report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        decisions: list[dict] = []
        decisions_path = child / "smart_shadow_decisions.jsonl"
        if decisions_path.is_file():
            try:
                for line in decisions_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        decisions.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                pass
        out.append({"report": report, "decisions": decisions})
    return out


def _stage_decisions_by_id(decisions: list[dict], stage_id: str) -> list[dict]:
    return [d for d in decisions
            if d.get("decision_kind") == "stage"
            and d.get("stage_or_segment_id") == stage_id]


def _format_rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0/0 (n/a)"
    pct = round(100 * numerator / denominator)
    return f"{numerator}/{denominator} ({pct}%)"


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # Nearest-rank, 1-indexed.
    k = max(1, int(round(pct / 100.0 * len(s))))
    return s[min(k - 1, len(s) - 1)]


# ----------------------------------------------------------------------------
# Aggregations (one function per top-level field)
# ----------------------------------------------------------------------------


def _smart_eligibility_breakdown(per_job: list[dict]) -> dict:
    """Bucket each job by report.smart_eligibility."""
    out: dict = {}
    for entry in per_job:
        elig = entry["report"].get("smart_eligibility", "unknown")
        out[elig] = out.get(elig, 0) + 1
    return out


def _stage_decision_diff_breakdown(per_job: list[dict]) -> dict:
    """For each of 6 stages, count diff_kind across jobs (5 buckets) +
    match_rate string."""
    out: dict = {}
    for stage_id in STAGE_IDS:
        bucket = {dk: 0 for dk in DIFF_KINDS}
        n_evaluated = 0
        for entry in per_job:
            stage_dec = _stage_decisions_by_id(entry["decisions"], stage_id)
            if not stage_dec:
                continue
            n_evaluated += 1
            dk = stage_dec[0].get("diff_kind", "no_studio_signal")
            if dk in bucket:
                bucket[dk] += 1
            else:
                bucket[dk] = bucket.get(dk, 0) + 1
        bucket["match_rate"] = _format_rate(bucket["match"], n_evaluated)
        out[stage_id] = bucket
    return out


def _stages_unevaluable_rate(per_job: list[dict]) -> dict:
    """Per-stage: (jobs where stage in stages_unevaluable) / total jobs."""
    n_jobs = len(per_job)
    out: dict = {}
    for stage_id in STAGE_IDS:
        unev_count = sum(
            1 for e in per_job
            if stage_id in (e["report"].get("stages_unevaluable") or [])
        )
        out[stage_id] = _format_rate(unev_count, n_jobs)
    return out


def _voice_selection_diff(per_job: list[dict]) -> dict:
    """Cross-job voice_sample_selection diff distribution.

    Buckets:
    - smart_studio_match: diff_kind == "match"
    - smart_more_clones: diff_kind == "smart_more_aggressive"
    - smart_fewer_clones: diff_kind == "smart_less_aggressive"
    - smart_unevaluable: diff_kind == "no_studio_signal" or smart unevaluable
    - studio_unknown_voices: count of jobs whose studio_actual contained any
      'unknown' choice (vt_/preset_/moss_audio_ classification = unknown).
      Tracked separately so unknown voices are NEVER counted as preset.
    """
    out = {
        "jobs_evaluated": 0,
        "smart_studio_match": 0,
        "smart_more_clones": 0,
        "smart_fewer_clones": 0,
        "smart_unevaluable": 0,
        "studio_unknown_voices": 0,
    }
    for entry in per_job:
        stage_dec = _stage_decisions_by_id(
            entry["decisions"], "voice_sample_selection")
        if not stage_dec:
            continue
        out["jobs_evaluated"] += 1
        d = stage_dec[0]
        dk = d.get("diff_kind")
        if dk == "match":
            out["smart_studio_match"] += 1
        elif dk == "smart_more_aggressive":
            out["smart_more_clones"] += 1
        elif dk == "smart_less_aggressive":
            out["smart_fewer_clones"] += 1
        elif dk in ("no_studio_signal", "orthogonal"):
            out["smart_unevaluable"] += 1
        # Track unknown studio voices separately. studio_actual may be a list
        # of {speaker_index, choice, voice_id} or "unknown" string.
        actual = d.get("studio_actual")
        if isinstance(actual, list) and any(
            isinstance(a, dict) and a.get("choice") == "unknown"
            for a in actual
        ):
            out["studio_unknown_voices"] += 1
    return out


def _translation_review_diff(per_job: list[dict]) -> dict:
    """4 quadrant + smart_unevaluable.

    Quadrants based on (smart_decision.decision, studio_actual):
    - smart_auto_approved_studio_unmodified: smart=auto_approve, studio=auto_approved
    - smart_auto_approved_studio_modified:   smart=auto_approve, studio=user_modified (smart missed)
    - smart_rejected_studio_unmodified:      smart=manual_review_required, studio=auto_approved (smart over-cautious)
    - smart_rejected_studio_modified:        smart=manual_review_required, studio=user_modified
    """
    out = {
        "jobs_evaluated": 0,
        "smart_auto_approved_studio_unmodified": 0,
        "smart_auto_approved_studio_modified": 0,
        "smart_rejected_studio_unmodified": 0,
        "smart_rejected_studio_modified": 0,
        "smart_unevaluable": 0,
    }
    for entry in per_job:
        stage_dec = _stage_decisions_by_id(
            entry["decisions"], "translation_review_auto_approval")
        if not stage_dec:
            continue
        out["jobs_evaluated"] += 1
        d = stage_dec[0]
        smart = d.get("smart_decision") or {}
        smart_dec = smart.get("decision") if isinstance(smart, dict) else None
        actual = d.get("studio_actual")
        if smart_dec == "unevaluable" or actual == "unknown" \
                or d.get("diff_kind") == "no_studio_signal":
            out["smart_unevaluable"] += 1
            continue
        if smart_dec == "auto_approve" and actual == "auto_approved":
            out["smart_auto_approved_studio_unmodified"] += 1
        elif smart_dec == "auto_approve" and actual == "user_modified":
            out["smart_auto_approved_studio_modified"] += 1
        elif smart_dec == "manual_review_required" and actual == "auto_approved":
            out["smart_rejected_studio_unmodified"] += 1
        elif smart_dec == "manual_review_required" and actual == "user_modified":
            out["smart_rejected_studio_modified"] += 1
        else:
            out["smart_unevaluable"] += 1
    return out


def _subtitle_drift_observations(per_job: list[dict]) -> dict:
    """Tally drift counts from subtitle_sync_policy.studio_actual.drift_count.

    Smart doesn't predict drift (drift is post-hoc). This is observational
    Studio-side cost/quality signal.
    """
    out = {
        "jobs_with_drift_data": 0,
        "jobs_with_drift_count_zero": 0,
        "jobs_with_drift_count_gt_zero": 0,
    }
    for entry in per_job:
        stage_dec = _stage_decisions_by_id(
            entry["decisions"], "subtitle_sync_policy")
        if not stage_dec:
            continue
        actual = stage_dec[0].get("studio_actual")
        if not isinstance(actual, dict):
            continue
        drift = actual.get("drift_count")
        if drift is None:
            continue
        out["jobs_with_drift_data"] += 1
        if drift == 0:
            out["jobs_with_drift_count_zero"] += 1
        else:
            out["jobs_with_drift_count_gt_zero"] += 1
    return out


def _retry_estimation_vs_actual(per_job: list[dict]) -> dict:
    """Compute estimation error over jobs where BOTH smart estimate AND actual
    metering count are available.

    Three counts are surfaced (they differ when project_dirs are unavailable
    locally — e.g. running aggregator on facts without an extracted project
    tree, smart can't compute its segment-level estimate):

    - jobs_with_metering_actual: facts have metering-sourced retts count.
    - jobs_with_smart_estimate: simulator produced an `expected_retts_count`
      (requires editor/segments.json access).
    - jobs_with_metering: BOTH above (== `jobs_evaluable_for_estimation`).
      Estimation error percentiles are over this intersection only.
    """
    smart_vals: list[int] = []
    actual_vals: list[int] = []
    err_pcts: list[float] = []
    n_metering_actual = 0
    n_smart_estimate = 0
    formula_versions: set[int] = set()
    for entry in per_job:
        stage_dec = _stage_decisions_by_id(
            entry["decisions"], "tts_duration_repair_policy")
        if not stage_dec:
            continue
        d = stage_dec[0]
        actual = d.get("studio_actual")
        smart = d.get("smart_decision")
        # Count metering presence (actual side) independently of smart side.
        if isinstance(actual, dict) and actual.get("data_source") == "metering" \
                and actual.get("actual_retts_count") is not None:
            n_metering_actual += 1
        if isinstance(smart, dict) \
                and smart.get("expected_retts_count") is not None:
            n_smart_estimate += 1
            v = smart.get("estimation_formula_version")
            if isinstance(v, int):
                formula_versions.add(v)
        if not isinstance(actual, dict) or not isinstance(smart, dict):
            continue
        if actual.get("data_source") != "metering":
            continue
        actual_n = actual.get("actual_retts_count")
        smart_n = smart.get("expected_retts_count")
        if actual_n is None or smart_n is None:
            continue
        smart_vals.append(smart_n)
        actual_vals.append(actual_n)
        if actual_n > 0:
            err_pcts.append(100.0 * abs(smart_n - actual_n) / actual_n)
        elif smart_n == 0:
            err_pcts.append(0.0)
        else:
            err_pcts.append(100.0)  # actual=0 but smart predicted some — treat as 100%
    # Surface formula version from simulator decisions when available; default
    # 2 (current spec §3.5 v2) for sidecars produced by older simulators that
    # didn't emit the field. If decisions span multiple versions we emit the
    # set so downstream consumers can detect mixed-version aggregates.
    if not formula_versions:
        formula_version_out: int | str = 2
    elif len(formula_versions) == 1:
        formula_version_out = next(iter(formula_versions))
    else:
        formula_version_out = ",".join(
            str(v) for v in sorted(formula_versions))
    out = {
        "jobs_with_metering_actual": n_metering_actual,
        "jobs_with_smart_estimate": n_smart_estimate,
        "jobs_with_metering": len(actual_vals),  # BOTH sides — evaluable for error
        "smart_estimated_retts_count_p50": _percentile(smart_vals, 50),
        "smart_estimated_retts_count_p90": _percentile(smart_vals, 90),
        "actual_retts_count_p50": _percentile(actual_vals, 50),
        "actual_retts_count_p90": _percentile(actual_vals, 90),
        "estimation_error_p50": (
            f"{_percentile(err_pcts, 50):.1f}%"
            if _percentile(err_pcts, 50) is not None else "n/a"),
        "estimation_error_p90": (
            f"{_percentile(err_pcts, 90):.1f}%"
            if _percentile(err_pcts, 90) is not None else "n/a"),
        "estimation_formula_version": formula_version_out,
    }
    return out


def _p2_readiness_signals(per_job: list[dict]) -> dict:
    """post_phase_metered_jobs = jobs with metering data AND whisper alignment.

    Used as gate for "are we ready to re-run P0 / proceed toward P2"
    (per plan §15 P0 results note §7.1: ≥10 metered, ≥20 for P2 entry).
    """
    n_metered = 0
    for entry in per_job:
        # post-Phase-D = has metering data AND has whisper alignment
        retts_dec = _stage_decisions_by_id(
            entry["decisions"], "tts_duration_repair_policy")
        sub_dec = _stage_decisions_by_id(
            entry["decisions"], "subtitle_sync_policy")
        if not retts_dec or not sub_dec:
            continue
        retts_actual = retts_dec[0].get("studio_actual") or {}
        sub_actual = sub_dec[0].get("studio_actual") or {}
        if not isinstance(retts_actual, dict) or not isinstance(sub_actual, dict):
            continue
        if retts_actual.get("data_source") != "metering":
            continue
        if not sub_actual.get("alignment_model"):
            continue
        n_metered += 1
    return {
        "post_phase_metered_jobs": n_metered,
        "p2_threshold_metered_jobs": P2_THRESHOLD_METERED_JOBS,
        "ready_for_p2_rerun": n_metered >= P2_THRESHOLD_METERED_JOBS,
    }


def _user_edit_observations(per_job: list[dict]) -> dict:
    """Counts derived from translation_review_diff and voice_selection_diff
    studio_actual signals."""
    n_text_changes = 0
    for entry in per_job:
        tr = _stage_decisions_by_id(
            entry["decisions"], "translation_review_auto_approval")
        if tr and tr[0].get("studio_actual") == "user_modified":
            n_text_changes += 1
    return {
        "jobs_with_text_changes": n_text_changes,
        "_note": ("observational only — Smart does not directly do speaker "
                  "correction or split (those are S2 Pass1 stage, shared "
                  "between Studio and Smart). See voice_selection_diff and "
                  "translation_review_diff for indirect comparisons."),
    }


def _build_warnings(per_job: list[dict], p2_signals: dict, retry_stats: dict,
                    voice_diff: dict) -> list[str]:
    warnings: list[str] = []
    if not per_job:
        warnings.append(
            "No per-job reports found in simulator-out-dir — nothing to aggregate.")
        return warnings
    if p2_signals["post_phase_metered_jobs"] < P2_THRESHOLD_METERED_JOBS:
        warnings.append(
            f"Only {p2_signals['post_phase_metered_jobs']} post-Phase-D metered "
            f"job(s) found; need ≥{P2_THRESHOLD_METERED_JOBS} for P0 re-run "
            "and ≥20 to consider P2 — smoke set too small for production decisions."
        )
    n_actual = retry_stats.get("jobs_with_metering_actual", 0)
    n_smart = retry_stats.get("jobs_with_smart_estimate", 0)
    n_eval = retry_stats["jobs_with_metering"]
    if n_eval == 0 and n_actual == 0:
        warnings.append(
            "No metering-sourced retry data — retry_estimation_vs_actual is "
            "INCONCLUSIVE; estimation accuracy cannot be measured.")
    elif n_eval == 0 and n_actual > 0 and n_smart == 0:
        warnings.append(
            f"{n_actual} job(s) have metering-sourced actual retts count but "
            "smart estimate is unavailable for all (project_dirs/editor "
            "segments not accessible at run time). Estimation error "
            "INCONCLUSIVE — re-run with project_dirs available to evaluate "
            "smart's retry-estimation formula.")
    elif n_eval == 0 and n_actual > 0:
        warnings.append(
            f"{n_actual} job(s) have metering data but smart could only "
            f"estimate retts on {n_smart}; intersection is empty so error "
            "INCONCLUSIVE.")
    if voice_diff.get("studio_unknown_voices", 0) > 0:
        warnings.append(
            f"{voice_diff['studio_unknown_voices']} job(s) had unknown studio "
            "voice IDs (not vt_/moss_audio_/preset_/UUID); voice_selection_diff "
            "for those is unevaluable. Investigate voice_id provenance.")
    return warnings


# ----------------------------------------------------------------------------
# Markdown summary writer
# ----------------------------------------------------------------------------


def _build_markdown_summary(aggregate: dict) -> str:
    """Render aggregate dict to a human-readable markdown summary."""
    lines: list[str] = []
    lines.append("# Smart Shadow Aggregate Report")
    lines.append("")
    lines.append(f"- **run_id**: `{aggregate.get('run_id', 'n/a')}`")
    lines.append(f"- **generated_at**: {aggregate.get('generated_at', 'n/a')}")
    lines.append(f"- **simulator_out_dir**: `{aggregate.get('simulator_out_dir', 'n/a')}`")
    lines.append(f"- **jobs_simulated**: {aggregate.get('jobs_simulated', 0)}")
    lines.append("")

    # Eligibility breakdown
    eb = aggregate.get("smart_eligibility_breakdown", {})
    lines.append("## Smart Eligibility Breakdown")
    lines.append("")
    if eb:
        for k, v in sorted(eb.items()):
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- (no data)")
    lines.append("")

    # Stage decision diff breakdown
    lines.append("## Stage Decision Diff Breakdown (5-bucket per stage)")
    lines.append("")
    sd = aggregate.get("stage_decision_diff_breakdown", {})
    if sd:
        lines.append(
            "| stage | match | more_aggressive | less_aggressive | orthogonal | "
            "no_studio_signal | match_rate |")
        lines.append(
            "|---|---|---|---|---|---|---|")
        for stage_id in STAGE_IDS:
            b = sd.get(stage_id, {})
            lines.append(
                f"| `{stage_id}` | {b.get('match', 0)} | "
                f"{b.get('smart_more_aggressive', 0)} | "
                f"{b.get('smart_less_aggressive', 0)} | "
                f"{b.get('orthogonal', 0)} | "
                f"{b.get('no_studio_signal', 0)} | "
                f"{b.get('match_rate', 'n/a')} |")
    lines.append("")

    # Per-stage unevaluable rate
    lines.append("## Per-Stage Unevaluable Rate")
    lines.append("")
    ur = aggregate.get("stages_unevaluable_rate", {})
    if ur:
        for stage_id in STAGE_IDS:
            lines.append(f"- `{stage_id}`: {ur.get(stage_id, 'n/a')}")
    lines.append("")

    # Voice selection diff
    vsd = aggregate.get("voice_selection_diff", {})
    if vsd:
        lines.append("## Voice Selection Diff")
        lines.append("")
        lines.append(f"- jobs_evaluated: {vsd.get('jobs_evaluated', 0)}")
        lines.append(f"- smart_studio_match: {vsd.get('smart_studio_match', 0)}")
        lines.append(f"- smart_more_clones: {vsd.get('smart_more_clones', 0)}")
        lines.append(f"- smart_fewer_clones: {vsd.get('smart_fewer_clones', 0)}")
        lines.append(f"- smart_unevaluable: {vsd.get('smart_unevaluable', 0)}")
        lines.append(f"- studio_unknown_voices: {vsd.get('studio_unknown_voices', 0)} "
                     "(NOT counted as preset)")
        lines.append("")

    # Translation review diff
    trd = aggregate.get("translation_review_diff", {})
    if trd:
        lines.append("## Translation Review Diff")
        lines.append("")
        lines.append(f"- jobs_evaluated: {trd.get('jobs_evaluated', 0)}")
        lines.append("- smart_auto_approved_studio_unmodified: "
                     f"{trd.get('smart_auto_approved_studio_unmodified', 0)} (true positive)")
        lines.append("- smart_auto_approved_studio_modified: "
                     f"{trd.get('smart_auto_approved_studio_modified', 0)} (smart missed → false negative)")
        lines.append("- smart_rejected_studio_unmodified: "
                     f"{trd.get('smart_rejected_studio_unmodified', 0)} (smart over-cautious → false positive)")
        lines.append("- smart_rejected_studio_modified: "
                     f"{trd.get('smart_rejected_studio_modified', 0)} (true positive caught)")
        lines.append(f"- smart_unevaluable: {trd.get('smart_unevaluable', 0)}")
        lines.append("")

    # Subtitle drift observations
    sdo = aggregate.get("subtitle_drift_observations", {})
    if sdo:
        lines.append("## Subtitle Drift Observations")
        lines.append("")
        lines.append(f"- jobs_with_drift_data: {sdo.get('jobs_with_drift_data', 0)}")
        lines.append(f"- jobs_with_drift_count_zero: {sdo.get('jobs_with_drift_count_zero', 0)}")
        lines.append(f"- jobs_with_drift_count_gt_zero: {sdo.get('jobs_with_drift_count_gt_zero', 0)}")
        lines.append("")

    # Retry estimation
    re_ = aggregate.get("retry_estimation_vs_actual", {})
    if re_:
        lines.append("## Retry Estimation vs Actual")
        lines.append("")
        lines.append(f"- jobs_with_metering_actual: {re_.get('jobs_with_metering_actual', 0)} "
                     "(facts have metering retts)")
        lines.append(f"- jobs_with_smart_estimate: {re_.get('jobs_with_smart_estimate', 0)} "
                     "(simulator computed `expected_retts_count`)")
        lines.append(f"- jobs_with_metering: {re_.get('jobs_with_metering', 0)} "
                     "(BOTH — evaluable for estimation error)")
        lines.append(f"- smart_estimated_retts_count p50/p90: "
                     f"{re_.get('smart_estimated_retts_count_p50', 'n/a')} / "
                     f"{re_.get('smart_estimated_retts_count_p90', 'n/a')}")
        lines.append(f"- actual_retts_count p50/p90: "
                     f"{re_.get('actual_retts_count_p50', 'n/a')} / "
                     f"{re_.get('actual_retts_count_p90', 'n/a')}")
        lines.append(f"- estimation_error p50/p90: "
                     f"{re_.get('estimation_error_p50', 'n/a')} / "
                     f"{re_.get('estimation_error_p90', 'n/a')}")
        lines.append(f"- formula_version: {re_.get('estimation_formula_version', 'n/a')}")
        lines.append("")

    # P2 readiness
    p2 = aggregate.get("p2_readiness_signals", {})
    if p2:
        lines.append("## P2 Readiness Signals")
        lines.append("")
        lines.append(f"- post_phase_metered_jobs: {p2.get('post_phase_metered_jobs', 0)}")
        lines.append(f"- p2_threshold_metered_jobs: {p2.get('p2_threshold_metered_jobs', 0)}")
        ready = p2.get("ready_for_p2_rerun", False)
        lines.append(f"- **ready_for_p2_rerun**: {'YES' if ready else 'NO'}")
        lines.append("")

    # Warnings
    warns = aggregate.get("warnings", [])
    lines.append("## Warnings")
    lines.append("")
    if warns:
        for w in warns:
            lines.append(f"- ⚠️ {w}")
    else:
        lines.append("- (none)")
    lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow simulator aggregator (P1, read-only)."
    )
    parser.add_argument("--simulator-out-dir", required=True,
                        help="Path to dir containing per-job <job_id>/smart_shadow_report.json files.")
    parser.add_argument("--projects-root", required=False,
                        help="Optional. Project artifacts root for cross-job stats.")
    parser.add_argument("--out-dir", required=True,
                        help="Aggregator output dir for aggregate_report.json + .md.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    sim_out = Path(args.simulator_out_dir)
    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: out_dir not writable: {exc}", file=sys.stderr)
        return 2

    per_job = _load_per_job_data(sim_out)

    # Build all aggregations.
    eligibility = _smart_eligibility_breakdown(per_job)
    stage_diff = _stage_decision_diff_breakdown(per_job)
    unev_rate = _stages_unevaluable_rate(per_job)
    voice_diff = _voice_selection_diff(per_job)
    trans_diff = _translation_review_diff(per_job)
    drift_obs = _subtitle_drift_observations(per_job)
    retry_stats = _retry_estimation_vs_actual(per_job)
    p2_signals = _p2_readiness_signals(per_job)
    user_edits = _user_edit_observations(per_job)
    warnings = _build_warnings(per_job, p2_signals, retry_stats, voice_diff)

    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _make_run_id(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "simulator_out_dir": str(sim_out),
        "jobs_simulated": len(per_job),
        "smart_eligibility_breakdown": eligibility,
        "stage_decision_diff_breakdown": stage_diff,
        "stages_unevaluable_rate": unev_rate,
        "user_edit_observations": user_edits,
        "voice_selection_diff": voice_diff,
        "translation_review_diff": trans_diff,
        "subtitle_drift_observations": drift_obs,
        "retry_estimation_vs_actual": retry_stats,
        "p2_readiness_signals": p2_signals,
        "warnings": warnings,
    }

    (out_dir / "aggregate_report.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "aggregate_report.md").write_text(
        _build_markdown_summary(aggregate),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

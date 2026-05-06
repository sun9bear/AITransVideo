"""Smart Shadow Evaluator analyzer — read facts.jsonl + pricing snapshot, emit report.md.

Quick usage:
  python scripts/smart_shadow_eval_analyzer.py \\
    --facts D:/Claude/temp/smart_shadow_eval/<run_id>/facts.jsonl \\
    --summary D:/Claude/temp/smart_shadow_eval/<run_id>/summary.json \\
    --pricing-runtime-snapshot D:/Claude/temp/.../pricing_runtime.json \\
    --out-dir D:/Claude/temp/.../report

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


SCHEMA_VERSION = 1
AVG_REWRITE_CHARS = 30  # fallback per-rewrite char estimate (no metering data)


def _percentile(sorted_xs, p: float):
    """Return percentile p (0..1) from a pre-sorted iterable."""
    if not sorted_xs:
        return None
    idx = min(len(sorted_xs) - 1, int(len(sorted_xs) * p))
    return sorted_xs[idx]


def _section_metadata(facts, summary, args):
    return [
        "## §1 Run Metadata",
        f"- run_id: {(summary or {}).get('run_id', 'N/A')}",
        f"- facts loaded: {len(facts)}",
        f"- jobs_factsheeted: {((summary or {}).get('scan_stats') or {}).get('jobs_factsheeted', 'N/A')}",
        f"- is_complete_run: {(summary or {}).get('is_complete_run', 'N/A')}",
        "",
    ]


def _section_data_availability(facts, cutoff_date):
    if not facts:
        return ["## §2 数据可用性\n\n(no data)\n"]
    keys = ["project_state_json", "transcript_json",
            "metering_usage_events", "audit_user_edit_events",
            "subtitle_quality_report", "subtitle_cues"]
    pre = [f for f in facts if f.get("created_at", "") < cutoff_date]
    post = [f for f in facts if f.get("created_at", "") >= cutoff_date]
    lines = ["## §2 数据可用性\n"]
    for label, group in [(f"pre {cutoff_date} (N={len(pre)})", pre),
                          (f"post {cutoff_date} (N={len(post)})", post)]:
        lines.append(f"### {label}")
        for k in keys:
            present = sum(1 for f in group if (f.get("artifact_presence") or {}).get(k))
            pct = (present / len(group) * 100) if group else 0
            lines.append(f"- {k}: {present}/{len(group)} ({pct:.0f}%)")
        lines.append("")
    return lines


def _section_speaker_count(facts, threshold_set):
    if not facts:
        return ["## §3 Speaker 数分布\n\n(no data)\n"]
    thresholds = [t.strip() for t in threshold_set.split(",")]
    lines = ["## §3 Speaker 数分布\n",
             "| Threshold | Main ≤ 3 占比 | Main ≤ 2 | Main ≤ 1 |",
             "|---|---|---|---|"]
    for t in thresholds:
        counts = []
        for f in facts:
            sct = (f.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
            c = sct.get(t)
            if isinstance(c, int):
                counts.append(c)
        if not counts:
            lines.append(f"| {t} | (no data) | - | - |")
            continue
        leq3 = sum(1 for c in counts if c <= 3)
        leq2 = sum(1 for c in counts if c <= 2)
        leq1 = sum(1 for c in counts if c <= 1)
        n = len(counts)
        lines.append(f"| {t} | {leq3}/{n} ({leq3/n*100:.0f}%) | {leq2}/{n} ({leq2/n*100:.0f}%) | {leq1}/{n} ({leq1/n*100:.0f}%) |")
    lines.append("")
    return lines


def _section_clone_availability(facts):
    """§4: For each main_speaker_count bucket (using threshold=0.10),
    show what % of jobs have ALL speakers with ≥1 eligible sample ≥5s."""
    if not facts:
        return ["## §4 克隆样本可用率\n\n(no data)\n"]
    by_main_count = defaultdict(list)
    for f in facts:
        sct = (f.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
        main_count = sct.get("0.10")
        css = f.get("clone_sample_stats")
        if isinstance(main_count, int) and css:
            by_main_count[main_count].append(css)
    if not by_main_count:
        return ["## §4 克隆样本可用率\n\n(no clone_sample_stats data)\n"]
    lines = ["## §4 克隆样本可用率",
             "",
             "按 main_speaker_count (threshold=0.10) 分桶，每桶里所有主 speaker 都有 ≥1 个 ≥5s 合格样本的 job 占比：",
             "",
             "| main_count | jobs | all-eligible (≥5s) | all-eligible (≥8s) |",
             "|---|---|---|---|"]
    for mc in sorted(by_main_count.keys()):
        jobs = by_main_count[mc]
        all_5 = sum(
            1 for css in jobs
            if all(b.get("≥5s", 0) >= 1 for b in
                   (css.get("eligible_sample_count_buckets_by_speaker") or [])[:mc])
        )
        all_8 = sum(
            1 for css in jobs
            if all(b.get("≥8s", 0) >= 1 for b in
                   (css.get("eligible_sample_count_buckets_by_speaker") or [])[:mc])
        )
        n = len(jobs)
        lines.append(
            f"| main={mc} | {n} | {all_5}/{n} ({all_5/n*100:.0f}%) | "
            f"{all_8}/{n} ({all_8/n*100:.0f}%) |"
        )
    lines.append("")
    return lines


def _section_retry_distribution(facts):
    """§5: rewrite/retts distribution split by metering vs fallback.

    Includes rewrite_input_text_chars_total p50/p90/p99 — same denominator
    used by §8 cost (G5), so owner can reconcile §5 retry volume with §8 cost.
    """
    if not facts:
        return ["## §5 Retry 分布\n\n(no data)\n"]
    metering = [f for f in facts
                if (f.get("retry_stats") or {}).get("_data_source") == "metering"]
    fallback = [f for f in facts
                if (f.get("retry_stats") or {}).get("_data_source", "").startswith("fallback")]
    lines = ["## §5 Retry 分布", "",
             f"- jobs with metering data: {len(metering)}",
             f"- jobs with fallback data: {len(fallback)}",
             ""]
    if metering:
        rwc = sorted((f["retry_stats"]["rewrite_count"] or 0) for f in metering)
        rtc = sorted((f["retry_stats"]["retts_count"] or 0) for f in metering)
        rtd = sorted((f["retry_stats"]["retts_total_duration_ms"] or 0) for f in metering)
        rwch = sorted(
            (f.get("usage_meter") or {}).get("rewrite_input_text_chars_total") or 0
            for f in metering
        )
        ratios = sorted(
            (f["retry_stats"].get("retts_total_duration_ms") or 0) / 1000.0 /
            max(1, f.get("duration_seconds") or 1)
            for f in metering
        )
        lines += [
            "### Metering subset",
            "",
            "| Metric | p50 | p90 | p99 |",
            "|---|---|---|---|",
            f"| rewrite_count | {_percentile(rwc, 0.5)} | {_percentile(rwc, 0.9)} | {_percentile(rwc, 0.99)} |",
            f"| rewrite_input_text_chars_total | {_percentile(rwch, 0.5)} | {_percentile(rwch, 0.9)} | {_percentile(rwch, 0.99)} |",
            f"| retts_count | {_percentile(rtc, 0.5)} | {_percentile(rtc, 0.9)} | {_percentile(rtc, 0.99)} |",
            f"| retts_audio_ms | {_percentile(rtd, 0.5)} | {_percentile(rtd, 0.9)} | {_percentile(rtd, 0.99)} |",
            f"| retts_audio/src ratio | {_percentile(ratios, 0.5):.3f} | {_percentile(ratios, 0.9):.3f} | {_percentile(ratios, 0.99):.3f} |",
            "",
            "> `rewrite_input_text_chars_total` 是 §8 cost 公式 `rewrite_rmb` 项的输入分母，"
            "owner 可用此列与 §8 cost 数据对账。",
            "",
        ]
    if fallback:
        rwc = sorted((f["retry_stats"]["rewrite_count"] or 0) for f in fallback)
        lines += [
            "### Fallback subset (editor.segments rewrite_count only)",
            "",
            "| Metric | p50 | p90 | p99 |",
            "|---|---|---|---|",
            f"| rewrite_count | {_percentile(rwc, 0.5)} | {_percentile(rwc, 0.9)} | {_percentile(rwc, 0.99)} |",
            "",
            "> retts_count 在 fallback 路径 N/A（旧 job 无 metering）",
            "",
        ]
    return lines


def build_arg_parser():
    p = argparse.ArgumentParser(description="Smart shadow eval analyzer")
    p.add_argument("--facts", required=True)
    p.add_argument("--inventory", required=False)
    p.add_argument("--summary", required=False)
    p.add_argument("--pricing-runtime-snapshot", required=False)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--phase-cutoff-date", default="2026-05-05")
    p.add_argument("--smart-eligibility-threshold-set", default="0.05,0.10,0.15,0.20")
    p.add_argument("--min-sample-seconds-set", default="5,8,10,15")
    p.add_argument("--allow-incomplete-run", action="store_true")
    p.add_argument("--expected-schema-version", type=int, default=SCHEMA_VERSION)
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    facts_path = Path(args.facts)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Gate: summary.is_complete_run + schema_version
    # schema_version default MUST be a sentinel that's never equal to
    # expected_schema_version, so missing field is treated as explicit reject
    # (not silent passthrough).
    _MISSING = object()
    summary = None
    if args.summary:
        try:
            summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read summary.json: {exc}", file=sys.stderr)
            return 2
        if not args.allow_incomplete_run and not summary.get("is_complete_run", True):
            print("ERROR: summary.is_complete_run=false; "
                  "pass --allow-incomplete-run to override",
                  file=sys.stderr)
            return 2
        sv = summary.get("schema_version", _MISSING)
        if sv is _MISSING:
            print("ERROR: summary missing schema_version field; "
                  "produced by an unsupported collector version",
                  file=sys.stderr)
            return 2
        if sv != args.expected_schema_version:
            print(f"ERROR: summary schema_version={sv} != expected="
                  f"{args.expected_schema_version}",
                  file=sys.stderr)
            return 2

    # Load facts
    facts = []
    if facts_path.is_file():
        for line in facts_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # PLACEMENT CONTRACT for subsequent tasks (G2.2, G3a/b/c, G4a/b/c, G5):
    # All `report_lines += _section_*(facts)` and `summary_extra.update(...)`
    # MUST be inserted ABOVE the `summary_payload = {...}` and `write_text`
    # calls below. Placing them after = silent loss (dict snapshot at unpack
    # time / report.md already written).
    # ─────────────────────────────────────────────────────────────────────

    # Generate skeleton report (Phase G1: only metadata)
    # summary_extra accumulates fields written by later sections (e.g., §10 threshold_matrix)
    summary_extra: dict = {}
    report_lines = [
        "# Smart Shadow Evaluator Report",
        "",
        f"- Facts loaded: {len(facts)}",
        f"- Out dir: {out_dir}",
    ]
    if not facts:
        report_lines.append("")
        report_lines.append("⚠️ No facts available — empty dump or no jobs in date range.")

    # ↓↓↓ Subsequent tasks insert their section calls HERE ↓↓↓
    # (G2.2 inserts §1+§2+§3, G3a inserts §4, G3b inserts §5, etc.)
    report_lines += _section_metadata(facts, summary, args)
    report_lines += _section_data_availability(facts, args.phase_cutoff_date)
    report_lines += _section_speaker_count(facts, args.smart_eligibility_threshold_set)
    report_lines += _section_clone_availability(facts)
    report_lines += _section_retry_distribution(facts)
    # ↑↑↑ All section calls MUST be above the writes below ↑↑↑

    (out_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")

    # report_summary.json payload — sections accumulate fields into summary_extra
    summary_payload = {
        "facts_count": len(facts),
        **summary_extra,
    }
    (out_dir / "report_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

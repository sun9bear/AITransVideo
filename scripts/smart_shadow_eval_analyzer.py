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

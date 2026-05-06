"""Smart Shadow Simulator Aggregator (P1) — read multiple per-job sidecars,
emit cross-job aggregate report. Read-only, offline, stdlib-only.

Quick usage:
  python scripts/smart_shadow_sim_aggregator.py \
    --simulator-out-dir D:/Claude/temp/smart_shadow_sim/local_smoke \
    --out-dir D:/Claude/temp/smart_shadow_sim/local_smoke

See docs/plans/2026-05-06-smart-shadow-sim-design.md.
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


def _load_per_job_reports(simulator_out_dir: Path) -> list[dict]:
    """Glob <simulator_out_dir>/<job_id>/smart_shadow_report.json."""
    if not simulator_out_dir.is_dir():
        return []
    reports = []
    for child in sorted(simulator_out_dir.iterdir()):
        if not child.is_dir():
            continue
        report_path = child / "smart_shadow_report.json"
        if not report_path.is_file():
            continue
        try:
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return reports


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow simulator aggregator (P1, read-only)."
    )
    parser.add_argument("--simulator-out-dir", required=True,
                        help="Path to dir containing per-job <job_id>/smart_shadow_report.json files.")
    parser.add_argument("--projects-root", required=False,
                        help="Optional. Project artifacts root for cross-job stats.")
    parser.add_argument("--out-dir", required=True,
                        help="Aggregator output dir for aggregate_report.json.")
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

    reports = _load_per_job_reports(sim_out)

    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _make_run_id(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "simulator_out_dir": str(sim_out),
        "jobs_simulated": len(reports),
        "warnings": [],
        # Phase C will populate the rest:
        # smart_eligibility_breakdown, stage_decision_match_rate,
        # voice_selection_diff, translation_review_diff, etc.
    }
    if not reports:
        aggregate["warnings"].append(
            "No per-job reports found in simulator-out-dir — nothing to aggregate."
        )

    (out_dir / "aggregate_report.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

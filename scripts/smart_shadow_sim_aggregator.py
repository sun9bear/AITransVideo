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
import json
import sys
from pathlib import Path


SCHEMA_VERSION = 1


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
    # P-A2: skeleton only — full logic in subsequent tasks
    return 0


if __name__ == "__main__":
    sys.exit(main())

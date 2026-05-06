"""Smart Shadow Simulator (P1) — read fact sheets + Studio artifacts (read-only),
emit per-job decisions + report. NO production lifecycle hooks. NO paid API calls.

Quick usage:
  python scripts/smart_shadow_sim_simulator.py \
    --facts D:/Claude/temp/smart_shadow_eval/prod_full/facts.jsonl \
    --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \
    --out-dir D:/Claude/temp/smart_shadow_sim/local_smoke \
    --limit 3

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
        description="Smart shadow simulator (P1, read-only, offline)."
    )
    parser.add_argument("--facts", required=True,
                        help="Path to facts.jsonl produced by P0 evaluator collector.")
    parser.add_argument("--projects-root", required=False,
                        help="Optional. Project artifacts root (read-only).")
    parser.add_argument("--out-dir", required=True,
                        help="Simulator output dir. Per-job sidecars go under <out-dir>/<job_id>/.")
    parser.add_argument("--main-speaker-threshold", type=float, default=0.10)
    parser.add_argument("--clone-min-seconds-soft", type=int, default=8)
    parser.add_argument("--clone-min-seconds-preferred", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional. Only simulate first N facts (for smoke).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # P-A1: skeleton only — full logic in subsequent tasks
    return 0


if __name__ == "__main__":
    sys.exit(main())

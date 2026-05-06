"""Smart Shadow Evaluator collector — stdlib-only read-only scanner.

Quick usage:
  # Local smoke (against .codex_tmp samples):
  python scripts/smart_shadow_eval_collector.py \\
    --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \\
    --jobs-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \\
    --out-dir D:/Claude/temp/smart_shadow_eval/local_smoke --limit 3

  # Production (on 154 host):
  python3 scripts/smart_shadow_eval_collector.py \\
    --projects-root /opt/aivideotrans/data/projects \\
    --jobs-root /opt/aivideotrans/data/jobs \\
    --out-dir /tmp/smart_shadow_eval/<run_id>

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
import argparse
import os
import sys
from pathlib import Path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow eval collector (read-only)."
    )
    parser.add_argument(
        "--projects-root",
        default=os.environ.get(
            "AIVIDEOTRANS_PROJECTS_DIR",
            "/opt/aivideotrans/data/projects",
        ),
    )
    parser.add_argument(
        "--jobs-root",
        default=os.environ.get(
            "AIVIDEOTRANS_JOBS_DIR",
            "/opt/aivideotrans/data/jobs",
        ),
    )
    parser.add_argument("--out-dir", required=False)
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-running", action="store_true")
    parser.add_argument("--scan-from", choices=["jobs", "projects"], default="jobs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # P-A1: skeleton only — full logic in subsequent tasks
    return 0


if __name__ == "__main__":
    sys.exit(main())

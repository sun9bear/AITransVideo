#!/usr/bin/env python
"""Build a local Phase 1b job-report analysis table.

Examples:
  python scripts/phase1b_report_summary.py --project-root data/projects
  python scripts/phase1b_report_summary.py --project-dir data/projects/job_123 --json-out reports/phase1b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from services.phase1b_report_summary import (  # noqa: E402
    build_phase1b_csv,
    build_phase1b_summary,
    discover_project_dirs,
    summarize_project_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate translation/speaker/subtitle/voice report sidecars.",
    )
    parser.add_argument(
        "--project-root",
        default="data/projects",
        help="Root whose immediate children are project directories.",
    )
    parser.add_argument(
        "--project-dir",
        action="append",
        default=[],
        help="Specific project directory to include. Can be repeated.",
    )
    parser.add_argument("--json-out", help="Optional output JSON path.")
    parser.add_argument("--csv-out", help="Optional output CSV path.")
    args = parser.parse_args()

    project_dirs = [Path(value) for value in args.project_dir]
    if not project_dirs:
        project_dirs = discover_project_dirs(Path(args.project_root))

    rows = []
    for project_dir in project_dirs:
        report = summarize_project_reports(project_dir, job_id=project_dir.name)
        rows.append({
            "job_id": project_dir.name,
            "user_id": "",
            "user_email": None,
            "display_name": project_dir.name,
            "status": "",
            "service_mode": "",
            "created_at": None,
            "project_dir_name": report.get("project_dir_name"),
            "reports": {
                "translation_quality": report["translation_quality"],
                "subtitle_width": report["subtitle_width"],
                "speaker_evidence": report["speaker_evidence"],
                "voice_sample_scoring": report["voice_sample_scoring"],
            },
            "cost_view_url": "",
        })

    payload = build_phase1b_summary(rows)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)

    if args.csv_out:
        out = Path(args.csv_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(build_phase1b_csv(rows))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

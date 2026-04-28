#!/usr/bin/env python3
"""Generate the baseline quality and cost report for the benchmark fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.quality_dataset import DEFAULT_OUTPUT_DIR, build_baseline_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/benchmark/video_translation_quality/latest"),
    )
    parser.add_argument("--llm-rewrite-cost-cny", type=float, default=0.0003)
    parser.add_argument("--tts-rewrite-cost-low-cny", type=float, default=0.02)
    parser.add_argument("--tts-rewrite-cost-high-cny", type=float, default=0.07)
    parser.add_argument("--manual-speaker-fix-cost-cny", type=float, default=3.0)
    args = parser.parse_args()

    report = build_baseline_report(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
        llm_rewrite_cost_cny=args.llm_rewrite_cost_cny,
        tts_rewrite_cost_low_cny=args.tts_rewrite_cost_low_cny,
        tts_rewrite_cost_high_cny=args.tts_rewrite_cost_high_cny,
        manual_speaker_fix_cost_cny=args.manual_speaker_fix_cost_cny,
    )
    print(
        json.dumps(
            {
                "output_dir": args.output_dir.as_posix(),
                "jobs": report["dataset"]["jobs"],
                "segments": report["quality_baseline"]["segments"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the sanitized video translation quality benchmark fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.quality_dataset import (
    DEFAULT_ANALYSIS_DIR,
    DEFAULT_ARTIFACTS_ROOT,
    DEFAULT_OUTPUT_DIR,
    BuildPaths,
    build_quality_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", type=Path, default=DEFAULT_ANALYSIS_DIR)
    parser.add_argument("--artifacts-root", type=Path, default=DEFAULT_ARTIFACTS_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-jobs", type=int, default=12)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    manifest = build_quality_dataset(
        paths=BuildPaths(
            analysis_dir=args.analysis_dir,
            artifacts_root=args.artifacts_root,
            output_dir=args.output_dir,
        ),
        max_jobs=args.max_jobs,
        force=args.force,
    )
    print(json.dumps({"output_dir": args.output_dir.as_posix(), "jobs": len(manifest["jobs"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

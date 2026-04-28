#!/usr/bin/env python3
"""Validate the sanitized video translation quality benchmark fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.quality_dataset import DEFAULT_OUTPUT_DIR, validate_quality_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_dir", nargs="?", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    summary = validate_quality_dataset(args.dataset_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

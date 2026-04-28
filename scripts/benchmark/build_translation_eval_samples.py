#!/usr/bin/env python3
"""CLI wrapper for building real-prompt translation evaluation samples."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.translation_eval_samples import main


if __name__ == "__main__":
    raise SystemExit(main())

"""Manifest construction + serialization for pan backup tar.gz.

Plan 2026-05-13 §4.4. Manifest stored in TWO places (redundancy):
- PG `backup_records.manifest_json` JSONB
- tar.gz first entry `manifest.json` (self-describing — PG can be lost)
"""
from __future__ import annotations

import hashlib
import io
import json
import socket
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def walk_project_dir_inventory(project_dir: Path) -> list[dict]:
    """For each file under project_dir (recursive), compute relative path +
    size + sha256.

    Order: lexicographic by relative path (sorted rglob output). Directories
    are skipped — only regular files appear in the inventory. sha256 is
    streamed in 1MB chunks so large files do not load fully into RAM.
    """
    inventory = []
    for f in sorted(project_dir.rglob('*')):
        if not f.is_file():
            continue
        rel = f.relative_to(project_dir).as_posix()
        sha = hashlib.sha256()
        with f.open('rb') as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b''):
                sha.update(chunk)
        inventory.append({
            'path': rel,
            'size': f.stat().st_size,
            'sha256': sha.hexdigest(),
        })
    return inventory

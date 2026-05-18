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


def build_manifest(*, project_dir: Path, job_record: dict, r2_artifacts: list[dict]) -> dict:
    """Assemble the full manifest dict per plan §4.4.

    Fields:
      - backup_format_version: int, bump when wire format changes
      - created_at_utc: ISO-8601 with explicit +00:00 offset
      - source_host: socket.gethostname() — useful for triage
      - job_record: serialized JobRecord snapshot at archive time
      - r2_artifacts_snapshot: list of R2 artifact rows for this job
      - file_inventory: walk_project_dir_inventory(project_dir) output

    The returned dict is what gets persisted to PG `backup_records.manifest_json`
    AND embedded as `manifest.json` inside the tar.gz (redundant by design).
    """
    return {
        'backup_format_version': 1,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_host': socket.gethostname(),
        'job_record': job_record,
        'r2_artifacts_snapshot': list(r2_artifacts),
        'file_inventory': walk_project_dir_inventory(project_dir),
    }


def write_tar_with_manifest(tar_path: Path, manifest: dict, project_dir: Path) -> None:
    """Stream tar.gz with manifest.json as FIRST entry + project_dir contents.

    Writing manifest first lets the restore path peek at metadata via
    `read_manifest_from_tar` without fully extracting — useful when the
    tar is large or possibly corrupt past the header.

    Uses 'w:gz' streaming mode so RAM stays bounded regardless of project
    size. project_dir contents are stored under arcname=project_dir.name
    (typically the job_id), keeping a clean root inside the archive.
    """
    with tarfile.open(tar_path, 'w:gz') as tf:
        # 1. manifest first
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        info = tarfile.TarInfo(name='manifest.json')
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tf.addfile(info, io.BytesIO(manifest_bytes))

        # 2. project_dir contents (recursive, arcname keeps a clean root)
        tf.add(project_dir, arcname=project_dir.name)

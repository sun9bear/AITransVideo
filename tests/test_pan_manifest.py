"""Tests for gateway.pan.manifest helpers.

Plan 2026-05-14 Phase 4 — file inventory + manifest build + tar serialization.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest


# --- T4.1: walk_project_dir_inventory ---


def test_walk_project_dir_inventory(tmp_path: Path):
    """Walk a fake project_dir, build {path, size, sha256} for each file."""
    project = tmp_path / 'job_xyz'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text('{"key": "value"}')
    (project / 'tts').mkdir()
    (project / 'tts' / 'seg_0.wav').write_bytes(b'\x00' * 1024)

    from gateway.pan.manifest import walk_project_dir_inventory
    inventory = walk_project_dir_inventory(project)

    paths = sorted(item['path'] for item in inventory)
    assert paths == ['transcript/review.json', 'tts/seg_0.wav']

    review_item = next(i for i in inventory if i['path'] == 'transcript/review.json')
    assert review_item['size'] == len('{"key": "value"}')
    assert review_item['sha256'] == hashlib.sha256(b'{"key": "value"}').hexdigest()


def test_inventory_skips_empty_dirs_and_uses_posix_separators(tmp_path: Path):
    """Empty directories are skipped; output paths use forward slashes
    even on Windows."""
    project = tmp_path / 'job_a'
    (project / 'empty_dir').mkdir(parents=True)
    (project / 'sub' / 'deep').mkdir(parents=True)
    (project / 'sub' / 'deep' / 'leaf.bin').write_bytes(b'leaf')

    from gateway.pan.manifest import walk_project_dir_inventory
    inventory = walk_project_dir_inventory(project)

    assert len(inventory) == 1
    # POSIX separator even on Windows
    assert inventory[0]['path'] == 'sub/deep/leaf.bin'


def test_inventory_is_sorted_for_determinism(tmp_path: Path):
    """Inventory entries must be in lexicographic order so manifest is
    reproducible across hosts."""
    project = tmp_path / 'job_b'
    project.mkdir()
    (project / 'z_last.txt').write_text('z')
    (project / 'a_first.txt').write_text('a')
    (project / 'm_middle.txt').write_text('m')

    from gateway.pan.manifest import walk_project_dir_inventory
    inventory = walk_project_dir_inventory(project)
    paths = [i['path'] for i in inventory]
    assert paths == sorted(paths)
    assert paths == ['a_first.txt', 'm_middle.txt', 'z_last.txt']


def test_inventory_sha256_streamed_for_large_file(tmp_path: Path):
    """sha256 must match hashlib.sha256(payload) even for >1MB file
    (covers the streaming loop)."""
    project = tmp_path / 'job_c'
    project.mkdir()
    payload = b'X' * (3 * 1024 * 1024 + 7)  # 3MB + 7 bytes, crosses chunk boundary
    (project / 'big.bin').write_bytes(payload)

    from gateway.pan.manifest import walk_project_dir_inventory
    inventory = walk_project_dir_inventory(project)

    assert len(inventory) == 1
    assert inventory[0]['size'] == len(payload)
    assert inventory[0]['sha256'] == hashlib.sha256(payload).hexdigest()

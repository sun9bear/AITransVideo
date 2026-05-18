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


# --- T4.2: build_manifest ---


def test_build_manifest_includes_all_required_fields(tmp_path: Path):
    project = tmp_path / 'job_abc'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text('{}')

    from gateway.pan.manifest import build_manifest
    job_record_snapshot = {'job_id': 'job_abc', 'status': 'archiving', 'edit_generation': 0}
    r2_artifacts = [{
        'artifact_key': 'publish.dubbed_video',
        'edit_generation': 0,
        'state': 'pushed',
        'r2_key': 'jobs/job_abc/publish.dubbed_video.mp4',
    }]

    m = build_manifest(project_dir=project, job_record=job_record_snapshot,
                       r2_artifacts=r2_artifacts)

    assert m['backup_format_version'] == 1
    assert m['created_at_utc'].endswith('+00:00')
    assert m['source_host']  # populated
    assert m['job_record']['job_id'] == 'job_abc'
    assert m['r2_artifacts_snapshot'] == r2_artifacts
    assert len(m['file_inventory']) == 1
    assert m['file_inventory'][0]['path'] == 'transcript/review.json'


def test_build_manifest_r2_artifacts_is_a_copy_not_alias(tmp_path: Path):
    """Caller modifying the input list after build_manifest must not mutate
    the persisted manifest snapshot."""
    project = tmp_path / 'job_d'
    project.mkdir()
    (project / 'noop.txt').write_text('x')

    from gateway.pan.manifest import build_manifest
    r2 = [{'artifact_key': 'a', 'r2_key': 'k1'}]
    m = build_manifest(project_dir=project, job_record={'job_id': 'job_d'}, r2_artifacts=r2)
    r2.append({'artifact_key': 'b', 'r2_key': 'k2'})
    # 内嵌 snapshot 不受外部 mutation 影响
    assert len(m['r2_artifacts_snapshot']) == 1


def test_build_manifest_empty_project_dir(tmp_path: Path):
    """Empty project_dir yields empty file_inventory but full manifest shape."""
    project = tmp_path / 'job_empty'
    project.mkdir()

    from gateway.pan.manifest import build_manifest
    m = build_manifest(project_dir=project, job_record={'job_id': 'job_empty'},
                       r2_artifacts=[])

    assert m['file_inventory'] == []
    assert m['r2_artifacts_snapshot'] == []
    assert m['backup_format_version'] == 1
    assert m['source_host']


def test_build_manifest_created_at_is_recent_utc(tmp_path: Path):
    """created_at_utc must be parseable and within ~5s of now."""
    import datetime as dt
    project = tmp_path / 'job_t'
    project.mkdir()

    from gateway.pan.manifest import build_manifest
    m = build_manifest(project_dir=project, job_record={}, r2_artifacts=[])

    parsed = dt.datetime.fromisoformat(m['created_at_utc'])
    assert parsed.tzinfo is not None
    now = dt.datetime.now(dt.timezone.utc)
    assert abs((now - parsed).total_seconds()) < 5


# --- T4.3: write_tar_with_manifest ---


def test_write_tar_with_manifest_first_entry(tmp_path: Path):
    """Manifest must be the FIRST tar entry — restore reads it before extraction."""
    tar_path = tmp_path / 'backup.tar.gz'
    manifest = {'backup_format_version': 1, 'created_at_utc': '2026-05-14T00:00:00+00:00'}
    project = tmp_path / 'job_xyz'
    project.mkdir()
    (project / 'a.txt').write_text('hello')

    from gateway.pan.manifest import write_tar_with_manifest
    write_tar_with_manifest(tar_path, manifest, project)

    with tarfile.open(tar_path, 'r:gz') as tf:
        names = tf.getnames()
        assert names[0] == 'manifest.json'  # 第一条
        assert any(n.endswith('a.txt') for n in names)

        first = tf.extractfile('manifest.json').read()
        assert json.loads(first.decode()) == manifest


def test_write_tar_preserves_project_dir_layout(tmp_path: Path):
    """project_dir is stored under arcname=project_dir.name so the archive
    has a clean root (e.g. job_xyz/transcript/foo.json)."""
    tar_path = tmp_path / 'b.tar.gz'
    project = tmp_path / 'job_layout'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'foo.json').write_text('{}')
    (project / 'tts').mkdir()
    (project / 'tts' / 'a.wav').write_bytes(b'\x00\x01')

    from gateway.pan.manifest import write_tar_with_manifest
    write_tar_with_manifest(tar_path, {'backup_format_version': 1}, project)

    with tarfile.open(tar_path, 'r:gz') as tf:
        names = set(tf.getnames())

    # job_layout root + the two files (paths normalized by tar to use /)
    assert 'manifest.json' in names
    assert 'job_layout/transcript/foo.json' in names
    assert 'job_layout/tts/a.wav' in names


def test_write_tar_handles_unicode_in_manifest(tmp_path: Path):
    """Manifest with non-ASCII bytes (中文 / emoji) round-trips correctly."""
    tar_path = tmp_path / 'u.tar.gz'
    project = tmp_path / 'job_u'
    project.mkdir()
    (project / 'x.txt').write_text('x')

    manifest = {
        'backup_format_version': 1,
        'job_record': {'job_id': 'job_u', 'note': '配音任务 ✨'},
    }
    from gateway.pan.manifest import write_tar_with_manifest
    write_tar_with_manifest(tar_path, manifest, project)

    with tarfile.open(tar_path, 'r:gz') as tf:
        first = tf.extractfile('manifest.json').read()
        decoded = json.loads(first.decode('utf-8'))
    assert decoded['job_record']['note'] == '配音任务 ✨'

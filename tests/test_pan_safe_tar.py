"""Tests for gateway.pan.manifest.safe_extract_tar.

Plan 2026-05-14 §T5.11.5 — restore-time tar extractor that rejects
unsafe entries BEFORE any byte is written. CodeX Q-B critical addition.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest


def _make_tar_with_member(
    tar_path: Path,
    name: str,
    *,
    content: bytes = b'x',
    is_symlink: bool = False,
    is_hardlink: bool = False,
    link_target: str = '',
) -> None:
    """Build a tar containing one custom member to probe extractor rejection."""
    with tarfile.open(tar_path, 'w:gz') as tf:
        info = tarfile.TarInfo(name=name)
        if is_symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = link_target
            tf.addfile(info)
        elif is_hardlink:
            info.type = tarfile.LNKTYPE
            info.linkname = link_target
            tf.addfile(info)
        else:
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))


def test_safe_extract_rejects_dotdot_path(tmp_path: Path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(tar, '../etc/passwd', content=b'evil')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match=r'unsafe.*\.\.'):
        safe_extract_tar(tar, dest)
    # 没有任何文件被解
    assert not (tmp_path / 'etc' / 'passwd').exists()
    assert not (dest / '..' / 'etc' / 'passwd').exists()


def test_safe_extract_rejects_deep_dotdot_in_middle(tmp_path: Path):
    """`a/../../b` traversal — `..` in middle of path is still unsafe."""
    tar = tmp_path / 'mal.tar.gz'
    _make_tar_with_member(tar, 'a/../../escaped.bin', content=b'evil')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match=r'unsafe.*\.\.'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_absolute_path(tmp_path: Path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(tar, '/etc/passwd', content=b'evil')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*absolute'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_symlink(tmp_path: Path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(
        tar, 'link_to_root', is_symlink=True, link_target='/etc/passwd'
    )
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*symlink'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_hardlink(tmp_path: Path):
    """Hardlink (LNKTYPE) entries are rejected alongside symlinks."""
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(
        tar, 'hard_link', is_hardlink=True, link_target='/etc/passwd'
    )
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*(symlink|hardlink)'):
        safe_extract_tar(tar, dest)


def test_safe_extract_allows_normal_files(tmp_path: Path):
    tar = tmp_path / 'good.tar.gz'
    _make_tar_with_member(tar, 'transcript/seg.json', content=b'{}')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    safe_extract_tar(tar, dest)
    assert (dest / 'transcript' / 'seg.json').read_bytes() == b'{}'


def test_safe_extract_allows_nested_directory_layout(tmp_path: Path):
    """The realistic backup tar layout (manifest.json + job_xyz/*) extracts cleanly."""
    from gateway.pan.manifest import (
        build_manifest,
        write_tar_with_manifest,
        safe_extract_tar,
    )

    project = tmp_path / 'job_real'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text('{"k": 1}')
    (project / 'tts').mkdir()
    (project / 'tts' / 'seg_0.wav').write_bytes(b'\x00\x01\x02')

    manifest = build_manifest(project_dir=project, job_record={}, r2_artifacts=[])
    tar_path = tmp_path / 'real.tar.gz'
    write_tar_with_manifest(tar_path, manifest, project)

    dest = tmp_path / 'restored'
    safe_extract_tar(tar_path, dest)

    # Manifest extracted at top.
    assert (dest / 'manifest.json').exists()
    # Project_dir contents extracted under its name (path contract from P2).
    assert (dest / 'job_real' / 'transcript' / 'review.json').read_text() == '{"k": 1}'
    assert (dest / 'job_real' / 'tts' / 'seg_0.wav').read_bytes() == b'\x00\x01\x02'


def test_safe_extract_aborts_before_any_byte_written(tmp_path: Path):
    """If ANY member is unsafe, extraction must abort BEFORE extracting other
    safe members in the same tar. (Pre-extraction validation, not per-member.)"""
    tar = tmp_path / 'mixed.tar.gz'
    with tarfile.open(tar, 'w:gz') as tf:
        # First a safe file
        safe_info = tarfile.TarInfo(name='ok/safe.txt')
        safe_info.size = 3
        tf.addfile(safe_info, io.BytesIO(b'hi!'))
        # Then a malicious one
        mal_info = tarfile.TarInfo(name='../escape.bin')
        mal_info.size = 4
        tf.addfile(mal_info, io.BytesIO(b'evil'))

    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match=r'unsafe.*\.\.'):
        safe_extract_tar(tar, dest)

    # The safe member must NOT have leaked out — validation pre-empts extraction.
    assert not (dest / 'ok' / 'safe.txt').exists()


def test_safe_extract_creates_dest_if_missing(tmp_path: Path):
    """If dest doesn't exist yet, safe_extract_tar creates it (parents too)."""
    tar = tmp_path / 'g.tar.gz'
    _make_tar_with_member(tar, 'inner/x.txt', content=b'x')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'newly' / 'made' / 'dest'
    safe_extract_tar(tar, dest)
    assert (dest / 'inner' / 'x.txt').read_bytes() == b'x'

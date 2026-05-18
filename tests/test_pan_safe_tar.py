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
    type_override: bytes | None = None,
) -> None:
    """Build a tar containing one custom member to probe extractor rejection.

    `type_override` (e.g. tarfile.CHRTYPE / BLKTYPE / FIFOTYPE) lets the
    test fixture insert non-file/non-dir entries that real-world tars
    could carry. devmajor / devminor default to 0 for char/block tests."""
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
        elif type_override is not None:
            info.type = type_override
            if type_override in (tarfile.CHRTYPE, tarfile.BLKTYPE):
                info.devmajor = 1
                info.devminor = 3
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


# --- CodeX P1: allowlist (only isfile() / isdir()) — reject special types ---


def test_safe_extract_rejects_character_device(tmp_path: Path):
    """CHRTYPE entries (character device nodes) must be rejected by the
    isfile()/isdir() allowlist — CodeX P1."""
    tar = tmp_path / 'chr.tar.gz'
    _make_tar_with_member(tar, 'evil_char_dev', type_override=tarfile.CHRTYPE)
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe entry type'):
        safe_extract_tar(tar, dest)
    assert not (dest / 'evil_char_dev').exists()


def test_safe_extract_rejects_block_device(tmp_path: Path):
    """BLKTYPE entries (block device nodes) — CodeX P1."""
    tar = tmp_path / 'blk.tar.gz'
    _make_tar_with_member(tar, 'evil_block_dev', type_override=tarfile.BLKTYPE)
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe entry type'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_fifo(tmp_path: Path):
    """FIFOTYPE entries (named pipes) — CodeX P1."""
    tar = tmp_path / 'fifo.tar.gz'
    _make_tar_with_member(tar, 'evil_fifo', type_override=tarfile.FIFOTYPE)
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe entry type'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_contiguous_file(tmp_path: Path):
    """CONTTYPE (contiguous file, Solaris/old-style) — allowlist catches
    even this rare type. Future tar types we haven't enumerated are also
    blocked by the same isfile()/isdir() check."""
    tar = tmp_path / 'cont.tar.gz'
    _make_tar_with_member(tar, 'cont_file', type_override=tarfile.CONTTYPE)
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe entry type'):
        safe_extract_tar(tar, dest)


def test_safe_extract_allows_explicit_directory_entry(tmp_path: Path):
    """DIRTYPE entries (explicit directory members) ARE allowed — they're
    in the allowlist alongside regular files. write_tar_with_manifest
    produces them when project_dir has subdirs."""
    tar = tmp_path / 'dir.tar.gz'
    with tarfile.open(tar, 'w:gz') as tf:
        # Explicit DIRTYPE entry
        dir_info = tarfile.TarInfo(name='somedir')
        dir_info.type = tarfile.DIRTYPE
        tf.addfile(dir_info)
        # A file inside it
        file_info = tarfile.TarInfo(name='somedir/file.txt')
        file_info.size = 4
        tf.addfile(file_info, io.BytesIO(b'data'))

    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    safe_extract_tar(tar, dest)
    assert (dest / 'somedir' / 'file.txt').read_bytes() == b'data'

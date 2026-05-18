"""Manifest construction + serialization for pan backup tar.gz.

Plan 2026-05-13 §4.4. Manifest stored in TWO places (redundancy):
- PG `backup_records.manifest_json` JSONB
- tar.gz first entry `manifest.json` (self-describing — PG can be lost)
"""
from __future__ import annotations

import copy
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

    Symbolic links are REJECTED with RuntimeError. The backup/restore
    contract requires regular files only: if inventory followed a symlink
    and hashed its target, but write_tar_with_manifest stored the symlink
    AS a symlink entry, the three commit-point gates would still pass —
    yet restore would refuse the link entry, leaving the user with no
    way to recover after the local copy is purged. Reject early so the
    archive operator gets a loud failure instead of silent data loss.
    """
    inventory = []
    for f in sorted(project_dir.rglob('*')):
        # Reject BEFORE is_file() (which follows symlinks). Path.is_symlink()
        # returns True for the link itself, not the target.
        if f.is_symlink():
            rel = f.relative_to(project_dir).as_posix()
            raise RuntimeError(
                f"Refusing to inventory symlink: {rel}. "
                f"Backup requires regular files only "
                f"(symlink would be archived as link entry but inventory "
                f"would record target content — restore would fail)."
            )
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


def _reject_link_entries(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """tarfile.add() filter: refuse symbolic and hard link entries.

    Defense in depth: walk_project_dir_inventory already raises on symlinks,
    but write_tar_with_manifest may be invoked with a project_dir that did
    not go through inventory (or with a manifest computed earlier and a
    project_dir mutated between then and now). Catching here protects the
    invariant on a different code path (tarfile.add walks via os.walk;
    inventory walks via Path.rglob).
    """
    if tarinfo.issym() or tarinfo.islnk():
        kind = 'symlink' if tarinfo.issym() else 'hardlink'
        raise RuntimeError(
            f"Refusing to archive {kind} entry: {tarinfo.name}. "
            f"Backup requires regular files only."
        )
    return tarinfo


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

    job_record and r2_artifacts are DEEP-COPIED to honor "snapshot" semantics:
    if the caller mutates a nested dict (e.g. adds a key to job_record after
    receiving the manifest), the persisted snapshot must not follow. Shallow
    copy via `list(...)` was insufficient — outer list was independent but
    inner dicts were aliased to the caller's references.
    """
    return {
        'backup_format_version': 1,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_host': socket.gethostname(),
        'job_record': copy.deepcopy(job_record),
        'r2_artifacts_snapshot': copy.deepcopy(list(r2_artifacts)),
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

        # 2. project_dir contents (recursive, arcname keeps a clean root).
        # filter= rejects sym/hardlink entries — backup contract requires
        # regular files only (see _reject_link_entries docstring).
        tf.add(project_dir, arcname=project_dir.name, filter=_reject_link_entries)


def read_manifest_from_tar(tar_path: Path) -> dict:
    """Read manifest.json from tar without full extraction.

    Raises RuntimeError if manifest.json is missing or is a directory entry
    (both signal a corrupt or wrong-format tar). JSON decode errors surface
    as-is so callers can distinguish "no manifest" from "manifest unparseable".
    """
    with tarfile.open(tar_path, 'r:gz') as tf:
        try:
            f = tf.extractfile('manifest.json')
        except KeyError:
            raise RuntimeError(
                f"tar at {tar_path} has no manifest.json — corrupt or wrong format"
            )
        if f is None:
            raise RuntimeError(
                f"tar at {tar_path}: manifest.json is a directory entry"
            )
        return json.loads(f.read().decode('utf-8'))


# Strict allowlist of tar entry types permitted by safe_extract_tar.
# REGTYPE   = b'0'    — regular file (canonical)
# AREGTYPE  = b'\x00' — regular file (pre-POSIX, written by old tars)
# DIRTYPE   = b'5'    — directory
# Everything else (symlink, hardlink, char/block device, FIFO,
# CONTTYPE=b'7', GNUTYPE_SPARSE=b'S', future types, …) rejected.
_ALLOWED_TAR_TYPES: frozenset[bytes] = frozenset((
    tarfile.REGTYPE,
    tarfile.AREGTYPE,
    tarfile.DIRTYPE,
))


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Safe tar extraction with an ALLOWLIST of member types.

    Python 3.12+ has tarfile.data_filter; 3.11 needs DIY (CodeX Q-B).

    Pass 1 validates EVERY tar member before any extraction starts — partial
    extraction of a malicious tar is itself a hazard (an early entry could
    plant a file that a later validation failure wouldn't undo).

    **Allowlist (not blocklist):** ONLY regular files and directories are
    permitted. Any other tar type (symlink, hardlink, char/block device,
    FIFO, contiguous file, sparse, …) is rejected. This is stricter than
    a blocklist and protects against future tar types we haven't enumerated.

    Backup contract per plan §4.4: backups contain only regular files
    (walk_project_dir_inventory + _reject_link_entries already enforce
    this at archive time). safe_extract_tar is the corresponding restore-
    side gate — if a tar somehow contains a non-allowed type, refuse it.

    Rejection categories (all surface as RuntimeError with a discriminating
    substring so callers / tests can branch):
      - absolute path (name starts with '/')  → 'unsafe absolute path'
      - .. path traversal segment             → 'unsafe .. path traversal'
      - symlink / hardlink entry              → 'unsafe symlink/hardlink'
      - char/block/FIFO/other special entry   → 'unsafe entry type'
      - resolved path falls outside dest      → 'unsafe resolved outside dest'
        (catches Windows-drive-prefixed names, exotic encodings, and any
        absolute-path bypass the first check might miss)
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    with tarfile.open(tar_path, 'r:gz') as tf:
        members = tf.getmembers()
        # Pass 1: validate every member.
        for m in members:
            if m.name.startswith('/'):
                raise RuntimeError(f"unsafe absolute path in tar: {m.name!r}")
            if '..' in Path(m.name).parts:
                raise RuntimeError(f"unsafe .. path traversal: {m.name!r}")
            if m.issym() or m.islnk():
                raise RuntimeError(
                    f"unsafe symlink/hardlink: {m.name!r} → {m.linkname!r}"
                )
            # Allowlist by explicit type byte. We do NOT use m.isfile()
            # because tarfile.REGULAR_TYPES is permissive (it includes
            # CONTTYPE=b'7' and GNUTYPE_SPARSE=b'S' alongside the two
            # genuine regular-file types REGTYPE=b'0' / AREGTYPE=b'\x00').
            # A strict explicit allowlist protects against future tarfile
            # additions and any exotic type a real-world tar might carry.
            if m.type not in _ALLOWED_TAR_TYPES:
                raise RuntimeError(
                    f"unsafe entry type for {m.name!r}: type={m.type!r} "
                    f"(only regular files and directories allowed)"
                )
            target = (dest / m.name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise RuntimeError(
                    f"unsafe resolved outside dest: {m.name!r} → {target}"
                )
        # Pass 2: extract — all validated.
        tf.extractall(dest)

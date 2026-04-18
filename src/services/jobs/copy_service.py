"""Filesystem helpers for copy_as_new (T1-8).

Plan ref: §3.5 / §7.8 / D27 / D28 / D34

Responsibilities:

- ``hardlink_baseline_audio`` — for every wav in
  ``<source>/editor/tts_segments/*.wav``, create an ``os.link`` at the
  matching path under ``<target>/editor/tts_segments/``. Inode is shared;
  deleting either end doesn't free the storage until both are gone.
- ``apply_draft_segment`` — SAFELY replace a (possibly hardlinked) wav at
  the target with the contents of a draft. **Must NOT** ``open(target, 'wb')``
  because that would mutate the shared inode, corrupting the source's
  baseline audio (§3.5 invariant). Instead: unlink the target first, then
  move the draft in. This is the single chokepoint enforced by AST guard
  ``test_no_raw_open_wb_on_shared_paths`` (plan §16.4).
- ``write_audio_safely`` — generic "produce a wav at this path" helper
  with the same safety posture; callers give us bytes, we atomically
  place them. Uses temp file + ``os.replace`` so there is no window in
  which the target is half-written.
- ``prepare_copy_project_dir`` — Phase A of copy_as_new (§7.8):
  1. ``shutil.copy2`` transcript.json / segments.json / manifest.json
  2. hardlink all baseline tts_segments wavs
  3. apply editing/segments.json edits → overwrite target segments.json
  4. apply editing/voice_map.json overrides → merge into target segments.json
  5. apply editing/tts_segments_draft → replace target wavs via
     ``apply_draft_segment`` (one per draft)
  Raises on any failure; caller is responsible for ``rollback_prepared_target``
  which performs best-effort cleanup so the source stays untouched.
- ``rollback_prepared_target`` — rm -rf the target project_dir. Used by
  T1-9 commit's Phase A failure branch.

What's NOT here:

- Creating the new Job row (Gateway's concern — T1-9 Phase A step 5).
- Submitting the new job to the runner (``runner_extensions``).
- Deleting source ``editor/editing/`` + resetting source status (Phase B,
  T1-9 runs after Phase A returns successfully).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CopyPreparationError",
    "apply_draft_segment",
    "hardlink_baseline_audio",
    "prepare_copy_project_dir",
    "rollback_prepared_target",
    "write_audio_safely",
]


class CopyPreparationError(Exception):
    """Raised when Phase A of copy_as_new cannot complete. Callers treat
    this as "source project_dir is untouched; target may be half-built —
    run rollback_prepared_target"."""


# ---------------------------------------------------------------------------
# Individual file helpers
# ---------------------------------------------------------------------------


def hardlink_baseline_audio(source_dir: str | Path, target_dir: str | Path) -> list[str]:
    """For every wav in ``source_dir/editor/tts_segments/``, create an
    ``os.link`` at the matching path under ``target_dir/editor/tts_segments/``.

    Returns the list of segment IDs that were hardlinked. Creates target
    directories as needed. Existing files at the target path are NOT
    replaced (caller should have a fresh target); raises FileExistsError.

    Linux/macOS: ``os.link`` is supported by all common filesystems.
    Windows NTFS supports it via CreateHardLinkW — this project runs on
    Linux in production (per plan §Linux 主机 decision). If future
    platforms need COW instead, swap this function.
    """
    src_audio = Path(source_dir) / "editor" / "tts_segments"
    dst_audio = Path(target_dir) / "editor" / "tts_segments"
    dst_audio.mkdir(parents=True, exist_ok=True)

    if not src_audio.is_dir():
        return []

    linked: list[str] = []
    for src_wav in sorted(src_audio.glob("*.wav")):
        segment_id = src_wav.stem
        dst_wav = dst_audio / src_wav.name
        os.link(src_wav, dst_wav)  # raises FileExistsError if dst exists
        linked.append(segment_id)
    return linked


def write_audio_safely(target_path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target_path`` via temp file + os.replace.

    Rationale: if ``target_path`` is a hardlink to another file (common in
    copy_as_new targets), ``open(target_path, 'wb')`` would open the shared
    inode for writing and the source's baseline audio would be corrupted.
    ``os.replace`` on a fresh temp file inside the same directory creates
    a new inode for the new bytes; the hardlink relationship is broken
    cleanly.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def apply_draft_segment(
    draft_path: str | Path,
    target_baseline_path: str | Path,
) -> None:
    """Replace (possibly hardlinked) ``target_baseline_path`` with contents
    of ``draft_path``. Unlinks the target first so a shared-inode source
    never sees the new bytes.

    - ``draft_path`` is moved (``shutil.move``), not copied, so the draft
      file no longer exists after success. Caller can assume draft is gone.
    - If ``draft_path`` does not exist, raises FileNotFoundError (callers
      should have already checked via ``tts_segments_draft`` enumeration).
    - If ``target_baseline_path`` does not exist (edge case: user edited a
      segment with no baseline audio), we create it fresh — no unlink.
    """
    draft = Path(draft_path)
    target = Path(target_baseline_path)
    if not draft.is_file():
        raise FileNotFoundError(f"draft not found: {draft}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Break any hardlink relationship before installing the new content.
        # Using unlink + move (instead of open('wb') overwrite) preserves
        # the source's inode intact.
        target.unlink()
    shutil.move(str(draft), str(target))


# ---------------------------------------------------------------------------
# Full Phase A orchestration
# ---------------------------------------------------------------------------


def _apply_voice_map_to_segments(
    segments: list[dict[str, Any]],
    voice_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a new list where every segment whose segment_id is in
    voice_map has its ``provider`` + ``voice_id`` overwritten. Segments
    not in voice_map pass through unchanged. Original list not mutated."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        sid = seg.get("segment_id")
        override = voice_map.get(sid) if isinstance(sid, str) else None
        if override:
            new_seg = dict(seg)
            new_seg["provider"] = override["provider"]
            new_seg["voice_id"] = override["voice_id"]
            out.append(new_seg)
        else:
            out.append(seg)
    return out


def prepare_copy_project_dir(
    source_project_dir: str | Path,
    target_project_dir: str | Path,
) -> dict[str, Any]:
    """Build the target project_dir as a Phase A preparation (plan §7.8 A1-A4).

    Returns a summary dict on success. Raises ``CopyPreparationError`` on
    any failure. Caller owns rollback in the failure path.

    The source is treated as read-only; we only ever read ``editor/...``
    files from it. Target must not already exist (caller's error if it does).
    """
    source = Path(source_project_dir)
    target = Path(target_project_dir)
    if target.exists():
        raise CopyPreparationError(
            f"target project_dir already exists: {target}; refusing to overwrite"
        )
    editing_dir = source / "editor" / "editing"
    if not editing_dir.is_dir():
        raise CopyPreparationError(
            f"source has no editor/editing/ dir: {editing_dir}; "
            "copy_as_new requires an active editing session"
        )

    try:
        # A2: baseline JSON files
        (target / "editor").mkdir(parents=True, exist_ok=True)
        for name in ("transcript.json", "manifest.json"):
            src_json = source / "editor" / name
            if src_json.is_file():
                shutil.copy2(src_json, target / "editor" / name)

        # A3: hardlink baseline wavs
        linked = hardlink_baseline_audio(source, target)

        # A4: apply editing diff
        # A4.1 segments.json: prefer editing/segments.json, fall back to baseline
        editing_segments_file = editing_dir / "segments.json"
        baseline_segments_file = source / "editor" / "segments.json"
        if editing_segments_file.is_file():
            segments_source_file = editing_segments_file
        elif baseline_segments_file.is_file():
            segments_source_file = baseline_segments_file
        else:
            segments_source_file = None
        segments: list[dict[str, Any]] = []
        if segments_source_file is not None:
            segments = json.loads(segments_source_file.read_text(encoding="utf-8"))
            if not isinstance(segments, list):
                raise CopyPreparationError(
                    f"segments JSON is not a list: {segments_source_file}"
                )
        # A4.2 voice_map.json — merge into segments
        voice_map_file = editing_dir / "voice_map.json"
        voice_map: dict[str, dict[str, Any]] = {}
        if voice_map_file.is_file():
            raw = json.loads(voice_map_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for sid, entry in raw.items():
                    if isinstance(entry, dict):
                        voice_map[str(sid)] = {
                            "provider": str(entry.get("provider", "")).strip(),
                            "voice_id": str(entry.get("voice_id", "")).strip(),
                        }
        if voice_map:
            segments = _apply_voice_map_to_segments(segments, voice_map)
        # A4.3 write merged segments.json to target
        target_segments_path = target / "editor" / "segments.json"
        target_segments_path.parent.mkdir(parents=True, exist_ok=True)
        target_segments_path.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # A4.4 draft wavs → target baseline via apply_draft_segment
        # We COPY (not move) drafts because the source's editing/ dir must
        # stay intact until Phase B deletes it — moving would leave Phase B
        # with nothing to clean if commit fails between A and B.
        drafts_dir = editing_dir / "tts_segments_draft"
        applied_drafts: list[str] = []
        if drafts_dir.is_dir():
            for draft_wav in sorted(drafts_dir.glob("*.wav")):
                segment_id = draft_wav.stem
                target_wav = target / "editor" / "tts_segments" / draft_wav.name
                # Copy first (draft stays intact), then install via apply
                copy_tmp = target / "editor" / "tts_segments" / f".{draft_wav.name}.copy"
                copy_tmp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(draft_wav, copy_tmp)
                apply_draft_segment(copy_tmp, target_wav)
                applied_drafts.append(segment_id)

    except CopyPreparationError:
        raise
    except Exception as exc:
        raise CopyPreparationError(
            f"copy Phase A failed: {exc}"
        ) from exc

    return {
        "linked_segment_ids": linked,
        "applied_draft_segment_ids": applied_drafts,
        "segments_count": len(segments),
    }


def rollback_prepared_target(target_project_dir: str | Path) -> None:
    """Best-effort cleanup of a half-built target from a failed Phase A.

    Uses ``ignore_errors=True`` because (a) this runs inside an error
    handler — we must not mask the primary exception by raising here;
    (b) hardlinked wavs are safe to remove (source's inode survives until
    all links are gone, and we're only unlinking the target's).
    """
    target = Path(target_project_dir)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

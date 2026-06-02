"""Lazy backfill for ``editor/tts_segments/`` from legacy ``tts/`` layout.

Two pipeline eras produced Studio tasks in different audio layouts:

- **Modern** (post-Phase-1): S6 publish writes
  ``editor/tts_segments/{sid}.wav`` directly — this is the baseline that
  γ resume-publish reads and copy_as_new hardlinks.

- **Legacy** (pre-Phase-1): S5 alignment wrote
  ``tts/segment_{sid:03d}_aligned.wav`` (zero-padded, ``_aligned`` suffix).
  The editor package writer used these files for packaging but did not
  promote them to ``editor/tts_segments/``.

Legacy tasks therefore cannot enter the commit → γ publish loop: copy_service's
``hardlink_baseline_audio`` finds the source's ``editor/tts_segments/`` empty
and copies only the user's drafts. γ then fails fast with "N segments missing
wavs" (correct behaviour — better than silently producing a silent video).

The helper in this module seeds ``editor/tts_segments/{sid}.wav`` from the
legacy ``tts/segment_{sid:03d}_aligned.wav`` files on the first
``enter_editing`` click. Subsequent edits / commits then flow normally.

Invariants:

- **Idempotent.** Existing wavs in ``editor/tts_segments/`` are never
  overwritten (important: user-accepted drafts must be preserved).
- **Non-destructive.** Source ``tts/`` files are left intact (copied, not
  moved) so any downstream consumer still reading them keeps working.
- **Segment-id mapping.** ``segment_007_aligned.wav`` → ``editor/tts_segments/7.wav``
  — the editing HTTP contract uses the raw id (no zero-padding) so the
  filename must match.
- **Tolerant of partial legacy data.** A missing aligned wav for a specific
  segment is recorded in ``missing_legacy_segment_ids`` and skipped, not
  raised. γ's own hard guard at commit time will surface any remaining
  gaps with an actionable sid list.

Only raises ``EditorTtsBaselineError`` when the task literally has no audio
source (no ``tts/`` directory AND no existing ``editor/tts_segments/``) —
then enter_editing should refuse (409) since there is no path to produce
a dubbed video for this task.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EditorTtsBaselineError",
    "ensure_editor_tts_segments_baseline",
]


class EditorTtsBaselineError(Exception):
    """Raised when the editor/tts_segments/ baseline cannot be materialized.

    The only cause: the task has neither a legacy ``tts/`` directory nor
    any pre-existing content in ``editor/tts_segments/``. enter_editing
    surfaces this as a 409 (conflict) — the task is simply too broken for
    the editing workflow.
    """


def _read_segment_records(project_dir: Path) -> list[dict[str, Any]]:
    """Return segment records, reading from editor/segments.json first
    (modern), falling back to translation/segments.json (legacy).

    Returns an empty list if neither file is present. Caller decides
    whether that's fatal.
    """
    editor_path = project_dir / "editor" / "segments.json"
    translation_path = project_dir / "translation" / "segments.json"

    raw_records: list[Any] = []
    if editor_path.is_file():
        payload = json.loads(editor_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            raw_records = payload
        elif isinstance(payload, dict):
            inner = payload.get("segments")
            if isinstance(inner, list):
                raw_records = inner
    if not raw_records and translation_path.is_file():
        payload = json.loads(translation_path.read_text(encoding="utf-8"))
        inner = (
            payload.get("segments") if isinstance(payload, dict) else payload
        )
        if isinstance(inner, list):
            raw_records = inner

    records: list[dict[str, Any]] = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            continue
        sid = rec.get("segment_id")
        if sid is None:
            continue
        records.append(rec)
    return records


def _read_segment_ids(project_dir: Path) -> list[str]:
    """Return segment_ids as strings."""
    return [str(rec.get("segment_id")) for rec in _read_segment_records(project_dir)]


def _legacy_aligned_wav_path(project_dir: Path, sid_str: str) -> Path | None:
    """Resolve the legacy aligned wav for ``sid_str``.

    Pipeline writes the path with zero-padded id: ``segment_{sid:03d}_aligned.wav``
    (see ``services/alignment/aligner.py``). If ``sid_str`` is not castable
    to int (weird legacy task), return None — caller skips.
    """
    try:
        sid_int = int(sid_str)
    except (TypeError, ValueError):
        return None
    return project_dir / "tts" / f"segment_{sid_int:03d}_aligned.wav"


def _is_keep_original_segment(record: dict[str, Any]) -> bool:
    values = (
        record.get("dubbing_mode"),
        record.get("alignment_method"),
        record.get("tts_provider"),
        record.get("selected_voice"),
    )
    normalized = {str(value).strip().lower() for value in values if value is not None}
    return bool(
        {
            "keep_original",
            "original",
            "original_audio",
        }
        & normalized
    )


def _metadata_wav_path(project_dir: Path, record: dict[str, Any]) -> Path | None:
    project_root = project_dir.resolve(strict=False)
    for key in ("aligned_audio_path", "tts_audio_path"):
        raw_path = record.get(key)
        if not raw_path:
            continue
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(project_root)
        except ValueError:
            continue
        if resolved.is_file() and resolved.suffix.lower() == ".wav":
            return resolved
    return None


def _legacy_original_wav_path(project_dir: Path, sid_str: str) -> Path | None:
    try:
        sid_int = int(sid_str)
    except (TypeError, ValueError):
        return None
    pattern = f"segment_{sid_int:03d}_*_original.wav"
    matches = sorted((project_dir / "tts").glob(pattern))
    return matches[0] if matches else None


def _legacy_audio_wav_path(
    project_dir: Path, sid_str: str, record: dict[str, Any]
) -> Path | None:
    aligned = _legacy_aligned_wav_path(project_dir, sid_str)
    if aligned is not None and aligned.is_file():
        return aligned
    if not _is_keep_original_segment(record):
        return None
    metadata_path = _metadata_wav_path(project_dir, record)
    if metadata_path is not None:
        return metadata_path
    return _legacy_original_wav_path(project_dir, sid_str)


def ensure_editor_tts_segments_baseline(project_dir: Path) -> dict[str, Any]:
    """Materialise ``editor/tts_segments/{sid}.wav`` for every segment that
    has a legacy ``tts/segment_{sid:03d}_aligned.wav`` counterpart.

    Parameters
    ----------
    project_dir
        Task project directory. Must contain either
        ``editor/segments.json`` or ``translation/segments.json`` so the
        segment list is known.

    Returns
    -------
    dict
        ``{
            "backfilled_segment_ids": [...],          # newly copied
            "skipped_existing_segment_ids": [...],    # already in editor/tts_segments/
            "missing_legacy_segment_ids": [...],      # no legacy wav, left ungenerated
        }``

    Raises
    ------
    EditorTtsBaselineError
        When the task has no viable audio source (no ``tts/`` dir AND no
        pre-existing ``editor/tts_segments/`` content).
    """
    segment_records = _read_segment_records(project_dir)

    editor_tts_dir = project_dir / "editor" / "tts_segments"
    editor_tts_dir.mkdir(parents=True, exist_ok=True)

    legacy_tts_dir = project_dir / "tts"
    has_legacy = legacy_tts_dir.is_dir() and (
        any(legacy_tts_dir.glob("segment_*_aligned.wav"))
        or any(legacy_tts_dir.glob("segment_*_original.wav"))
    )
    has_existing_baseline = any(editor_tts_dir.glob("*.wav"))

    if not has_legacy and not has_existing_baseline:
        raise EditorTtsBaselineError(
            f"no audio source for editor/tts_segments/ at {project_dir}: "
            "neither tts/*_aligned.wav nor editor/tts_segments/*.wav exists"
        )

    backfilled: list[str] = []
    skipped_existing: list[str] = []
    missing_legacy: list[str] = []

    for record in segment_records:
        sid_str = str(record.get("segment_id"))
        dst = editor_tts_dir / f"{sid_str}.wav"
        if dst.is_file():
            skipped_existing.append(sid_str)
            continue
        src = _legacy_audio_wav_path(project_dir, sid_str, record)
        if src is None or not src.is_file():
            missing_legacy.append(sid_str)
            continue
        # copy2 preserves mtime — useful so downstream cache-checks treat
        # the new file as "as old as the aligned wav it came from".
        shutil.copy2(src, dst)
        backfilled.append(sid_str)

    logger.info(
        "ensure_editor_tts_segments_baseline: project=%s backfilled=%d "
        "skipped_existing=%d missing_legacy=%d",
        project_dir, len(backfilled), len(skipped_existing), len(missing_legacy),
    )

    return {
        "backfilled_segment_ids": backfilled,
        "skipped_existing_segment_ids": skipped_existing,
        "missing_legacy_segment_ids": missing_legacy,
    }

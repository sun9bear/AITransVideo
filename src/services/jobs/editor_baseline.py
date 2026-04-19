"""Shared writer for ``editor/segments.json`` baseline.

Two callers:

1. ``src.pipeline.process.LocalizeVideoOrchestrator`` — S6 publish stage
   calls this after ``translation/segments.json`` is rewritten with full
   DubbingSegment schema, so newly completed Studio jobs land with a
   baseline already on disk. This is the authoritative path.

2. ``services.jobs.editing.enter_editing`` — legacy / pre-Phase-1 tasks
   whose publish step ran before this helper existed have no baseline.
   The editing layer falls back to this helper on first ``/enter-edit``
   and writes the baseline lazily. Subsequent enter_editing calls see
   the baseline and skip the helper entirely.

Both callers share the same normalisation rules so a task whose baseline
was produced by path 1 behaves identically to one produced by path 2.
In particular, ``segment_id`` is cast to ``str`` so that the HTTP edit
contract (input_validators regex + patch/regen lookups) works unchanged —
the pipeline otherwise carries integer ids.

Filesystem write is atomic (``tmpfile + replace``) so a crash mid-write
cannot leave half-written JSON that downstream readers would mistake for
a valid baseline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EditorBaselineError",
    "normalise_segment_record",
    "write_editor_segments_from_translation",
]


class EditorBaselineError(Exception):
    """Raised when the baseline cannot be produced from translation/segments.json.

    Causes: translation file absent, unreadable JSON, or no usable
    ``segments`` list inside the payload. Callers decide whether to treat
    this as fatal (editing.enter_editing → 409) or non-fatal (pipeline
    publish → log warning and continue, letting lazy seed pick up later).
    """


def normalise_segment_record(segment: Any) -> Any:
    """Return a segment dict with ``segment_id`` cast to ``str``.

    Non-dict items pass through untouched (caller should refuse wholly
    invalid payloads upstream). ``segment_id`` of ``None`` is left as
    ``None`` so downstream validators can surface the error, rather than
    silently producing the literal string ``"None"``.
    """
    if not isinstance(segment, dict):
        return segment
    sid = segment.get("segment_id")
    if sid is None or isinstance(sid, str):
        return segment
    return {**segment, "segment_id": str(sid)}


def write_editor_segments_from_translation(project_dir: Path) -> Path:
    """Seed ``<project_dir>/editor/segments.json`` from translation/.

    Reads ``<project_dir>/translation/segments.json``, extracts the
    ``segments`` list (supporting both ``{"segments": [...]}`` wrap and a
    raw list at top level), normalises ``segment_id`` to str, and writes
    the result to ``<project_dir>/editor/segments.json`` atomically.

    Returns the path written on success. Raises ``EditorBaselineError``
    with an explanatory message on any failure.

    The helper does NOT check whether editor/segments.json already exists —
    it unconditionally overwrites. Callers that want "seed only if missing"
    semantics (editing.enter_editing) must guard the call themselves. The
    pipeline publish caller intentionally overwrites to guarantee the
    baseline always reflects the latest translation snapshot.
    """
    translation_path = project_dir / "translation" / "segments.json"
    if not translation_path.is_file():
        raise EditorBaselineError(
            f"translation/segments.json not found at {translation_path}"
        )

    try:
        trans = json.loads(translation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorBaselineError(
            f"translation/segments.json is unreadable: {exc.__class__.__name__}"
        ) from exc

    if isinstance(trans, dict):
        segments = trans.get("segments")
    elif isinstance(trans, list):
        segments = trans
    else:
        segments = None

    if not isinstance(segments, list):
        raise EditorBaselineError(
            f"translation/segments.json has no usable 'segments' list "
            f"(got {type(segments).__name__})"
        )

    segments = [normalise_segment_record(s) for s in segments]

    editor_dir = project_dir / "editor"
    editor_dir.mkdir(parents=True, exist_ok=True)
    baseline = editor_dir / "segments.json"
    tmp = baseline.with_suffix(baseline.suffix + ".seed.tmp")
    try:
        tmp.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(baseline)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return baseline

"""Batch re-TTS orchestrator for the editing state (T1-6).

Plan ref: §7.4 / D38 / D39

Scans ``segment_status.json`` for segments that need re-synthesis
(``text_dirty`` / ``voice_dirty`` / ``tts_failed``), and calls
``regenerate_segment_tts`` one by one with the injected TTS caller. A
single segment's failure does NOT abort the batch — we collect failures
and return them alongside the successes so the UI can offer per-segment
retry (plan D38 response shape).

Async / background-task integration (plan D39) is intentionally deferred:
the synchronous form covers the Phase 1 functional surface and the
async wrapper can wrap this function without changing its semantics.
Adding the async hop now before we have any profiling evidence that
30-segment batches are too slow would be premature infrastructure.

Responses follow the plan D38 contract:

    {
      "total": 30,
      "succeeded_count": 29,
      "failed_count": 1,
      "failed_segment_ids": ["seg_042"],
      "failures": [{"segment_id": "seg_042", "error": "upstream 429"}]
    }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services.jobs.editing_segments import (
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
    SEGMENT_STATUS_VOICE_DIRTY,
    load_segment_status,
)
from services.jobs.editing_tts import SegmentTTSCaller, regenerate_segment_tts

logger = logging.getLogger(__name__)

__all__ = [
    "BATCH_REGENERATE_TRIGGER_STATUSES",
    "regenerate_all_dirty_segments",
]

# Statuses that cause a segment to be picked up by the batch regenerate.
# ``tts_loading`` is deliberately excluded — it means a previous regenerate
# is in flight (shouldn't happen concurrently but defensive skip).
# ``tts_dirty`` is excluded too: the user hasn't accepted/discarded yet, so
# clobbering their draft would be surprising.
BATCH_REGENERATE_TRIGGER_STATUSES: frozenset[str] = frozenset({
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_VOICE_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
})


def regenerate_all_dirty_segments(
    project_dir: str | Path,
    *,
    tts_caller: SegmentTTSCaller | None = None,
    default_tts_model: str | None = None,
    segment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run regenerate_segment_tts on every segment whose status is in
    BATCH_REGENERATE_TRIGGER_STATUSES. Continue on per-segment failure.

    Returns a summary dict per plan D38.
    """
    status_map = load_segment_status(project_dir)
    if segment_ids is None:
        eligible = sorted(
            sid for sid, status in status_map.items()
            if status in BATCH_REGENERATE_TRIGGER_STATUSES
        )
    else:
        seen: set[str] = set()
        eligible = []
        for raw_sid in segment_ids:
            sid = str(raw_sid).strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            if status_map.get(sid) in BATCH_REGENERATE_TRIGGER_STATUSES:
                eligible.append(sid)

    succeeded: list[str] = []
    failures: list[dict[str, str]] = []

    for segment_id in eligible:
        try:
            regenerate_segment_tts(
                project_dir,
                segment_id,
                tts_caller=tts_caller,
                default_tts_model=default_tts_model,
            )
            succeeded.append(segment_id)
        except Exception as exc:
            # Per-segment failure is recorded and we move on. The segment's
            # segment_status has already been flagged tts_failed by
            # regenerate_segment_tts's own error path, so subsequent batch
            # runs will re-attempt it unless the user discards first.
            logger.info(
                "batch regenerate: segment_id=%s failed: %s",
                segment_id,
                exc,
            )
            failures.append(
                {"segment_id": segment_id, "error": str(exc)}
            )

    return {
        "total": len(eligible),
        "succeeded_count": len(succeeded),
        "failed_count": len(failures),
        "succeeded_segment_ids": succeeded,
        "failed_segment_ids": [f["segment_id"] for f in failures],
        "failures": failures,
    }

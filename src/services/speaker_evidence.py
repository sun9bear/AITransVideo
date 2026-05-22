"""Feature-flagged speaker evidence sidecar helpers.

The sidecar is observational only. It must never alter transcript lines,
speaker ids, timing, or review decisions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

SPEAKER_EVIDENCE_SCHEMA_VERSION = 1


@dataclass(slots=True)
class SpeakerEvidence:
    line_id: str | None
    source_line_ids: list[str]
    parent_line_id: str | None
    semantic_block_id: str | None
    segment_id: int | None
    final_segment_id: str | None
    merge_group_id: str | None
    stage: str
    source_start_ms: int | None
    source_end_ms: int | None
    initial_speaker_id: str | None
    final_speaker_id: str | None
    source: str
    decision: str
    confidence: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    schema_version: int = SPEAKER_EVIDENCE_SCHEMA_VERSION


def speaker_evidence_path(project_dir: Path) -> Path:
    return project_dir / "reports" / "speaker_evidence.jsonl"


def write_speaker_evidence_jsonl(path: Path, rows: list[SpeakerEvidence]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = "".join(
            json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        )
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(encoded, encoding="utf-8")
        tmp_path.replace(path)
        return True
    except OSError:
        return False


def build_speaker_evidence_from_snapshots(
    *,
    original_snapshot: list[Mapping[str, Any]],
    final_snapshot: list[Mapping[str, Any]],
    review_model: str = "",
    has_audio: bool = False,
) -> list[SpeakerEvidence]:
    """Build conservative per-final-line evidence rows from review snapshots.

    Matching is intentionally local and deterministic. We prefer original line
    ``index`` and fall back to ``start_ms``. If a split/merge makes provenance
    ambiguous, the row still records final-line identity but marks the source as
    ``fallback`` instead of inventing a confident parent relation.
    """
    originals_by_index: dict[int, Mapping[str, Any]] = {}
    originals_by_start: dict[int, Mapping[str, Any]] = {}
    for row in original_snapshot:
        index = _coerce_int(row.get("index"))
        start_ms = _coerce_int(row.get("start_ms"))
        if index is not None:
            originals_by_index.setdefault(index, row)
        if start_ms is not None:
            originals_by_start.setdefault(start_ms, row)

    rows: list[SpeakerEvidence] = []
    for final_row in final_snapshot:
        final_index = _coerce_int(final_row.get("index"))
        start_ms = _coerce_int(final_row.get("start_ms"))
        original_row: Mapping[str, Any] | None = None
        if final_index is not None:
            original_row = originals_by_index.get(final_index)
        if original_row is None and start_ms is not None:
            original_row = originals_by_start.get(start_ms)

        source_line_id = _line_id(_coerce_int(original_row.get("index")) if original_row else None)
        final_line_id = _line_id(final_index)
        initial_speaker = _coerce_str(original_row.get("speaker_id")) if original_row else None
        final_speaker = _coerce_str(final_row.get("speaker_id"))
        decision = "kept"
        reason_codes: list[str] = []
        source = "asr"
        if original_row is None:
            decision = "kept_uncertain"
            source = "fallback"
            reason_codes.append("original_line_unmatched")
        elif initial_speaker != final_speaker:
            decision = "changed"
            source = "reviewer"
            reason_codes.append("speaker_changed")

        rows.append(
            SpeakerEvidence(
                line_id=final_line_id,
                source_line_ids=[source_line_id] if source_line_id else [],
                parent_line_id=source_line_id if source_line_id != final_line_id else None,
                semantic_block_id=None,
                segment_id=final_index,
                final_segment_id=f"seg_{final_index:06d}" if final_index is not None else None,
                merge_group_id=None,
                stage="s2_review",
                source_start_ms=start_ms,
                source_end_ms=_coerce_int(final_row.get("end_ms")),
                initial_speaker_id=initial_speaker,
                final_speaker_id=final_speaker,
                source=source,
                decision=decision,
                confidence=None,
                evidence={
                    "review_model": review_model,
                    "has_audio": bool(has_audio),
                    "original_index": _coerce_int(original_row.get("index")) if original_row else None,
                    "final_index": final_index,
                },
                reason_codes=reason_codes,
            )
        )
    return rows


def _line_id(index: int | None) -> str | None:
    if index is None:
        return None
    return f"line_{index:06d}"


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "SPEAKER_EVIDENCE_SCHEMA_VERSION",
    "SpeakerEvidence",
    "build_speaker_evidence_from_snapshots",
    "speaker_evidence_path",
    "write_speaker_evidence_jsonl",
]

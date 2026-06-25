"""PR#5/PR-1 — Smart-analytics admin dashboard backend.

Spec: ``docs/plans/2026-05-22-smart-analytics-v1.md``.

Provides two admin-only endpoints:

  GET /api/admin/smart-analytics/summary?days=N&status=&user=
  GET /api/admin/smart-analytics/csv?days=N&status=&user=

Both aggregate from three on-disk sources per smart-mode job:

  * ``{project_dir}/output/alignment_report.txt`` — alignment-quality stats
  * ``{project_dir}/audit/smart_decisions.jsonl`` — handoff reason codes
  * ``{project_dir}/audit/user_edit_events.jsonl`` — user-rework events

…plus PG `jobs` + `users` rows for status / display_name / user_email.

The aggregation pipeline is broken into small pure helpers so each can be
TDD-pinned independently (see ``tests/test_admin_smart_analytics.py``).

Admin-only: gated by ``_require_admin`` per the same pattern as
``admin_cost_api.py``.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import admin_settings as admin_settings_store
from admin_auth import _require_admin
from auth import get_current_user
from csrf import require_same_origin_state_change
from database import get_db
from models import Job, User
from services.phase1b_report_summary import (
    build_phase1b_csv,
    build_phase1b_summary,
    summarize_project_reports,
)


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/smart-analytics", tags=["admin-smart-analytics"]
)


# ─────────────────────────────────────────────────────────────────────
# alignment_report.txt parser
# ─────────────────────────────────────────────────────────────────────

# Each line has the shape:    <label>：<n>段（<pct>%）
# We anchor on the Chinese label and pull (n, pct) tolerant of whitespace.

_ALIGNMENT_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "direct": (
        re.compile(r"直接使用（误差<5%）：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "direct_pct",
    ),
    "dsp": (
        re.compile(r"(?<!后)DSP变速：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "dsp_pct",
    ),
    "rewrite_direct": (
        re.compile(r"Gemini重写后直接使用：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "rewrite_direct_pct",
    ),
    "rewrite_dsp": (
        re.compile(r"Gemini重写后DSP对齐：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "rewrite_dsp_pct",
    ),
    "forced_dsp": (
        re.compile(r"强制DSP兜底：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "forced_dsp_pct",
    ),
    "short_segment": (
        re.compile(r"短段听感保护DSP：\s*\d+\s*段\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）"),
        "short_segment_dsp_pct",
    ),
}

_TOTAL_SEGMENTS_RE = re.compile(r"总段数：\s*(\d+)\s*段")
_MANUAL_REVIEW_RE = re.compile(r"需要手工检查的段落（共\s*(\d+)\s*段）")


def _parse_alignment_report(text: str) -> dict[str, Any]:
    """Parse ``alignment_report.txt`` into a flat dict.

    Returned keys (all may be ``None`` if absent):
      total_segments, direct_pct, dsp_pct, rewrite_direct_pct,
      rewrite_dsp_pct, forced_dsp_pct, short_segment_dsp_pct,
      manual_review_segments

    Percentages are returned as fractions (37% → 0.37). Counts are ints.
    """
    out: dict[str, Any] = {
        "total_segments": None,
        "direct_pct": None,
        "dsp_pct": None,
        "rewrite_direct_pct": None,
        "rewrite_dsp_pct": None,
        "forced_dsp_pct": None,
        "short_segment_dsp_pct": None,
        "manual_review_segments": None,
    }
    if not text:
        return out

    if (m := _TOTAL_SEGMENTS_RE.search(text)) is not None:
        try:
            out["total_segments"] = int(m.group(1))
        except (TypeError, ValueError):
            pass

    for _, (pattern, out_key) in _ALIGNMENT_PATTERNS.items():
        if (m := pattern.search(text)) is not None:
            try:
                out[out_key] = float(m.group(1)) / 100.0
            except (TypeError, ValueError):
                continue

    if (m := _MANUAL_REVIEW_RE.search(text)) is not None:
        try:
            out["manual_review_segments"] = int(m.group(1))
        except (TypeError, ValueError):
            pass

    return out


# ─────────────────────────────────────────────────────────────────────
# JSONL audit parsers
# ─────────────────────────────────────────────────────────────────────


def _iter_jsonl_records(path: Path):
    """Yield parsed JSON dicts from a JSONL file. Silently skips
    blank lines and malformed JSON — audit logs are append-only so
    a partial last line shouldn't crash analytics."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def _count_handoff_reasons_from_decisions(path: Path) -> dict[str, int]:
    """Count occurrences of ``reason_code`` in ``smart_decisions.jsonl``
    restricted to rows where ``decision_type == "downgrade_handoff"``.

    Other decision_types (speaker_gate, voice_clone, etc) are excluded —
    they record routine state, not handoff causes.
    """
    counts: dict[str, int] = {}
    for record in _iter_jsonl_records(path):
        if record.get("decision_type") != "downgrade_handoff":
            continue
        code = record.get("reason_code")
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    return counts


def _count_edit_events(path: Path) -> dict[str, int]:
    """Count occurrences of each ``event_type`` in ``user_edit_events.jsonl``."""
    counts: dict[str, int] = {}
    for record in _iter_jsonl_records(path):
        event_type = record.get("event_type")
        if not event_type:
            continue
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


# ─────────────────────────────────────────────────────────────────────
# Voice auto-reuse quality (Task #26)
# Spec: docs/plans/2026-05-24-smart-analytics-voice-reuse-quality-design.md (v2)
# ─────────────────────────────────────────────────────────────────────


_VOICE_REUSE_BUCKETS = (
    "strong",
    "strong_named",
    "possible_auto",
    "strong_or_legacy_null",
)


def _classify_voice_decision(record: dict) -> str | None:
    """Bucket a smart_decisions.jsonl record into one of 4 voice
    auto-reuse tiers, or ``None`` if it's not a REUSED decision.

    Discrimination rules (design §2.1):
      - Phase 5 (Task #27 post-fix):
        reason_code="possible_user_voice_match_auto_reused" AND
        evidence.auto_reused_from_possible_match=True → "possible_auto"
      - Strong same-source: evidence.match_confidence="strong" → "strong"
      - Strong cross-source named: evidence.match_confidence="strong_named"
        → "strong_named"
      - Legacy/null (pre-Task-#27 or missing confidence) → "strong_or_legacy_null"
        (separate bucket so analytics doesn't silently merge into strong)
      - Anything else (CLONED, PRESET, handoff, etc.) → None

    evidence.* is the canonical path (on-disk JSONL); metrics.* is a
    fallback for legacy test fixtures that mirror the dataclass shape
    (codex 第二轮 review #1 + #4).
    """
    reason_code = record.get("reason_code")
    if not reason_code:
        return None

    evidence = record.get("evidence") or {}
    metrics = record.get("metrics") or {}

    if reason_code == "possible_user_voice_match_auto_reused":
        if evidence.get("auto_reused_from_possible_match") is True:
            return "possible_auto"
        # Phase 5 reason without the flag is malformed — fall through
        # to legacy bucket so it's surfaced rather than miscounted.
        return "strong_or_legacy_null"

    if reason_code != "reused_user_voice":
        return None

    confidence = evidence.get("match_confidence")
    if confidence is None and "match_confidence" not in evidence:
        # evidence didn't even have the key — try metrics fallback
        # for test fixtures / very old records.
        confidence = metrics.get("match_confidence")

    if confidence == "strong":
        return "strong"
    if confidence == "strong_named":
        return "strong_named"
    # Includes confidence=None, missing key, or any unrecognized
    # legacy value — separate bucket per codex #4.
    return "strong_or_legacy_null"


def _load_segment_to_speaker_mapping(project_dir: Path) -> dict[str, str]:
    """Build segment_id → speaker_id mapping from segments.json files.

    Codex review v2 (2026-05-24, codex #1): the canonical project
    layout in this repo (see process.py:6332-6346 + editing.py:108)
    is:

      1. editor/editing/segments.json — active editing draft. Most
         recent truth while a user is editing. Snapshotted from
         editor/segments.json on enter_editing.
      2. editor/segments.json — canonical post-commit baseline. Written
         by pipeline publish step + editing_commit (overwrite path).
         Stores user text edits + voice_map overrides + str-normalised
         segment_id.
      3. translation/segments.json — legacy fallback for tasks that
         never went through editing.

    Earlier sources win on collision (editing > committed > legacy).
    Segments without speaker_id are silently skipped (keep_original /
    overlap suspected).

    Both JSON shapes supported (mirrors editor_baseline /
    write_editor_segments_from_translation):
      - {"segments": [...]}
      - raw [...] at top level

    Returns {} when project_dir is None / missing or no sources exist.
    """
    if project_dir is None:
        return {}

    sources = (
        project_dir / "editor" / "editing" / "segments.json",
        project_dir / "editor" / "segments.json",
        project_dir / "translation" / "segments.json",
    )

    mapping: dict[str, str] = {}
    for path in sources:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "segments.json parse failed at %s: %s", path, exc,
            )
            continue
        # Accept both shapes: wrapped {"segments": [...]} OR raw list.
        if isinstance(data, dict):
            segments = data.get("segments")
        elif isinstance(data, list):
            segments = data
        else:
            segments = None
        if not isinstance(segments, list):
            continue
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_id = seg.get("segment_id")
            speaker_id = seg.get("speaker_id")
            if seg_id is None or not speaker_id:
                continue
            seg_id_str = str(seg_id)
            if seg_id_str in mapping:
                # Earlier source already populated; skip (editing wins).
                continue
            mapping[seg_id_str] = str(speaker_id)
    return mapping


def _count_voice_overrides_per_speaker(
    events_path: Path, segment_to_speaker: dict[str, str]
) -> tuple[set[str], int, int, list[dict[str, Any]]]:
    """Count distinct speakers with at least one
    ``post_edit_voice_override_changed`` event (design §3.1 main numerator).

    Returns ``(set_of_changed_speakers, unmapped_segment_count,
    total_voice_override_event_count, override_details)``.

    The 3rd element (codex v2 review #5) is the denominator for the
    unmapped-rate signal in the UI: ``unmapped / total > 5%`` triggers
    an ochre warning on the dashboard.

    The 4th element (codex v2 followup #1) is a list of per-event dicts
    with the speaker_id resolved + before/after voice_id + operation +
    changed_at, so the case_rows table can show "from what to what".
    Only mapped events appear in this list (unmapped are excluded —
    they'd have a null speaker and clutter the case table).

    ``voice_selection_speaker_reassigned`` and
    ``voice_selection_dubbing_mode_changed`` are intentionally excluded
    from the main numerator AND the total (they're not voice-override
    events — codex 第二轮 review #3 + design §2.2).
    """
    changed: set[str] = set()
    unmapped = 0
    total = 0
    details: list[dict[str, Any]] = []
    for record in _iter_jsonl_records(events_path):
        if record.get("event_type") != "post_edit_voice_override_changed":
            continue
        total += 1
        segment = record.get("segment") or {}
        if not isinstance(segment, dict):
            unmapped += 1
            continue
        seg_id = segment.get("segment_id")
        if seg_id is None:
            unmapped += 1
            continue
        speaker_id = segment_to_speaker.get(str(seg_id))
        if not speaker_id:
            unmapped += 1
            continue
        changed.add(speaker_id)
        # Capture event details for case_rows downstream. Defensive
        # dict access — older records may be missing some sub-blocks.
        before = record.get("before") or {}
        after = record.get("after") or {}
        context = record.get("context") or {}
        details.append({
            "speaker_id": speaker_id,
            "before_voice_id": (before.get("voice_id") if isinstance(before, dict) else None),
            "after_voice_id": (after.get("voice_id") if isinstance(after, dict) else None),
            "operation": (context.get("operation") if isinstance(context, dict) else None),
            "changed_at": record.get("created_at"),
        })
    return changed, unmapped, total, details


def _count_speaker_reassigned_per_job(
    events_path: Path,
    segment_to_speaker: dict[str, str] | None = None,
) -> set[str]:
    """Auxiliary indicator (design §3.4 + codex v2 followup #2).

    Reads ``before.speaker_id`` / ``after.speaker_id`` DIRECTLY from
    the voice_selection_speaker_reassigned event. The earlier
    implementation went through ``segment_to_speaker`` mapping, which
    is wrong because that mapping reflects the CURRENT editor state —
    a segment that was reassigned now points to after.speaker_id, so
    the original ``from`` correction signal would be lost.

    Returns the union of ``before.speaker_id`` ∪ ``after.speaker_id``
    so the caller can intersect against auto-reuse hit_speakers.

    ``segment_to_speaker`` kwarg is kept for back-compat with callers
    that still pass it (no longer used).
    """
    _ = segment_to_speaker  # silence "unused"
    out: set[str] = set()
    for record in _iter_jsonl_records(events_path):
        if record.get("event_type") != "voice_selection_speaker_reassigned":
            continue
        for block_key in ("before", "after"):
            block = record.get(block_key) or {}
            if not isinstance(block, dict):
                continue
            sp = block.get("speaker_id")
            if sp:
                out.add(str(sp))
    return out


def _empty_voice_reuse_hits() -> dict[str, set[str]]:
    return {bucket: set() for bucket in _VOICE_REUSE_BUCKETS}


def _collect_voice_reuse_hits(decisions_path: Path) -> dict[str, set[str]]:
    """Walk smart_decisions.jsonl, classify each record, and return
    per-bucket {speaker_id} sets."""
    hits = _empty_voice_reuse_hits()
    for record in _iter_jsonl_records(decisions_path):
        bucket = _classify_voice_decision(record)
        if bucket is None:
            continue
        speaker_id = record.get("speaker_id")
        if speaker_id is None:
            # Some early records may put speaker_id only in evidence
            evidence = record.get("evidence") or {}
            speaker_id = evidence.get("speaker_id")
        if not speaker_id:
            continue
        hits[bucket].add(str(speaker_id))
    return hits


def _aggregate_voice_reuse_quality(
    metrics_list,
) -> dict[str, Any]:
    """Cross-job aggregation of voice auto-reuse hit/change tallies.

    Codex v2 review #2 #3 #4 #5 — output now includes:
      - 4+1 bucket {hits, changes, change_rate, thresholds}
      - jobs_with_voice_change_rate (codex #3 — uses hit∩changed
        intersection, not "any voice change" non-empty)
      - auto_reuse_jobs_entering_edit_rate (codex #2 — design §3.2)
      - speaker_reassigned_rate (codex #2 — design §3.4 auxiliary)
      - unmapped_segment_rate (codex #5 — denominator from
        voice_override_event_count)
      - case_rows (codex #4 — backend produces sample table for UI)

    Input: iterable of objects exposing:
      - job_id, created_at
      - voice_reuse_hits: dict[bucket -> set[speaker_id]]
      - voice_changed_speakers: set[speaker_id]
      - speakers_reassigned: set[speaker_id]
      - unmapped_segment_count: int
      - voice_override_event_count: int
      - entered_editing: bool
    """
    THRESHOLD_WARN = 0.30  # rebaseline §6.3
    THRESHOLD_CRIT = 0.50  # cinnabar / "强烈建议收紧"
    UNMAPPED_WARN = 0.05   # design §3.4: >5% triggers ochre

    per_bucket_hits = {b: 0 for b in _VOICE_REUSE_BUCKETS}
    per_bucket_changes = {b: 0 for b in _VOICE_REUSE_BUCKETS}
    jobs_with_hits = 0
    jobs_with_hits_and_relevant_change = 0
    jobs_with_hits_entered_editing = 0
    unmapped_total = 0
    voice_override_total = 0
    total_hit_speakers = 0
    total_reassigned_speakers = 0
    case_rows: list[dict[str, Any]] = []

    for m in metrics_list:
        unmapped_total += int(getattr(m, "unmapped_segment_count", 0) or 0)
        voice_override_total += int(
            getattr(m, "voice_override_event_count", 0) or 0
        )

        # Per-bucket per-job tallies + map speaker → bucket for case rows.
        job_hit_speakers: set[str] = set()
        speaker_to_bucket: dict[str, str] = {}
        changed = m.voice_changed_speakers or set()
        for bucket in _VOICE_REUSE_BUCKETS:
            speakers = (m.voice_reuse_hits or {}).get(bucket) or set()
            per_bucket_hits[bucket] += len(speakers)
            hit_and_changed = speakers & changed
            per_bucket_changes[bucket] += len(hit_and_changed)
            job_hit_speakers |= speakers
            for sp in speakers:
                speaker_to_bucket[sp] = bucket

        # Codex v2 followup #1: emit one case row per voice_override
        # event whose speaker is in any auto-reuse hit bucket. Use the
        # event-level details (before/after voice_id + operation +
        # changed_at) so admin sees "from what to what".
        for detail in getattr(m, "voice_override_details", None) or []:
            sp = detail.get("speaker_id")
            if not sp or sp not in speaker_to_bucket:
                continue
            case_rows.append({
                "job_id": str(getattr(m, "job_id", "") or ""),
                "speaker_id": sp,
                "bucket": speaker_to_bucket[sp],
                "before_voice_id": detail.get("before_voice_id"),
                "after_voice_id": detail.get("after_voice_id"),
                "operation": detail.get("operation"),
                "changed_at": detail.get("changed_at"),
            })

        if job_hit_speakers:
            jobs_with_hits += 1
            total_hit_speakers += len(job_hit_speakers)
            total_reassigned_speakers += len(
                (m.speakers_reassigned or set()) & job_hit_speakers
            )
            # Codex #3: a job 'has voice change for its auto-reuse hits'
            # iff hit_speakers ∩ voice_changed_speakers is non-empty.
            # Plain "voice_changed_speakers non-empty" would over-count
            # because changing speaker_z (not in hits) shouldn't reflect
            # on auto-reuse quality.
            if job_hit_speakers & changed:
                jobs_with_hits_and_relevant_change += 1
            # Design §3.2: jobs that hit auto-reuse AND then entered editing.
            if getattr(m, "entered_editing", False):
                jobs_with_hits_entered_editing += 1

    def _bucket_payload(hits: int, changes: int) -> dict[str, Any]:
        rate = (changes / hits) if hits > 0 else None
        return {
            "hits": hits,
            "changes": changes,
            "change_rate": rate,
            "threshold_warn": THRESHOLD_WARN,
            "threshold_crit": THRESHOLD_CRIT,
        }

    overall_hits = sum(per_bucket_hits.values())
    overall_changes = sum(per_bucket_changes.values())

    # Codex #4 case_rows: sort by changed_at desc (event time, not
    # job created_at — admin wants the most recent edits first), cap at 20.
    case_rows.sort(
        key=lambda r: (r.get("changed_at") or ""),
        reverse=True,
    )
    case_rows = case_rows[:20]

    return {
        "strong": _bucket_payload(
            per_bucket_hits["strong"], per_bucket_changes["strong"],
        ),
        "strong_named": _bucket_payload(
            per_bucket_hits["strong_named"], per_bucket_changes["strong_named"],
        ),
        "possible_auto": _bucket_payload(
            per_bucket_hits["possible_auto"], per_bucket_changes["possible_auto"],
        ),
        "strong_or_legacy_null": _bucket_payload(
            per_bucket_hits["strong_or_legacy_null"],
            per_bucket_changes["strong_or_legacy_null"],
        ),
        "overall": _bucket_payload(overall_hits, overall_changes),
        # Auxiliary indicator (codex #2 + design §3.4)
        "speaker_reassigned_rate": (
            (total_reassigned_speakers / total_hit_speakers)
            if total_hit_speakers > 0
            else None
        ),
        # Data-contract drift signal (codex #5 + design §3.4)
        "unmapped_segment_count": unmapped_total,
        "unmapped_segment_rate": (
            (unmapped_total / voice_override_total)
            if voice_override_total > 0
            else None
        ),
        "unmapped_threshold_warn": UNMAPPED_WARN,
        # Derived job-level (codex #3 fixed + codex #2 added)
        "jobs_with_voice_change_rate": (
            (jobs_with_hits_and_relevant_change / jobs_with_hits)
            if jobs_with_hits > 0
            else None
        ),
        "auto_reuse_jobs_entering_edit_rate": (
            (jobs_with_hits_entered_editing / jobs_with_hits)
            if jobs_with_hits > 0
            else None
        ),
        # Codex #4: backend produces the case table so frontend doesn't
        # need a second round-trip. Top 20 by created_at desc.
        "case_rows": case_rows,
    }


# ─────────────────────────────────────────────────────────────────────
# Outcome classification
# ─────────────────────────────────────────────────────────────────────


def _classify_smart_outcome(job: Any, smart_state: dict | None) -> str:
    """Bucket a job into one of:

      succeeded_clean                            (status=succeeded, no handoff)
      succeeded_with_handoff_<reason>            (status=succeeded, smart_state.reason set)
      pipeline_failed_<error_type or unknown>    (status=failed)
      in_flight_<status>                         (status=running / editing / queued / etc)

    The handoff bucket uses ``smart_state.reason`` directly because
    spec 2026-05-20 lets a job complete (status=succeeded) while still
    carrying a handoff reason — admins approved it manually but the
    audit trail is preserved.
    """
    status = getattr(job, "status", None) or "unknown"
    smart_state = smart_state or {}

    if status == "succeeded":
        reason = (smart_state.get("reason") or "").strip()
        if reason:
            return f"succeeded_with_handoff_{reason}"
        return "succeeded_clean"

    if status == "failed":
        err = getattr(job, "error_summary", None) or {}
        error_type = (err.get("error_type") if isinstance(err, dict) else None) or "unknown"
        return f"pipeline_failed_{error_type}"

    return f"in_flight_{status}"


# ─────────────────────────────────────────────────────────────────────
# Per-job aggregation
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JobAggregatedMetrics:
    """Per-job snapshot used as the building block for /summary and /csv."""

    job_id: str
    user_id: str
    user_email: str | None
    display_name: str
    status: str
    source_duration_seconds: float | None
    source_duration_minutes: float | None
    total_segments: int | None
    outcome_category: str
    smart_handoff_reason: str | None
    direct_pct: float | None
    dsp_pct: float | None
    rewrite_direct_pct: float | None
    rewrite_dsp_pct: float | None
    forced_dsp_pct: float | None
    short_segment_dsp_pct: float | None
    manual_review_segments: int | None
    entered_editing: bool
    edit_event_count: int
    edit_events_by_type: dict[str, int] = field(default_factory=dict)
    created_at: str | None = None
    # Task #26 — voice auto-reuse quality (design §3.1 + §3.4)
    # Per-bucket sets of speaker_ids that hit each tier.
    voice_reuse_hits: dict[str, set[str]] = field(
        default_factory=_empty_voice_reuse_hits
    )
    # Speakers that the user changed via post_edit_voice_override_changed.
    voice_changed_speakers: set[str] = field(default_factory=set)
    # Speakers that had voice_selection_speaker_reassigned events
    # (auxiliary indicator — codex v2 review #2 + design §3.4).
    speakers_reassigned: set[str] = field(default_factory=set)
    # post_edit_voice_override_changed events whose segment_id couldn't
    # be mapped to a speaker — surfaced at the top of the dashboard as
    # a data-contract drift signal.
    unmapped_segment_count: int = 0
    # Total post_edit_voice_override_changed event count (denominator
    # for unmapped_segment_rate per codex v2 review #5).
    voice_override_event_count: int = 0
    # Per-event details (codex v2 followup #1): each entry holds
    # speaker_id (resolved), before_voice_id, after_voice_id,
    # operation, changed_at. Used by _aggregate_voice_reuse_quality
    # to build case_rows for the admin Tab 4.
    voice_override_details: list[dict[str, Any]] = field(default_factory=list)


def _aggregate_job(job: Any, user_email: str | None) -> JobAggregatedMetrics:
    """Aggregate one Job row + its on-disk audit dir into a metrics snapshot."""
    project_dir_raw = getattr(job, "project_dir", None)
    project_dir = Path(project_dir_raw) if project_dir_raw else None

    alignment = {
        "total_segments": None,
        "direct_pct": None,
        "dsp_pct": None,
        "rewrite_direct_pct": None,
        "rewrite_dsp_pct": None,
        "forced_dsp_pct": None,
        "short_segment_dsp_pct": None,
        "manual_review_segments": None,
    }
    edit_events_by_type: dict[str, int] = {}

    if project_dir is not None:
        report_path = project_dir / "output" / "alignment_report.txt"
        if report_path.exists():
            try:
                alignment = _parse_alignment_report(
                    report_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                logger.warning(
                    "alignment_report parse failed for job %s: %s",
                    getattr(job, "job_id", "?"),
                    exc,
                )

        events_path = project_dir / "audit" / "user_edit_events.jsonl"
        edit_events_by_type = _count_edit_events(events_path)

        # Task #26: voice auto-reuse quality enrichment (design §3.1 + §3.4)
        decisions_path = project_dir / "audit" / "smart_decisions.jsonl"
        voice_reuse_hits = _collect_voice_reuse_hits(decisions_path)
        segment_to_speaker = _load_segment_to_speaker_mapping(project_dir)
        (
            voice_changed_speakers,
            unmapped_segment_count,
            voice_override_event_count,
            voice_override_details,
        ) = _count_voice_overrides_per_speaker(events_path, segment_to_speaker)
        # Codex v2 followup #2: read before/after speaker_id directly
        # from event, NOT via segment_to_speaker mapping.
        speakers_reassigned = _count_speaker_reassigned_per_job(events_path)
    else:
        voice_reuse_hits = _empty_voice_reuse_hits()
        voice_changed_speakers = set()
        speakers_reassigned = set()
        unmapped_segment_count = 0
        voice_override_event_count = 0
        voice_override_details = []

    edit_event_count = sum(edit_events_by_type.values())
    edit_generation = int(getattr(job, "edit_generation", 0) or 0)
    entered_editing = edit_event_count > 0 or edit_generation > 0

    smart_state = getattr(job, "smart_state", None) or {}
    if not isinstance(smart_state, dict):
        smart_state = {}
    smart_handoff_reason = (smart_state.get("reason") or None) if smart_state else None

    outcome_category = _classify_smart_outcome(job, smart_state)

    source_duration_seconds = getattr(job, "source_duration_seconds", None)
    source_duration_minutes: float | None
    if source_duration_seconds is not None:
        source_duration_minutes = float(source_duration_seconds) / 60.0
    else:
        source_duration_minutes = None

    created_at = getattr(job, "created_at", None)
    created_at_iso = None
    if isinstance(created_at, datetime):
        created_at_iso = created_at.isoformat()
    elif created_at:
        created_at_iso = str(created_at)

    display_name = (
        getattr(job, "display_name", None)
        or getattr(job, "title", None)
        or ""
    )

    return JobAggregatedMetrics(
        job_id=str(getattr(job, "job_id", "")),
        user_id=str(getattr(job, "user_id", "") or ""),
        user_email=user_email,
        display_name=display_name,
        status=str(getattr(job, "status", "") or ""),
        source_duration_seconds=(
            float(source_duration_seconds)
            if source_duration_seconds is not None
            else None
        ),
        source_duration_minutes=source_duration_minutes,
        total_segments=alignment["total_segments"],
        outcome_category=outcome_category,
        smart_handoff_reason=smart_handoff_reason,
        direct_pct=alignment["direct_pct"],
        dsp_pct=alignment["dsp_pct"],
        rewrite_direct_pct=alignment["rewrite_direct_pct"],
        rewrite_dsp_pct=alignment["rewrite_dsp_pct"],
        forced_dsp_pct=alignment["forced_dsp_pct"],
        short_segment_dsp_pct=alignment["short_segment_dsp_pct"],
        manual_review_segments=alignment["manual_review_segments"],
        entered_editing=entered_editing,
        edit_event_count=edit_event_count,
        edit_events_by_type=dict(edit_events_by_type),
        created_at=created_at_iso,
        voice_reuse_hits=voice_reuse_hits,
        voice_changed_speakers=voice_changed_speakers,
        speakers_reassigned=speakers_reassigned,
        unmapped_segment_count=unmapped_segment_count,
        voice_override_event_count=voice_override_event_count,
        voice_override_details=voice_override_details,
    )


# ─────────────────────────────────────────────────────────────────────
# Summary payload builder
# ─────────────────────────────────────────────────────────────────────


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _build_summary_payload(
    metrics: list[JobAggregatedMetrics], days: int
) -> dict[str, Any]:
    """Assemble the JSON payload returned by ``GET /summary``.

    Spec: ``docs/plans/2026-05-22-smart-analytics-v1.md`` §3.
    """
    now = datetime.now(timezone.utc)
    window_from = (now - timedelta(days=days)).date().isoformat()
    window_to = now.date().isoformat()

    total = len(metrics)
    succeeded = [m for m in metrics if m.status == "succeeded"]
    failed = [m for m in metrics if m.status == "failed"]
    editing = [m for m in metrics if m.status == "editing"]

    succeeded_with_handoff = [
        m for m in succeeded if m.smart_handoff_reason
    ]
    handoff_count = len(succeeded_with_handoff) + len(failed)
    handoff_rate = (handoff_count / total) if total else 0

    forced_dsp_values = [
        m.forced_dsp_pct for m in succeeded if m.forced_dsp_pct is not None
    ]
    avg_forced_dsp_pct = _safe_mean(forced_dsp_values)

    sorted_forced = sorted(forced_dsp_values)
    if sorted_forced:
        p90_index = max(0, int(round(0.9 * (len(sorted_forced) - 1))))
        p90_forced_dsp_pct = sorted_forced[p90_index]
    else:
        p90_forced_dsp_pct = 0.0

    entered_editing_metrics = [m for m in metrics if m.entered_editing]
    rework_rate = (len(entered_editing_metrics) / total) if total else 0
    avg_edited_segments = _safe_mean(
        [float(m.edit_event_count) for m in entered_editing_metrics]
    )

    # ── handoff distribution: by outcome_category ──
    handoff_buckets: dict[str, list[JobAggregatedMetrics]] = {}
    for m in metrics:
        # The "clean / running" buckets are not interesting handoff causes —
        # include only failures and succeeded-with-handoff for distribution.
        if m.outcome_category == "succeeded_clean":
            continue
        if m.outcome_category.startswith("in_flight_"):
            continue
        handoff_buckets.setdefault(m.outcome_category, []).append(m)
    handoff_distribution = []
    for reason_code, bucket in sorted(
        handoff_buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        handoff_distribution.append({
            "reason_code": reason_code,
            "count": len(bucket),
            "pct": (len(bucket) / total) if total else 0,
            "sample_job_ids": [m.job_id for m in bucket[:3]],
        })

    top_handoff_reason = handoff_distribution[0]["reason_code"] if handoff_distribution else None

    # ── alignment quality table: only jobs with parsed forced_dsp_pct ──
    alignment_rows = []
    for m in metrics:
        if m.forced_dsp_pct is None:
            continue
        alignment_rows.append({
            "job_id": m.job_id,
            "display_name": m.display_name,
            "user_email": m.user_email,
            "source_duration_seconds": m.source_duration_seconds,
            "source_duration_minutes": m.source_duration_minutes,
            "total_segments": m.total_segments,
            "direct_pct": m.direct_pct,
            "dsp_pct": m.dsp_pct,
            "rewrite_direct_pct": m.rewrite_direct_pct,
            "rewrite_dsp_pct": m.rewrite_dsp_pct,
            "forced_dsp_pct": m.forced_dsp_pct,
            "short_segment_dsp_pct": m.short_segment_dsp_pct,
            "manual_review_segments": m.manual_review_segments,
        })
    alignment_rows.sort(
        key=lambda row: row["forced_dsp_pct"], reverse=True
    )

    # ── rework by user ──
    by_user: dict[str, dict[str, Any]] = {}
    for m in metrics:
        bucket = by_user.setdefault(m.user_id, {
            "user_id": m.user_id,
            "user_email": m.user_email,
            "smart_job_count": 0,
            "entered_editing_count": 0,
            "_edit_counts": [],
        })
        bucket["smart_job_count"] += 1
        if m.entered_editing:
            bucket["entered_editing_count"] += 1
            bucket["_edit_counts"].append(float(m.edit_event_count))
    rework_by_user = []
    for uid, bucket in sorted(
        by_user.items(),
        key=lambda kv: (-kv[1]["entered_editing_count"], kv[0]),
    ):
        counts = bucket.pop("_edit_counts")
        rate = (
            bucket["entered_editing_count"] / bucket["smart_job_count"]
            if bucket["smart_job_count"]
            else 0.0
        )
        rework_by_user.append({
            **bucket,
            "rework_rate": rate,
            "avg_edited_segments": _safe_mean(counts),
        })

    # ── edit event distribution: aggregate type counts across all metrics ──
    event_totals: dict[str, int] = {}
    for m in metrics:
        for k, v in m.edit_events_by_type.items():
            event_totals[k] = event_totals.get(k, 0) + int(v)
    grand_total = sum(event_totals.values())
    edit_event_distribution = []
    for event_type, count in sorted(
        event_totals.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        edit_event_distribution.append({
            "event_type": event_type,
            "count": count,
            "pct": (count / grand_total) if grand_total else 0,
        })

    # ── task_table: sort by created_at desc ──
    sorted_metrics = sorted(
        metrics,
        key=lambda m: (m.created_at or ""),
        reverse=True,
    )
    task_table = []
    for m in sorted_metrics:
        task_table.append({
            "job_id": m.job_id,
            "user_id": m.user_id,
            "user_email": m.user_email,
            "display_name": m.display_name,
            "status": m.status,
            "source_duration_minutes": m.source_duration_minutes,
            "total_segments": m.total_segments,
            "smart_handoff_reason": m.smart_handoff_reason,
            "outcome_category": m.outcome_category,
            "forced_dsp_pct": m.forced_dsp_pct,
            "dsp_pct": m.dsp_pct,
            "direct_pct": m.direct_pct,
            "manual_review_segments": m.manual_review_segments,
            "entered_editing": m.entered_editing,
            "edit_event_count": m.edit_event_count,
            "created_at": m.created_at,
            "cost_view_url": f"/admin/jobs/{m.job_id}/cost",
        })

    return {
        "window": {"days": days, "from": window_from, "to": window_to},
        "kpi": {
            "total_smart_jobs": total,
            "succeeded": len(succeeded),
            "failed": len(failed),
            "editing": len(editing),
            "handoff_rate": handoff_rate,
            "top_handoff_reason": top_handoff_reason,
            "avg_forced_dsp_pct": avg_forced_dsp_pct,
            "p90_forced_dsp_pct": p90_forced_dsp_pct,
            "rework_rate": rework_rate,
            "avg_edited_segments": avg_edited_segments,
        },
        "handoff_distribution": handoff_distribution,
        "alignment_quality": alignment_rows,
        "rework_by_user": rework_by_user,
        "edit_event_distribution": edit_event_distribution,
        "task_table": task_table,
        # Task #26 — voice auto-reuse quality block (design §3 + Tab 4)
        "voice_reuse_quality": _aggregate_voice_reuse_quality(metrics),
    }


# ─────────────────────────────────────────────────────────────────────
# CSV builder
# ─────────────────────────────────────────────────────────────────────

_CSV_COLUMNS: list[str] = [
    "job_id",
    "user_email",
    "display_name",
    "status",
    "source_duration_minutes",
    "total_segments",
    "smart_handoff_reason",
    "outcome_category",
    "direct_pct",
    "dsp_pct",
    "forced_dsp_pct",
    "short_segment_dsp_pct",
    "manual_review_segments",
    "entered_editing",
    "edit_event_count",
    "created_at",
    # Task #26 — voice auto-reuse quality, per-job COUNTS (codex review #5).
    # We emit hit/change counts per job, NOT global rates: a per-job row
    # carrying the global rate would be misleading (the value is identical
    # for every row of the same export). Admin can compute rates by
    # summing column groups in Excel if needed; the dashboard already
    # surfaces the rates.
    "strong_hits",
    "strong_named_hits",
    "possible_auto_hits",
    "strong_or_legacy_null_hits",
    "voice_changed_speakers",
    "unmapped_segment_count",
]


def _build_csv(metrics: list[JobAggregatedMetrics]) -> bytes:
    """Render task_table as Excel-compatible CSV with UTF-8 BOM.

    Excel without BOM mis-detects encoding as GBK and mangles Chinese.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)

    # Sort same as task_table (created_at desc) so CSV matches UI order.
    sorted_metrics = sorted(
        metrics,
        key=lambda m: (m.created_at or ""),
        reverse=True,
    )
    for m in sorted_metrics:
        row = []
        for col in _CSV_COLUMNS:
            # Task #26 special columns — derive count from set fields.
            if col in (
                "strong_hits", "strong_named_hits",
                "possible_auto_hits", "strong_or_legacy_null_hits",
            ):
                bucket = col[:-len("_hits")]
                row.append(str(len((m.voice_reuse_hits or {}).get(bucket, set()))))
                continue
            if col == "voice_changed_speakers":
                row.append(str(len(m.voice_changed_speakers or set())))
                continue
            if col == "unmapped_segment_count":
                row.append(str(int(m.unmapped_segment_count or 0)))
                continue
            value = getattr(m, col, None)
            if value is None:
                row.append("")
            elif isinstance(value, bool):
                row.append("true" if value else "false")
            else:
                row.append(str(value))
        writer.writerow(row)

    body = buffer.getvalue().encode("utf-8")
    return b"\xef\xbb\xbf" + body


# ─────────────────────────────────────────────────────────────────────
# DB query helper
# ─────────────────────────────────────────────────────────────────────


def _build_smart_jobs_query(
    *, days: int, status: str | None, user: str | None
):
    """Build the SELECT for smart-mode jobs within the time window.

    Filters:
      service_mode == 'smart'  (always)
      created_at >= now - days
      status filter applied if not 'all'
      user filter applied if not 'all'
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(Job, User)
        .outerjoin(User, Job.user_id == User.id)
        .where(Job.created_at >= cutoff)
        .where(Job.service_mode == "smart")
        .order_by(Job.created_at.desc())
    )
    if status and status != "all":
        stmt = stmt.where(Job.status == status)
    if user and user != "all":
        stmt = stmt.where(Job.user_id == user)
    return stmt


async def _query_smart_jobs(
    db: AsyncSession, *, days: int, status: str | None, user: str | None,
) -> list[tuple[Any, Any]]:
    """Execute the query and return rows as ``[(job, user_or_None), …]``."""
    stmt = _build_smart_jobs_query(days=days, status=status, user=user)
    result = await db.execute(stmt)
    rows = result.all()
    # SQLAlchemy returns Row objects; the test passes plain tuples back via
    # MagicMock so we treat them as iterables either way.
    out: list[tuple[Any, Any]] = []
    for row in rows:
        if isinstance(row, tuple):
            job_obj = row[0]
            owner = row[1] if len(row) > 1 else None
        else:
            job_obj = row[0]
            owner = row[1] if len(row) > 1 else None
        out.append((job_obj, owner))
    return out


def _aggregate_rows(rows: list[tuple[Any, Any]]) -> list[JobAggregatedMetrics]:
    metrics: list[JobAggregatedMetrics] = []
    for job, owner in rows:
        email = getattr(owner, "email", None) if owner is not None else None
        metrics.append(_aggregate_job(job, user_email=email))
    return metrics


def _build_report_jobs_query(
    *,
    days: int,
    status: str | None,
    user: str | None,
    service_mode: str | None,
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(Job, User)
        .outerjoin(User, Job.user_id == User.id)
        .where(Job.created_at >= cutoff)
        .order_by(Job.created_at.desc())
    )
    if status and status != "all":
        stmt = stmt.where(Job.status == status)
    if user and user != "all":
        stmt = stmt.where(Job.user_id == user)
    if service_mode and service_mode != "all":
        stmt = stmt.where(Job.service_mode == service_mode)
    return stmt


async def _query_report_jobs(
    db: AsyncSession,
    *,
    days: int,
    status: str | None,
    user: str | None,
    service_mode: str | None,
) -> list[tuple[Any, Any]]:
    stmt = _build_report_jobs_query(
        days=days,
        status=status,
        user=user,
        service_mode=service_mode,
    )
    result = await db.execute(stmt)
    rows = result.all()
    out: list[tuple[Any, Any]] = []
    for row in rows:
        if isinstance(row, tuple):
            job_obj = row[0]
            owner = row[1] if len(row) > 1 else None
        else:
            job_obj = row[0]
            owner = row[1] if len(row) > 1 else None
        out.append((job_obj, owner))
    return out


def _aggregate_report_rows(rows: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for job, owner in rows:
        job_id = str(getattr(job, "job_id", "") or "")
        project_dir = getattr(job, "project_dir", None)
        report_summary = summarize_project_reports(project_dir, job_id=job_id)
        created_at = getattr(job, "created_at", None)
        if isinstance(created_at, datetime):
            created_at_iso = created_at.isoformat()
        elif created_at:
            created_at_iso = str(created_at)
        else:
            created_at_iso = None
        out.append({
            "job_id": job_id,
            "user_id": str(getattr(job, "user_id", "") or ""),
            "user_email": getattr(owner, "email", None) if owner is not None else None,
            "display_name": (
                getattr(job, "display_name", None)
                or getattr(job, "title", None)
                or ""
            ),
            "status": str(getattr(job, "status", "") or ""),
            "service_mode": str(getattr(job, "service_mode", "") or ""),
            "created_at": created_at_iso,
            "project_dir_name": report_summary.get("project_dir_name"),
            "reports": {
                "translation_quality": report_summary["translation_quality"],
                "subtitle_width": report_summary["subtitle_width"],
                "speaker_evidence": report_summary["speaker_evidence"],
                "voice_sample_scoring": report_summary["voice_sample_scoring"],
            },
            "cost_view_url": f"/admin/jobs/{job_id}/cost" if job_id else "",
        })
    return out


def _json_response(status_code: int, body: Any) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_PHASE1B_FLAG_SPECS: dict[str, dict[str, Any]] = {
    "translation_script_gate_shadow": {
        "admin_key": "phase1b_translation_script_gate_shadow",
        "env": "AVT_TRANSLATION_SCRIPT_GATE_SHADOW",
        "label": "Translation script gate shadow",
        "category": "shadow",
        "implemented": True,
        "risk": "low",
    },
    "voice_sample_scoring_shadow": {
        "admin_key": "phase1b_voice_sample_scoring_shadow",
        "env": "AVT_VOICE_SAMPLE_SCORING_SHADOW",
        "label": "Voice sample scoring shadow",
        "category": "shadow",
        "implemented": True,
        "risk": "low",
    },
    "translation_script_gate": {
        "admin_key": "phase1b_translation_script_gate_enabled",
        "env": "AVT_TRANSLATION_SCRIPT_GATE",
        "label": "Translation script gate behavior",
        "category": "behavior",
        "implemented": False,
        "risk": "medium",
    },
    "voice_sample_scoring": {
        "admin_key": "phase1b_voice_sample_scoring_enabled",
        "env": "AVT_VOICE_SAMPLE_SCORING",
        "label": "Voice sample scoring behavior",
        "category": "behavior",
        "implemented": False,
        "risk": "high",
    },
    "audio_tail_trim": {
        "admin_key": "phase1b_audio_tail_trim_enabled",
        "env": "AVT_AUDIO_TAIL_TRIM",
        "label": "Audio tail trim behavior",
        "category": "behavior",
        "implemented": False,
        "risk": "medium",
    },
    "whisper_quality_gate": {
        "admin_key": "phase1b_whisper_quality_gate_enabled",
        "env": "AVT_WHISPER_QUALITY_GATE",
        "label": "Whisper quality gate behavior",
        "category": "behavior",
        "implemented": False,
        "risk": "medium",
    },
}


class Phase1bFlagUpdate(BaseModel):
    flags: dict[str, bool] = Field(default_factory=dict)


def _env_flag_value(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _load_raw_admin_settings() -> dict[str, Any]:
    path = admin_settings_store.SETTINGS_FILE
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("phase1b flags: failed to parse %s", path, exc_info=True)
    return {}


def _build_phase1b_flags_payload() -> dict[str, Any]:
    raw_settings = _load_raw_admin_settings()
    flags: list[dict[str, Any]] = []
    for public_key, spec in _PHASE1B_FLAG_SPECS.items():
        admin_raw = raw_settings.get(spec["admin_key"])
        admin_value = admin_raw if isinstance(admin_raw, bool) else None
        env_value = _env_flag_value(str(spec["env"]))
        if admin_value is not None:
            effective = admin_value
            source = "admin_settings"
        elif env_value is not None:
            effective = env_value
            source = "env"
        else:
            effective = False
            source = "default"
        flags.append({
            "key": public_key,
            "admin_key": spec["admin_key"],
            "env": spec["env"],
            "label": spec["label"],
            "category": spec["category"],
            "implemented": spec["implemented"],
            "risk": spec["risk"],
            "admin_value": admin_value,
            "env_value": env_value,
            "effective": effective,
            "effective_source": source,
        })
    return {"flags": flags}


def _update_phase1b_admin_flags(updates: dict[str, bool]) -> dict[str, Any]:
    unknown = sorted(set(updates) - set(_PHASE1B_FLAG_SPECS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_phase1b_flags", "flags": unknown},
        )
    settings = admin_settings_store.load_settings()
    raw_settings = _load_raw_admin_settings()
    for public_key, spec in _PHASE1B_FLAG_SPECS.items():
        admin_key = str(spec["admin_key"])
        if public_key in updates or admin_key in raw_settings:
            continue
        env_value = _env_flag_value(str(spec["env"]))
        if env_value is not None:
            setattr(settings, admin_key, env_value)
    for public_key, enabled in updates.items():
        admin_key = str(_PHASE1B_FLAG_SPECS[public_key]["admin_key"])
        setattr(settings, admin_key, bool(enabled))
    admin_settings_store.save_settings(settings)
    return _build_phase1b_flags_payload()


@router.get("/summary")
async def get_summary(
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
    user: str = Query("all"),
    user_acc: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return the full smart-analytics summary JSON payload."""
    _require_admin(user_acc)
    rows = await _query_smart_jobs(db, days=days, status=status, user=user)
    metrics = _aggregate_rows(rows)
    payload = _build_summary_payload(metrics, days=days)
    payload["filters"] = {"status": status, "user": user}
    return _json_response(200, payload)


@router.get("/csv")
async def get_csv(
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
    user: str = Query("all"),
    user_acc: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return the smart-analytics task_table as Excel-compatible CSV."""
    _require_admin(user_acc)
    rows = await _query_smart_jobs(db, days=days, status=status, user=user)
    metrics = _aggregate_rows(rows)
    body = _build_csv(metrics)
    filename = f"smart-analytics-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return Response(
        content=body,
        status_code=200,
        headers={
            "content-type": "text/csv; charset=utf-8",
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/job-reports-summary")
async def get_job_reports_summary(
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
    user: str = Query("all"),
    service_mode: str = Query("all"),
    user_acc: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Aggregate Phase 1a/1b job report sidecars across recent jobs."""
    _require_admin(user_acc)
    rows = await _query_report_jobs(
        db,
        days=days,
        status=status,
        user=user,
        service_mode=service_mode,
    )
    job_rows = _aggregate_report_rows(rows)
    payload = build_phase1b_summary(job_rows, days=days)
    payload["filters"] = {
        "status": status,
        "user": user,
        "service_mode": service_mode,
    }
    return _json_response(200, payload)


@router.get("/job-reports-csv")
async def get_job_reports_csv(
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
    user: str = Query("all"),
    service_mode: str = Query("all"),
    user_acc: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export Phase 1a/1b report analysis rows as Excel-compatible CSV."""
    _require_admin(user_acc)
    rows = await _query_report_jobs(
        db,
        days=days,
        status=status,
        user=user,
        service_mode=service_mode,
    )
    body = build_phase1b_csv(_aggregate_report_rows(rows))
    filename = f"job-report-analysis-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return Response(
        content=body,
        status_code=200,
        headers={
            "content-type": "text/csv; charset=utf-8",
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/phase1b-flags")
async def get_phase1b_flags(
    user_acc: User | None = Depends(get_current_user),
) -> Response:
    _require_admin(user_acc)
    return _json_response(200, _build_phase1b_flags_payload())


@router.post(
    "/phase1b-flags",
    dependencies=[Depends(require_same_origin_state_change)],
)
async def update_phase1b_flags(
    body: Phase1bFlagUpdate,
    user_acc: User | None = Depends(get_current_user),
) -> Response:
    _require_admin(user_acc)
    payload = _update_phase1b_admin_flags(body.flags)
    return _json_response(200, payload)

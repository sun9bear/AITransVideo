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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from auth import get_current_user
from database import get_db
from models import Job, User


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/smart-analytics", tags=["admin-smart-analytics"]
)


# ─────────────────────────────────────────────────────────────────────
# Admin gate (mirror ``admin_cost_api._require_admin`` shape)
# ─────────────────────────────────────────────────────────────────────


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if (getattr(user, "role", None) or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


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


def _json_response(status_code: int, body: Any) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


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
